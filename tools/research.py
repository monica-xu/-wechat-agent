"""Research tools — trending topics, news search via LLM knowledge base.

Uses the LLM's broad training knowledge as a built-in search engine.
For production with real-time web search, swap in NewsAPI / Bing / SerpAPI
by replacing the LLM call with an HTTP request to the search provider.
"""

import json
import logging
from infra.llm import acall_llm

logger = logging.getLogger(__name__)

SEARCH_TRENDING_PROMPT = """你是一个热点趋势分析工具。请列出当前最受关注的话题。

要求：
1. 每个话题包含名称和热度分数（0-1）
2. 话题应覆盖科技、商业、社会、文化等多个领域
3. 优先返回近期的热点事件和趋势

严格输出JSON数组：
[
  {"topic": "话题名称", "score": 0.95, "category": "科技"},
  ...
]"""

SEARCH_NEWS_PROMPT = """你是一个新闻搜索工具。请搜索与指定话题相关的新闻和信息。

对每条新闻提供：
- 标题
- 来源（如已知）
- 关键事实（1-2句话）
- 可信度（high/medium/low）

严格输出JSON数组：
[
  {"title": "新闻标题", "source": "来源名称", "key_fact": "关键事实", "credibility": "high"},
  ...
]"""


async def search_trending(keyword: str = "", count: int = 5) -> list:
    """Get trending topics using LLM knowledge base.

    Falls back gracefully: returns empty list on failure so callers
    can continue with cached/static topic pools.
    """
    user_msg = f"请列出当前最热门的{count}个话题"
    if keyword:
        user_msg += f'，重点关注与"{keyword}"相关的'

    try:
        resp = await acall_llm(
            SEARCH_TRENDING_PROMPT, user_msg,
            provider="deepseek",
            temperature=0.7,
            max_tokens=1024,
            json_mode=True,
            trace_stage="tool_search_trending",
        )
        results = json.loads(resp)
        if isinstance(results, dict):
            results = results.get("topics", results.get("items", []))
        if isinstance(results, list):
            return results[:count]
        logger.warning(f"Unexpected search_trending response format: {type(results)}")
        return []
    except Exception as e:
        logger.warning(f"search_trending failed (non-fatal): {e}")
        return []


async def search_news(topic: str = "", freshness: str = "week") -> list:
    """Search for news articles using LLM knowledge base.

    Falls back gracefully: returns empty list on failure.
    """
    if not topic:
        return []

    time_hint = {"day": "最近24小时", "week": "最近一周", "month": "最近一个月"}.get(
        freshness, "最近一周"
    )

    user_msg = f'请搜索关于"{topic}"的新闻（{time_hint}），返回5条相关新闻。'

    try:
        resp = await acall_llm(
            SEARCH_NEWS_PROMPT, user_msg,
            provider="deepseek",
            temperature=0.5,
            max_tokens=1536,
            json_mode=True,
            trace_stage="tool_search_news",
        )
        results = json.loads(resp)
        if isinstance(results, dict):
            results = results.get("news", results.get("articles", results.get("items", [])))
        if isinstance(results, list):
            return results[:5]
        logger.warning(f"Unexpected search_news response format: {type(results)}")
        return []
    except Exception as e:
        logger.warning(f"search_news failed (non-fatal): {e}")
        return []
