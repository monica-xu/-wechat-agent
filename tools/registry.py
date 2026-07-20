"""Tool registry — JSON schema definitions and keyword-based selection.

Pattern: mirrors ai-invest-agent's tool_registry.py.
Tools are grouped by category, selected via Chinese keyword word-boundary regex matching.
"""

import re

# Tool JSON Schema definitions (for LLM function-calling)
TOOLS = [
    {
        "name": "search_trending",
        "description": "搜索当前热点话题和趋势",
        "category": "research",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词"},
                "count": {"type": "integer", "description": "返回数量，默认5"},
            },
        },
    },
    {
        "name": "search_news",
        "description": "搜索与话题相关的新闻和文章",
        "category": "research",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "搜索话题"},
                "freshness": {"type": "string", "description": "时效性：day/week/month"},
            },
        },
    },
    {
        "name": "search_web",
        "description": "使用 Google 搜索网页，获取实时信息、数据、事实和最新资料。适合查找具体数据、价格、日期、事件等需要准确信息的场景。",
        "category": "research",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "count": {"type": "integer", "description": "返回数量，默认5"},
            },
        },
    },
    {
        "name": "find_similar_articles",
        "description": "查询与给定文本相似的历史文章",
        "category": "memory",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要比较的文本"},
                "threshold": {"type": "number", "description": "相似度阈值，默认0.85"},
            },
        },
    },
    {
        "name": "get_topic_cooldown",
        "description": "查询某个话题距上次发布的天数",
        "category": "memory",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "话题名称"},
            },
        },
    },
    {
        "name": "get_publish_stats",
        "description": "获取发布统计数据（今日/本周发布数）",
        "category": "wechat",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_draft_stats",
        "description": "获取微信草稿箱统计",
        "category": "wechat",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
]

# Category definitions
TOOL_CATEGORIES = {
    "research": ["search_trending", "search_news", "search_web"],
    "memory": ["find_similar_articles", "get_topic_cooldown"],
    "wechat": ["get_publish_stats", "get_draft_stats"],
}

# Chinese keyword → category mapping (word-boundary regex)
CATEGORY_HINTS = [
    (r"(话题|选题|主题|角度|热点|趋势|热门)", ["research", "memory"]),
    (r"(研究|搜索|资料|素材|信息|新闻|数据|来源)", ["research"]),
    (r"(历史|已发|之前|重复|相似|发过|写过|冷却)", ["memory"]),
    (r"(发布|草稿|统计|数量|频率|次数|微信)", ["wechat"]),
]

# Default tool set (always available)
DEFAULT_CATEGORIES = ["research", "memory"]


def select_tools(query: str) -> list[dict]:
    """Select relevant tools based on keyword matching in query.

    Pattern from ai-invest-agent: word-boundary regex to prevent accidental activation.
    """
    matched_categories = set()

    for pattern, categories in CATEGORY_HINTS:
        if re.search(pattern, query):
            matched_categories.update(categories)

    if not matched_categories:
        matched_categories = set(DEFAULT_CATEGORIES)

    # Collect tools from matched categories
    selected_names = set()
    for cat in matched_categories:
        if cat in TOOL_CATEGORIES:
            selected_names.update(TOOL_CATEGORIES[cat])

    return [t for t in TOOLS if t["name"] in selected_names]


def get_tool_map() -> dict:
    """Return mapping of tool name → handler function."""
    return {
        "search_trending": _tool_search_trending,
        "search_news": _tool_search_news,
        "search_web": _tool_search_web,
        "find_similar_articles": _tool_find_similar,
        "get_topic_cooldown": _tool_topic_cooldown,
        "get_publish_stats": _tool_publish_stats,
        "get_draft_stats": _tool_draft_stats,
    }


# ---- Tool handler stubs (expanded in individual tool files) ----

async def _tool_search_trending(keyword: str = "", count: int = 5) -> list:
    from tools.research import search_trending
    return await search_trending(keyword, count)


async def _tool_search_news(topic: str = "", freshness: str = "week") -> list:
    from tools.research import search_news
    return await search_news(topic, freshness)


async def _tool_find_similar(text: str = "", threshold: float = 0.85) -> list:
    from tools.memory import find_similar_articles
    return await find_similar_articles(text, threshold)


async def _tool_topic_cooldown(topic: str = "") -> dict:
    from tools.memory import get_cooldown
    return await get_cooldown(topic)


async def _tool_publish_stats() -> dict:
    from tools.wechat import get_stats
    return await get_stats()


async def _tool_draft_stats() -> dict:
    from tools.wechat import get_draft_count
    return await get_draft_count()


async def _tool_search_web(query: str = "", count: int = 5) -> list:
    from tools.research import search_web
    return await search_web(query, count)
