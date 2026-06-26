"""Adversarial Critic — triggered on-demand (borderline scores or post-rewrite)."""

import json
import logging
from infra.llm import acall_llm
from agent.prompts import CRITIC_ADVERSARIAL_PROMPT

logger = logging.getLogger(__name__)


async def evaluate(state, runtime) -> dict:
    """Run adversarial critic. Returns {overall_issue_score, factual_issues, logic_gaps, ..., is_fatal}.
    Lower overall_issue_score = better article.
    is_fatal = True means the article has fatal problems and should be rejected.
    """
    user_msg = f"文章标题：{state.draft_title}\n\n文章正文：\n{state.draft_content_markdown}"
    try:
        resp = await acall_llm(
            CRITIC_ADVERSARIAL_PROMPT, user_msg,
            provider=runtime.llm_provider,
            temperature=0.5,
            json_mode=True,
            trace_stage="critic_adversarial",
        )
        result = json.loads(resp)
        result["is_fatal"] = result.get("overall_issue_score", 0) >= 0.7
        return result
    except Exception as e:
        logger.error(f"Adversarial critic error: {e}")
        return {
            "overall_issue_score": 0.0, "factual_issues": [], "logic_gaps": [],
            "overclaims": [], "bias_concerns": [], "is_fatal": False,
            "summary": f"Adversarial critic unavailable: {e}", "fallback": True,
        }
