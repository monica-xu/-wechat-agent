"""Compliance Critic — hard gate with veto power (always runs)."""

import json
import logging
from infra.llm import acall_llm
from agent.prompts import CRITIC_COMPLIANCE_PROMPT

logger = logging.getLogger(__name__)


async def evaluate(state, runtime) -> dict:
    """Run compliance check. Returns {overall_score, pass, risk_items, fix_suggestions}.
    score < 0.5 = HARD FAIL (veto power).
    """
    user_msg = f"文章标题：{state.draft_title}\n\n文章正文：\n{state.draft_content_markdown}"
    try:
        resp = await acall_llm(
            CRITIC_COMPLIANCE_PROMPT, user_msg,
            provider=runtime.llm_provider,
            temperature=0.1,
            json_mode=True,
            trace_stage="critic_compliance",
        )
        result = json.loads(resp)
        result["pass"] = result.get("overall_score", 1.0) >= 0.5
        return result
    except Exception as e:
        logger.error(f"Compliance critic error: {e}")
        # Fail-closed: assume non-compliant on error
        return {
            "overall_score": 0.0, "pass": False,
            "risk_items": [f"Compliance check unavailable: {e}"],
            "fix_suggestions": [], "error": str(e), "fallback": True,
        }
