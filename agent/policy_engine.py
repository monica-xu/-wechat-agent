"""Policy Engine — scoring model for publish decision (3-layer: time + content + risk).

Replaces rule-chain with a continuous scoring model:
    publish_score = w_time * time_score + w_content * content_score + w_risk * risk_score

All weights are DB-configurable (runtime_config table).
Bandit upgrade path: weights can be learned via UCB/Thompson sampling over article performance.
"""

import logging
from datetime import datetime, timezone, timedelta

from db._articles import count_published_today, count_published_this_week, get_recently_published
from db._content_memory import get_active_topics
from db._config import RuntimeConfig

logger = logging.getLogger(__name__)

TZ_SHANGHAI = timezone(timedelta(hours=8))


async def compute_publish_score(state, runtime) -> dict:
    """Three-layer scoring model. Returns a dict with all factor values and final score.

    Output:
        should_publish: bool
        publish_score: float (0-1)
        time_score, content_score, risk_score: float (0-1 each)
        mode_override: "normal" | "skip" | "force"
        reason: str
        confidence: float
    """
    sid = state.session_id

    # Fetch configurable weights
    weights = await RuntimeConfig.get_scoring_weights(sid)
    threshold = await RuntimeConfig.get_publish_threshold(sid)

    # Layer 1: Time-based score
    time_score = _compute_time_score(state)

    # Layer 2: Content-based score
    content_score = await _compute_content_score(sid)

    # Layer 3: Risk-based score
    risk_score = await _compute_risk_score(sid, state)

    # Weighted final score
    publish_score = (
        weights["time"] * time_score
        + weights["content"] * content_score
        + weights["risk"] * risk_score
    )

    # Decision
    if publish_score >= threshold:
        should_publish = True
        mode_override = "normal"
        reason = f"Score {publish_score:.2f} >= {threshold} (time={time_score:.2f}, content={content_score:.2f}, risk={risk_score:.2f})"
    elif publish_score >= 0.5:
        should_publish = True
        mode_override = "normal"
        reason = f"Score {publish_score:.2f} in borderline range (0.5-{threshold}), publishing with warning"
    else:
        should_publish = False
        mode_override = "skip"
        reason = f"Score {publish_score:.2f} < {threshold}, skipping (time={time_score:.2f}, content={content_score:.2f}, risk={risk_score:.2f})"

    # Force mode detection: if manual trigger or explicit force flag
    if state.mode_override == "force":
        should_publish = True
        mode_override = "force"
        reason = "Manual force trigger — skipping all checks"

    confidence = min(publish_score / threshold, 1.0) if should_publish else 1.0

    return {
        "should_publish": should_publish,
        "publish_score": round(publish_score, 4),
        "time_score": round(time_score, 4),
        "content_score": round(content_score, 4),
        "risk_score": round(risk_score, 4),
        "mode_override": mode_override,
        "reason": reason,
        "confidence": round(confidence, 4),
    }


def _compute_time_score(state) -> float:
    """Layer 1: Time-based scoring.

    Factors:
    - cooldown: hours since last publish (>18h = 1.0, 12-18h = 0.5, <12h = 0.0)
    - is_weekday: weekday=1.0, weekend=0.3
    - in_publish_window: 8:00-18:00 = 1.0, else = 0.3
    """
    now = datetime.now(TZ_SHANGHAI)
    hour = now.hour
    weekday = now.weekday()  # 0=Mon, 6=Sun

    # Cooldown check
    sid = state.session_id
    recent = get_recently_published(sid, limit=1)
    cooldown_score = 1.0
    if recent and recent[0].get("published_at"):
        try:
            last_pub = datetime.fromisoformat(recent[0]["published_at"])
            hours_since = (now - last_pub.replace(tzinfo=TZ_SHANGHAI)).total_seconds() / 3600
            if hours_since < 12:
                cooldown_score = 0.0
            elif hours_since < 18:
                cooldown_score = 0.5
            else:
                cooldown_score = 1.0
        except (ValueError, TypeError):
            cooldown_score = 1.0

    # Weekday score
    weekday_score = 1.0 if weekday < 5 else 0.3

    # Window score
    window_score = 1.0 if 8 <= hour <= 18 else 0.3

    return 0.5 * cooldown_score + 0.25 * weekday_score + 0.25 * window_score


async def _compute_content_score(session_id: str) -> float:
    """Layer 2: Content readiness scoring.

    Factors:
    - pool_size: ≥5 = 1.0, 3-4 = 0.7, <3 = 0.3
    - pool_avg_quality: avg trend_score ≥ 0.6 = 1.0, else = 0.5
    - has_fresh: topics fetched in last 6h = 1.0, else = 0.5
    """
    topics = get_active_topics(session_id, min_score=0.0)
    pool_size = len(topics)

    # Pool size
    if pool_size >= 5:
        size_score = 1.0
    elif pool_size >= 3:
        size_score = 0.7
    else:
        size_score = 0.3

    # Average quality
    if pool_size > 0:
        avg_quality = sum(t.get("trend_score", 0) for t in topics) / pool_size
        quality_score = 1.0 if avg_quality >= 0.6 else max(avg_quality / 0.6, 0.3)
    else:
        quality_score = 0.1

    # Freshness (check if any topic was fetched recently)
    fresh_score = 0.5  # default
    if pool_size > 0:
        now = datetime.now(TZ_SHANGHAI)
        for t in topics:
            try:
                fetched = datetime.fromisoformat(t.get("fetched_at", ""))
                if (now - fetched.replace(tzinfo=TZ_SHANGHAI)).total_seconds() < 6 * 3600:
                    fresh_score = 1.0
                    break
            except (ValueError, TypeError):
                pass

    return 0.4 * size_score + 0.4 * quality_score + 0.2 * fresh_score


async def _compute_risk_score(session_id: str, state) -> float:
    """Layer 3: Risk assessment. Higher score = lower risk.

    Factors:
    - daily_count_ok: today < limit = 1.0
    - weekly_count_ok: this week < limit = 1.0
    - wechat_health: token validity and API connectivity check
    - similarity_risk: topic overlap with recently published articles
    """
    daily_limit = int(RuntimeConfig.get("daily_publish_limit", 1))
    weekly_limit = int(RuntimeConfig.get("weekly_publish_limit", 5))

    daily_count = count_published_today(session_id)
    weekly_count = count_published_this_week(session_id)

    daily_ok = 1.0 if daily_count < daily_limit else 0.0
    weekly_ok = 1.0 if weekly_count < weekly_limit else 0.0

    # WeChat health: lightweight connectivity check via token refresh.
    # Uses the cached token if valid; only hits the API if token is expired.
    # Fail-open: health=1.0 on error (don't block publish on transient API issues).
    wechat_health = await _check_wechat_health()

    # Similarity risk: checks topic-level overlap with recently published articles.
    # Higher overlap with recent publishes → higher risk → lower score.
    similarity_risk = _compute_similarity_risk(session_id)

    return 0.35 * daily_ok + 0.25 * weekly_ok + 0.20 * wechat_health + 0.20 * similarity_risk


async def _check_wechat_health() -> float:
    """Lightweight WeChat API health check. Returns 1.0 (healthy) or 0.0 (degraded).

    Uses the WeChatClient's built-in token cache — only triggers a real API
    call if the cached token is expired. Fail-open: returns 1.0 on any error
    to avoid blocking publish due to transient network issues.
    """
    try:
        from infra.wechat_api import WeChatClient, WeChatAuthError
        client = WeChatClient()
        token = await client._ensure_token()
        return 1.0 if token else 0.0
    except WeChatAuthError:
        logger.warning("WeChat health check: authentication failed (check WECHAT_APP_ID/SECRET)")
        return 0.0
    except Exception as e:
        logger.warning(f"WeChat health check failed (fail-open → 1.0): {e}")
        return 1.0


def _compute_similarity_risk(session_id: str) -> float:
    """Compute topic-overlap risk with recently published articles.

    Strategy: compare active topic pool topics against recently published
    article topics. High overlap → elevated risk of repeating ourselves.

    Returns 0.0-1.0 where 1.0 = low risk (no overlap), 0.0 = high risk (all
    candidate topics were recently published).
    """
    from db._content_memory import get_active_topics

    recent = get_recently_published(session_id, limit=5)
    if not recent:
        return 1.0  # No history → no similarity risk

    recent_topics = set()
    for r in recent:
        topic = r.get("topic", "")
        if topic:
            recent_topics.add(topic)

    if not recent_topics:
        return 1.0

    # Check overlap between active topic pool and recently published topics
    active = get_active_topics(session_id, min_score=0.0)
    if not active:
        return 0.5  # No candidates → moderate risk (can't assess)

    active_topic_names = {t.get("topic", "") for t in active if t.get("topic")}
    if not active_topic_names:
        return 0.5

    overlap = active_topic_names & recent_topics
    overlap_ratio = len(overlap) / len(active_topic_names)

    # Map overlap ratio to risk score:
    #   no overlap → 1.0  (safe)
    #   full overlap → 0.1 (high risk, but not zero — topic may have a new angle)
    #   linear interpolation between
    if overlap_ratio == 0:
        return 1.0
    elif overlap_ratio >= 0.8:
        return 0.2
    else:
        return 1.0 - overlap_ratio
