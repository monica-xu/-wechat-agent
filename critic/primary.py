"""Primary Critic — comprehensive quality evaluation (always runs)."""

import json
import logging
from infra.llm import acall_llm
from agent.prompts import CRITIC_PRIMARY_PROMPT

logger = logging.getLogger(__name__)


async def evaluate(state, runtime) -> dict:
    """Run primary critic. Returns {overall_score, pass, dimensions, strengths, weaknesses, rewrite_instructions}."""
    user_msg = f"文章标题：{state.draft_title}\n\n文章正文：\n{state.draft_content_markdown}"
    try:
        resp = await acall_llm(
            CRITIC_PRIMARY_PROMPT, user_msg,
            provider=runtime.llm_provider,
            temperature=0.3,
            json_mode=True,
            trace_stage="critic_primary",
        )
        result = json.loads(resp)
        result["pass"] = result.get("overall_score", 0) >= 0.7
        return result
    except Exception as e:
        logger.error(f"Primary critic error: {e}")
        return {
            "overall_score": 0.5, "pass": False,
            "dimensions": {}, "strengths": [], "weaknesses": [str(e)],
            "rewrite_instructions": "", "error": str(e), "fallback": True,
        }
