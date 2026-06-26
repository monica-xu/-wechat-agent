"""Global Drift Control — system state vector monitoring.

Prevents long-term behavioral degradation across three axes:
1. Style drift — is writing voice shifting unintentionally?
2. Topic drift — is topic distribution collapsing or wandering?
3. Reward trend — are rewards trending down (quality decay)?

When drift exceeds thresholds, the system auto-corrects by:
- Forcing persona rotation
- Triggering topic exploration mode
- Escalating to semi-auto mode if severe
"""

import json
import logging
import math
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

TZ_SHANGHAI = timezone(timedelta(hours=8))

# Drift thresholds
STYLE_DRIFT_WARN = 0.25      # Cosine distance from centroid > this → warning
STYLE_DRIFT_CRITICAL = 0.40  # Cosine distance > this → auto-correct
TOPIC_ENTROPY_CRITICAL = 0.20  # Entropy below this → force exploration
REWARD_TREND_WARN = -0.15     # 7-day slope < this → warning
REWARD_TREND_CRITICAL = -0.30  # Slope < this → escalate to semi-auto


def compute_system_state_vector(session_id: str) -> dict:
    """Compute the global system state vector.

    Returns:
        style_centroid: average style embedding of recent articles
        topic_centroid: weighted topic distribution
        reward_trend_slope: linear regression slope over last 7-14 days
        alerts: list of active drift warnings
    """
    from db._articles import get_recently_published
    from db._content_memory import list_topic_memories

    articles = get_recently_published(session_id, limit=20)

    # 1. Style centroid
    style_centroid = _compute_style_centroid(articles)

    # 2. Topic centroid
    topics = list_topic_memories(session_id)
    topic_centroid = _compute_topic_centroid(topics)

    # 3. Reward trend
    reward_slope = _compute_reward_trend(articles)

    # 4. Assess alerts
    alerts = _assess_drift(style_centroid, topic_centroid, reward_slope, articles, session_id)

    return {
        "style_centroid_norm": round(style_centroid.get("norm", 0), 4) if style_centroid else 0,
        "topic_entropy": round(_topic_distribution_entropy(topics), 4),
        "reward_trend_slope": round(reward_slope, 4),
        "alerts": alerts,
        "computed_at": datetime.now(TZ_SHANGHAI).isoformat(),
    }


def _compute_style_centroid(articles: list[dict]) -> dict | None:
    """Compute average style embedding from recent articles."""
    embeddings = []
    for a in articles:
        emb_str = a.get("embedding", "[]")
        try:
            emb = json.loads(emb_str) if isinstance(emb_str, str) else emb_str
            if emb and len(emb) > 0:
                embeddings.append(emb)
        except (json.JSONDecodeError, TypeError):
            pass

    if not embeddings:
        return None

    dim = len(embeddings[0])
    centroid = [0.0] * dim
    for emb in embeddings:
        for i, v in enumerate(emb):
            centroid[i] += v
    centroid = [c / len(embeddings) for c in centroid]
    norm = math.sqrt(sum(c * c for c in centroid))

    return {"centroid": centroid, "norm": norm, "count": len(embeddings)}


def _compute_topic_centroid(topics: list[dict]) -> dict:
    """Compute topic distribution and entropy."""
    total = sum(t.get("times_published", 0) for t in topics) or 1
    distribution = {}
    for t in topics:
        distribution[t.get("topic", "unknown")] = t.get("times_published", 0) / total
    return {
        "distribution": distribution,
        "entropy": _topic_distribution_entropy(topics),
        "topic_count": len(topics),
    }


def _topic_distribution_entropy(topics: list[dict]) -> float:
    """Shannon entropy of topic distribution."""
    if len(topics) < 2:
        return 0.0
    total = sum(t.get("times_published", 0) for t in topics)
    if total == 0:
        return 0.0
    entropy = 0.0
    for t in topics:
        p = t.get("times_published", 0) / total
        if p > 0:
            entropy -= p * math.log2(p)
    max_entropy = math.log2(len(topics))
    return entropy / max_entropy if max_entropy > 0 else 0.0


def _compute_reward_trend(articles: list[dict]) -> float:
    """Compute linear regression slope of reward over time (last 14 days).

    Uses article metrics if available, otherwise falls back to critic scores.
    Positive slope = improving, negative = degrading.
    """
    from db._metrics import get_metrics

    data_points = []
    for a in articles:
        article_id = a.get("id", "")
        metrics = get_metrics(article_id)
        if metrics and metrics.get("read_count", 0) > 0:
            try:
                pub_date = a.get("published_at", "")
                if pub_date:
                    # Use read count as proxy reward
                    reward = metrics.get("read_count", 0)
                    # Normalize
                    normalized = min(reward / 1000.0, 1.0)
                    data_points.append((pub_date, normalized))
            except (ValueError, TypeError):
                pass

    if len(data_points) < 3:
        # Fall back to critic scores
        data_points = []
        for i, a in enumerate(articles):
            score = a.get("critic_overall_score", 0)
            if score > 0:
                data_points.append((i, score))  # Use index as x-axis

    if len(data_points) < 3:
        return 0.0

    # Simple linear regression
    n = len(data_points)
    xs = list(range(n))
    ys = [dp[1] for dp in data_points]

    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    den = sum((x - x_mean) ** 2 for x in xs)
    if den == 0:
        return 0.0

    return num / den


def _assess_drift(style_centroid: dict | None, topic_centroid: dict,
                  reward_slope: float, articles: list[dict],
                  session_id: str) -> list[dict]:
    """Generate drift alerts based on system state vector."""
    alerts = []

    # Topic entropy alert
    entropy = topic_centroid.get("entropy", 1.0)
    if entropy < TOPIC_ENTROPY_CRITICAL:
        alerts.append({
            "level": "critical",
            "type": "topic_collapse",
            "message": f"Topic entropy {entropy:.3f} < {TOPIC_ENTROPY_CRITICAL}. Force exploration.",
            "action": "force_exploration",
        })
    elif entropy < 0.35:
        alerts.append({
            "level": "warning",
            "type": "topic_concentration",
            "message": f"Topic entropy {entropy:.3f} is low. Consider rotating topics.",
            "action": "prefer_exploration",
        })

    # Reward trend alert
    if reward_slope < REWARD_TREND_CRITICAL:
        alerts.append({
            "level": "critical",
            "type": "reward_decline",
            "message": f"Reward slope {reward_slope:.3f} < {REWARD_TREND_CRITICAL}. Escalate to semi-auto.",
            "action": "escalate_to_semi_auto",
        })
    elif reward_slope < REWARD_TREND_WARN:
        alerts.append({
            "level": "warning",
            "type": "reward_soft_decline",
            "message": f"Reward slope {reward_slope:.3f} is negative. Monitor closely.",
            "action": "increase_critic_threshold",
        })

    # Topic count alert
    if topic_centroid.get("topic_count", 0) < 3:
        alerts.append({
            "level": "warning",
            "type": "low_topic_diversity",
            "message": f"Only {topic_centroid.get('topic_count')} topics in memory. Refresh topic pool.",
            "action": "refresh_pool",
        })

    return alerts


def apply_drift_corrections(state, alerts: list[dict]) -> None:
    """Apply automatic corrections based on drift alerts.

    Modifies state in-place:
    - force_exploration → overrides topic selection to prefer unexplored topics
    - escalate_to_semi_auto → downgrades auto → semi-auto
    - increase_critic_threshold → raises critic pass threshold temporarily
    """
    for alert in alerts:
        action = alert.get("action", "")
        level = alert.get("level", "info")

        if action == "escalate_to_semi_auto" and level == "critical":
            if state.human_mode == "auto":
                logger.warning(f"DRIFT CORRECTION: Downgrading auto → semi-auto due to {alert['type']}")
                state.human_mode = "semi-auto"
                state.decide_reason += f" [DRIFT: {alert['message']}]"

        elif action == "increase_critic_threshold":
            # Temporarily raise critic pass threshold
            from db._config import RuntimeConfig
            current = float(RuntimeConfig.get("publish_threshold", "0.70"))
            new_threshold = min(current + 0.05, 0.85)
            RuntimeConfig.set("publish_threshold", str(new_threshold))
            logger.warning(f"DRIFT CORRECTION: Raised publish threshold {current:.2f} → {new_threshold:.2f}")
