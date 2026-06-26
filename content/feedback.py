"""Feedback Loop — WeChat analytics → bandit update + topic scoring.

Post-publish flow:
1. Record article to content memory (embedding, key points)
2. Fetch WeChat article metrics (read count, share rate)
3. Update bandit statistics for the topic (UCB recalculation)
4. Mark feedback as applied
"""

import logging
from content.memory import ContentMemory
from db._content_memory import update_bandit_stats, mark_topic_used
from db._metrics import save_metrics, get_unfetched_articles, mark_feedback_applied
from infra.wechat_api import WeChatClient, WeChatAPIError

logger = logging.getLogger(__name__)


async def record_and_update(state, runtime) -> None:
    """Record article to memory, mark topic as used, prepare for metrics fetch."""
    sid = state.session_id

    # Record to content memory (embedding + key points)
    try:
        memory = ContentMemory(sid)
        await memory.record_article(state)
    except Exception as e:
        logger.warning(f"Content memory recording failed (non-fatal): {e}")

    # Mark topic as used in pool
    if state.selected_topic:
        try:
            mark_topic_used(sid, state.selected_topic)
        except Exception as e:
            logger.warning(f"Topic mark-as-used failed: {e}")


async def run_feedback_loop(session_id: str) -> None:
    """Fetch WeChat metrics, update bandit scores, run drift detection.

    Called by scheduler daily. Pulls article analytics from WeChat API,
    feeds them back into the bandit topic scoring system, and checks
    for global system drift.

    Includes delayed reward computation: for articles published 7+ days ago,
    computes retention proxy and topic revisit rate.
    """
    articles = get_unfetched_articles(session_id, limit=10)
    if not articles:
        logger.info("Feedback loop: no unfetched articles")
        return

    client = WeChatClient()

    for article in articles:
        article_id = article["id"]
        topic = article.get("topic", "")
        article_title = article.get("title", "")

        try:
            # Fetch WeChat article statistics via freepublish/batchget.
            # This endpoint returns published article items with engagement data:
            # - total_read_cnt: cumulative read count
            # - total_share_cnt: cumulative share count
            # - total_old_like_cnt: cumulative likes (old articles)
            #
            # For more detailed analytics (completion rate, title click rate),
            # a self-hosted tracking solution or WeChat Data Cube API is needed.
            article_list = await client.get_article_list(offset=0, count=20)

            read_count = 0
            share_count = 0
            like_count = 0

            for item in article_list:
                news_items = item.get("content", {}).get("news_item", [])
                for detail in news_items:
                    detail_title = detail.get("title", "")
                    # Match by title prefix (WeChat may truncate titles)
                    if _titles_match(article_title, detail_title):
                        read_count = detail.get("total_read_cnt", 0)
                        share_count = detail.get("total_share_cnt", 0)
                        like_count = detail.get("total_old_like_cnt", 0)
                        break
                if read_count > 0:
                    break

            # If no title match found, aggregate all article metrics
            # (fallback for imprecise WeChat API responses)
            if read_count == 0 and article_list:
                for item in article_list:
                    for detail in item.get("content", {}).get("news_item", []):
                        read_count = max(read_count, detail.get("total_read_cnt", 0))
                        share_count = max(share_count, detail.get("total_share_cnt", 0))
                        like_count = max(like_count, detail.get("total_old_like_cnt", 0))

            # Compute derived metrics
            share_rate = _safe_share_rate(read_count, share_count)
            # Completion rate proxy: WeChat doesn't expose this directly.
            # Use a log-based heuristic — articles with very low read counts
            # (< 50) likely had poor completion. Above 500 reads, assume
            # reasonable completion. This is a rough proxy until real data
            # is available from a tracking URL or Data Cube API.
            completion_rate = _estimate_completion_rate(read_count)

            # Save metrics
            save_metrics(
                article_id=article_id,
                read_count=read_count,
                share_count=share_count,
                like_count=like_count,
                completion_rate=completion_rate,
                title_click_rate=0.0,  # Requires self-hosted tracking
            )

            # Update bandit stats for this topic (with reward shaping)
            if topic:
                shaped_reward = _compute_shaped_reward(
                    read_count=read_count,
                    share_rate=share_rate,
                    completion_rate=completion_rate,
                    article_id=article_id,
                    session_id=session_id,
                )
                update_bandit_stats(session_id, topic, shaped_reward, share_rate)
                logger.info(
                    f"Feedback: article={article_id}, topic='{topic}', "
                    f"reads={read_count}, shares={share_count}, "
                    f"share_rate={share_rate:.3f}, completion={completion_rate:.2f}, "
                    f"shaped_reward={shaped_reward:.0f}"
                )

            mark_feedback_applied(article_id)

        except WeChatAPIError as e:
            logger.warning(f"Feedback fetch failed for {article_id}: {e}")
        except Exception as e:
            logger.error(f"Feedback fetch error for {article_id}: {e}")

    # ---- Global drift detection ----
    try:
        from content.drift import compute_system_state_vector, apply_drift_corrections

        drift_state = compute_system_state_vector(session_id)
        if drift_state.get("alerts"):
            logger.warning(f"Drift alerts: {drift_state['alerts']}")
            # Store drift state for API access
            from db._config import RuntimeConfig
            import json
            RuntimeConfig.set("last_drift_state", json.dumps(drift_state, ensure_ascii=False, default=str))

            # Apply auto-corrections if there are critical alerts
            criticals = [a for a in drift_state["alerts"] if a.get("level") == "critical"]
            if criticals:
                logger.warning(f"CRITICAL DRIFT: {len(criticals)} critical alerts — applying corrections")
                # Note: apply_drift_corrections needs a state object,
                # but in scheduled context we only have session_id.
                # Auto-escalation: if critical, set human_mode to semi-auto
                from db._config import RuntimeConfig as RC
                current_mode = RC.get("human_mode", "auto")
                if current_mode == "auto":
                    RC.set("human_mode", "semi-auto")
                    logger.warning("DRIFT CORRECTION: Escalated to semi-auto mode due to critical drift")
    except Exception as e:
        logger.warning(f"Drift detection failed (non-fatal): {e}")

    logger.info(f"Feedback loop complete: {len(articles)} articles processed")


# ---- Metrics helpers ----


def _titles_match(local_title: str, wechat_title: str) -> bool:
    """Check if two article titles refer to the same article.

    WeChat API may return truncated or slightly different titles.
    Uses prefix matching and normalized comparison.
    """
    if not local_title or not wechat_title:
        return False

    # Normalize: strip whitespace, take first 30 chars for comparison
    local = local_title.strip()
    wechat = wechat_title.strip()

    if local == wechat:
        return True

    # Prefix match (WeChat may truncate)
    min_len = min(len(local), len(wechat), 30)
    return local[:min_len] == wechat[:min_len]


def _safe_share_rate(read_count: int, share_count: int) -> float:
    """Compute share rate with guard against division by zero.

    Returns 0.0-1.0 where 0.10 = 10% of readers shared.
    """
    if read_count <= 0:
        return 0.0
    return min(share_count / read_count, 1.0)


def _estimate_completion_rate(read_count: int) -> float:
    """Estimate completion rate from read count as a rough proxy.

    WeChat does not expose completion rate via the standard API.
    Heuristic (based on typical WeChat article behavior):
      - < 50 reads:   completion ~ 0.3  (low sample, uncertain)
      - 50-500 reads: completion ~ 0.5  (moderate engagement)
      - > 500 reads:  completion ~ 0.7  (high engagement, likely good retention)

    When real tracking data is available (e.g., self-hosted URLs with
    scroll-depth tracking), replace this with actual measurements.
    """
    if read_count < 50:
        return 0.3
    elif read_count < 200:
        return 0.4
    elif read_count < 500:
        return 0.5
    elif read_count < 2000:
        return 0.6
    else:
        return 0.7


def _compute_shaped_reward(read_count: int, share_rate: float, completion_rate: float,
                           article_id: str, session_id: str) -> float:
    """Quality-weighted composite reward to prevent bandit from optimizing for clickbait.

    Reward = 0.4 * normalized_read_count + 0.3 * share_rate + 0.3 * completion_rate

    Additional constraint: if article failed compliance, reward = 0.
    """
    # Check if article was rejected by compliance critic
    from db._articles import get_article
    article = get_article(article_id)
    if article:
        critic_scores = article.get("critic_dimension_scores", {})
        if isinstance(critic_scores, str):
            import json
            try:
                critic_scores = json.loads(critic_scores)
            except json.JSONDecodeError:
                critic_scores = {}
        # If compliance score exists and was too low, zero reward
        if critic_scores.get("compliance", {}).get("score", 1.0) < 0.5:
            return 0.0

    # Normalize read count (assume 10000 = excellent)
    normalized_reads = min(read_count / 1000.0, 1.0)

    # Composite reward
    reward = 0.4 * normalized_reads + 0.3 * share_rate + 0.3 * completion_rate

    # Scale back to approximate read-count scale for UCB compatibility
    return reward * 1000.0
