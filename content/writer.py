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

    # Detect co-writing mode: seed_text = user draft to polish
    if state.seed_text and state.seed_text.strip():
        polish_prompt = """你是微信公众号的润色编辑。以下是作者写的初稿。这不是一篇需要压缩的素材——这是一篇已经写好的文章，需要你精修。

你的任务：保持作者全部观点、叙事顺序、场景跳跃、个人风格，逐段润色。

规则：
- 保留每一个作者写到的场景和人物——不能删
- 保留所有个人判断和跨界联想（如郭柯宇类比）——这是作者的声音
- 保留原文的层叠结构：隐喻→情节→人物→感悟，不改成"总分总"
- 只做：顺语句、调节奏、润标题、让段落间过渡更自然
- 改写每段的开头，让第一句更有吸引力
- 字数不能比原文少，只能更丰富
- 禁止：压缩、删场景、加作者没说的观点、套模板

输出JSON：{title, content_markdown, estimated_read_time}"""
        full_system_prompt = persona_injection + "\n\n" + polish_prompt if persona_injection else polish_prompt
        user_msg = f"""## 原标题（可优化）
{state.draft_title or state.selected_topic}

## 作者初稿
{state.seed_text}

请润色这篇初稿，保持作者的个人风格和观点。"""
    else:
        user_msg = f"""## 话题
{state.selected_topic}

## 切入角度
{state.selected_angle}

## 研究笔记
{state.research_data}

请根据以上信息撰写一篇微信公众号文章。"""
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
