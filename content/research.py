"""Research Agent — gather materials and organize into structured notes."""

import json
import logging
from infra.llm import acall_llm
from agent.prompts import RESEARCH_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


async def run_research(state, runtime) -> dict:
    """Gather and organize research materials. Returns {notes, sources}."""
    user_msg = f"""话题：{state.selected_topic}
切入角度：{state.selected_angle}

请整理相关的研究笔记。你可以：
1. 基于你的知识库提供事实和数据
2. 标注信息的可信度
3. 提供不同的观点视角
4. 推荐可用于文章中的案例或类比

注意：如果搜索工具可用，请优先使用搜索获取最新信息。"""

    try:
        resp = await acall_llm(
            RESEARCH_SYSTEM_PROMPT, user_msg,
            provider=runtime.llm_provider,
            temperature=0.4,
            max_tokens=3000,
            trace_stage="research",
        )
        # Research output is free-form text, not JSON
        return {
            "notes": resp,
            "sources": _extract_sources(resp),
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
