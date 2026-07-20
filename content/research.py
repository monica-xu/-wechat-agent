"""Research Agent — gather materials and organize into structured notes."""

import json
import logging
from infra.llm import acall_llm
from agent.prompts import RESEARCH_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


async def run_research(state, runtime) -> dict:
    """Gather and organize research materials. Returns {notes, sources}.

    If Google Search is configured, pre-searches the web for the topic and
    injects real-time results into the research prompt.
    """
    from datetime import datetime
    from tools.research import search_web

    today = datetime.now().strftime("%Y年%m月%d日")

    # Pre-search the web if Google API is configured
    web_context = ""
    try:
        search_results = await search_web(f"{state.selected_topic} {state.selected_angle}".strip(), count=5)
        if search_results:
            web_context = "\n\n## 实时网络搜索结果\n"
            for i, r in enumerate(search_results):
                web_context += f"{i+1}. **{r['title']}**\n   {r['snippet']}\n   来源：{r['url']}\n\n"
            web_context += "请优先使用以上实时搜索结果中的事实和数据。如果搜索结果不充分，再补充你的知识库信息。\n"
    except Exception as e:
        logger.warning(f"Web pre-search failed: {e}")

    user_msg = f"""当前日期：{today}

话题：{state.selected_topic}
切入角度：{state.selected_angle}
{web_context}
请整理相关的研究笔记。你需要：
1. 优先使用上面的实时搜索结果（如果有的话），提取其中的事实和数据
2. 标注信息的可信度
3. 提供不同的观点视角
4. 推荐可用于文章中的案例或类比

注意：所有时间和日期信息必须以当前日期（{today}）为基准推算，不要使用过时的时间表述。"""

    try:
        resp = await acall_llm(
            RESEARCH_SYSTEM_PROMPT, user_msg,
            provider=runtime.llm_provider,
            temperature=0.4,
            max_tokens=3000,
            trace_stage="research",
        )
        return {
            "notes": resp,
            "sources": _extract_sources(resp),
            "web_searched": bool(search_results),
            "status": "success",
        }
    except Exception as e:
        logger.warning(f"Research failed: {e}")
        return {
            "notes": f"话题：{state.selected_topic}\n角度：{state.selected_angle}\n（研究数据不可用，基于话题直接写作）",
            "sources": [],
            "status": "fallback",
            "error": str(e),
        }


def _extract_sources(text: str) -> list[str]:
    """Extract source references from research notes."""
    sources = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("来源") or line.startswith("参考") or "http" in line:
            sources.append(line)
    return sources[:10]
