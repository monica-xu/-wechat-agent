"""Writer Agent — draft generation and rewrite."""

import json
import logging
from infra.llm import acall_llm
from agent.prompts import WRITER_SYSTEM_PROMPT, REWRITE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


async def run_writer(state, runtime) -> dict:
    """Generate initial draft. Returns {title, content_markdown, estimated_read_time}."""
    from content.persona import get_persona_prompt_injection

    persona_injection = get_persona_prompt_injection(state.persona_config) if state.persona_config else ""

    user_msg = f"""## 话题
{state.selected_topic}

## 切入角度
{state.selected_angle}

## 研究笔记
{state.research_data}

请根据以上信息撰写一篇微信公众号文章。"""

    # Inject persona into system prompt
    full_system_prompt = persona_injection + "\n\n" + WRITER_SYSTEM_PROMPT if persona_injection else WRITER_SYSTEM_PROMPT

    try:
        resp = await acall_llm(
            full_system_prompt, user_msg,
            provider=runtime.llm_provider,
            temperature=0.75,
            max_tokens=4096,
            json_mode=True,
            trace_stage="writer",
        )
        result = json.loads(resp)
        _validate_draft(result)
        result["status"] = "success"
        return result
    except Exception as e:
        logger.error(f"Writer failed: {e}")
        raise


async def run_rewrite(state, runtime) -> dict:
    """Rewrite draft based on critic feedback. Returns {title, content_markdown}."""
    # Use .replace() instead of .format() to avoid curly-brace collisions
    # with JSON in critic feedback.
    prompt = REWRITE_SYSTEM_PROMPT
    prompt = prompt.replace("{critic_feedback}", state.critic_feedback)
    prompt = prompt.replace("{rewrite_instructions}", state.critic_rewrite_instructions)

    user_msg = f"""## 原文章标题
{state.draft_title}

## 原文章内容
{state.draft_content_markdown}

## 话题
{state.selected_topic}

请根据评审反馈修改文章。"""

    try:
        resp = await acall_llm(
            prompt, user_msg,
            provider=runtime.llm_provider,
            temperature=0.7,
            max_tokens=4096,
            json_mode=True,
            trace_stage="writer_rewrite",
        )
        result = json.loads(resp)
        _validate_draft(result)
        result["status"] = "success"
        return result
    except Exception as e:
        logger.error(f"Rewrite failed: {e}")
        raise


def _validate_draft(draft: dict) -> None:
    """Validate draft meets minimum requirements."""
    title = draft.get("title", "")
    content = draft.get("content_markdown", "")

    if not title:
        raise ValueError("Draft has no title")
    if len(content) < 300:
        raise ValueError(f"Content too short: {len(content)} chars (min 300)")
    if len(title) > 100:
        draft["title"] = title[:64]
