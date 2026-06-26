"""Topic Agent — select the best topic + angle from candidate pool."""

import json
import logging
from infra.llm import acall_llm
from agent.prompts import TOPIC_SYSTEM_PROMPT
from db._content_memory import get_active_topics, get_bandit_topics, get_topic_cooldown_days
from content.constants import BANDIT_EXPLORATION_WEIGHT

logger = logging.getLogger(__name__)


async def run_topic(state, runtime) -> dict:
    """Select topic and angle. Returns {topic, angle, source, reason, confidence}."""
    sid = state.session_id

    # Fetch candidates: bandit-scored topics + active topic pool
    bandit_topics = get_bandit_topics(sid, limit=5)
    active_topics = get_active_topics(sid, min_score=0.3)

    # Build context for LLM
    bandit_str = "\n".join(
        f"- [{t['topic']}] UCB={t.get('ucb_score', 0):.3f}, published={t.get('times_published', 0)}次, avg_read={t.get('avg_read_count', 0):.0f}"
        for t in bandit_topics
    ) if bandit_topics else "（暂无历史数据）"

    pool_str = "\n".join(
        f"- [{t['topic']}] trend={t.get('trend_score', 0):.2f}, source={t.get('source', 'auto')}"
        for t in active_topics[:10]
    ) if active_topics else "（候选池为空）"

    # Cooldown context
    cooldowns = []
    for t in active_topics[:5]:
        days = get_topic_cooldown_days(sid, t["topic"])
        cooldowns.append(f"- [{t['topic']}] 距上次发布: {days}天")
    cooldown_str = "\n".join(cooldowns) if cooldowns else "（无历史发布记录）"

    user_msg = f"""请从以下信息中选择今天发布的话题和角度：

## 历史表现（Bandit UCB 评分，越高越值得探索）
{bandit_str}

## 候选话题池
{pool_str}

## 话题冷却时间
{cooldown_str}

选择时请平衡：
- UCB高分话题（exploit，利用已验证的高表现话题）
- 长期未发布的话题（explore，探索新方向）
- 趋势热度高的新话题"""

    try:
        resp = await acall_llm(
            TOPIC_SYSTEM_PROMPT, user_msg,
            provider=runtime.llm_provider,
            temperature=0.7,
            json_mode=True,
            trace_stage="topic",
        )
        result = json.loads(resp)
        return {
            "topic": result.get("selected_topic", ""),
            "angle": result.get("selected_angle", ""),
            "source": "llm_selected",
            "reason": result.get("reason", ""),
            "confidence": result.get("confidence", 0.5),
            "alternatives": result.get("alternative_topics", []),
        }
    except Exception as e:
        logger.warning(f"Topic selection failed: {e}")
        # Fallback: pick highest UCB topic from bandit
        if bandit_topics:
            best = bandit_topics[0]
            return {
                "topic": best["topic"],
                "angle": f"深度解读：{best['topic']}",
                "source": "bandit_fallback",
                "reason": f"Fallback to highest UCB topic ({best['ucb_score']:.3f})",
                "confidence": 0.5,
            }
        return {"topic": "", "angle": "", "source": "fallback", "reason": str(e), "confidence": 0.0}


async def refresh_topic_pool(session_id: str) -> None:
    """Refresh the topic pool with trending topics (called by scheduler)."""
    import os
    from infra.llm import acall_llm
    from db._content_memory import save_topic_pool

    provider = os.getenv("LLM_PROVIDER", "deepseek")

    prompt = """你是一个公众号内容策划。请根据当前趋势和热点，推荐5个适合微信公众号发布的话题。

输出JSON数组，每个元素包含：
{
    "topic": "话题名称",
    "reason": "推荐理由（50字以内）",
    "category": "分类（科技/商业/生活/教育/文化/其他）",
    "trend_score": 0.8
}"""

    try:
        resp = await acall_llm(prompt, "请推荐今天的5个公众号话题。", provider=provider,
                               temperature=0.8, json_mode=True, trace_stage="topic_refresh")
        topics = json.loads(resp)
        if isinstance(topics, dict) and "topics" in topics:
            topics = topics["topics"]
        if isinstance(topics, list):
            save_topic_pool(session_id, topics)
            logger.info(f"Topic pool refreshed: {len(topics)} topics added")
        else:
            logger.warning(f"Unexpected topic refresh response format: {type(topics)}")
    except Exception as e:
        logger.error(f"Topic pool refresh failed: {e}")
