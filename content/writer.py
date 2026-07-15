"""Writer Agent — draft generation and rewrite."""

import json
import logging
from infra.llm import acall_llm
from agent.prompts import WRITER_SYSTEM_PROMPT, REWRITE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


async def run_writer(state, runtime) -> dict:
    """Generate initial draft. Returns {title, content_markdown, estimated_read_time}."""
    from content.persona import get_persona_prompt_injection
    from content.narrative import assemble_narrative_prompt

    persona_injection = get_persona_prompt_injection(state.persona_config) if state.persona_config else ""
    narrative_injection = assemble_narrative_prompt(state.narrative_shape_config) if state.narrative_shape_config else ""

    # Detect co-writing mode: seed_text = user draft
    if state.seed_text and state.seed_text.strip():
        seed_len = len(state.seed_text.strip())
        mode = getattr(state, 'co_writing_mode', 'polish')

        if mode == 'raw':
            # Raw mode: zero system prompt overhead, same as pasting into DeepSeek chat
            full_system_prompt = "你是资深中文编辑。"
            user_msg = f"逐段润色下面的文章，保持作者的语言风格和观点不变。只修改表达，不改变内容和结构。直接返回润色后的全文。\n\n{state.seed_text.strip()}"
            # ... skip the rest of the co-writing block, go straight to LLM call

            try:
                resp = await acall_llm(
                    full_system_prompt, user_msg,
                    provider=runtime.llm_provider,
                    temperature=0.5,
                    max_tokens=8192,
                    json_mode=False,
                    trace_stage="writer_raw_polish",
                )
                # Raw mode doesn't return JSON - wrap the output
                return {
                    "title": state.draft_title or state.selected_topic or "未命名",
                    "content_markdown": resp.strip(),
                    "estimated_read_time": max(3, len(resp) // 400),
                    "status": "success",
                }
            except Exception as e:
                logger.error(f"Raw polish failed: {e}")
                raise

        # Pre-check for polish mode: if article is already strong, don't touch it
        if mode == 'polish':
            try:
                import json as _json
                from agent.prompts import EVALUATION_PROMPT
                eval_resp = await acall_llm(
                    EVALUATION_PROMPT,
                    f"文章标题：{state.draft_title or state.selected_topic}\n\n文章正文：\n{state.seed_text.strip()}",
                    provider=runtime.llm_provider, temperature=0.4, json_mode=True,
                    trace_stage="co_writing_precheck",
                )
                pre_eval = _json.loads(eval_resp)
                pre_score = pre_eval.get("overall_score", 0)
                if pre_score >= 0.85:
                    logger.info(f"Co-writing pre-check: article already {pre_score:.2f}, returning as-is")
                    return {
                        "title": state.draft_title or state.selected_topic,
                        "content_markdown": state.seed_text.strip(),
                        "estimated_read_time": max(3, seed_len // 400),
                        "status": "success",
                        "pre_check_score": pre_score,
                        "skipped": True,
                    }
            except Exception:
                pass  # Pre-check failed — proceed with polish anyway

        if mode == 'polish':
            # Light polish: the article is already good — only fix what's clearly broken
            polish_prompt = """你是微信公众号的资深编辑。作者发来了一篇已经写好的文章。它可能已经很好了。

你的任务不是"改写"——是"仅修复明显问题"。多数句子不需要你动。你像一个有经验的同事在读稿子，偶尔说一句"这里删掉""这句换个说法更好"。

具体来说，只做这三件事：
1. 发现并删除那些作者自己都没注意到的重复——同一个意思在前后两段被说了两遍。
2. 找出那些一口气喘不过来的超长句（超过60字的中文句子），拆成两句。
3. 如果某一段的开头读起来很平，帮它找一个更有力的第一句。但只改开头那一句，段落后面的内容不动。

最重要的一条：如果一句话你已经觉得写得很好——不要碰它。好编辑知道什么时候该收手。

禁止：
- 改变作者的观点、隐喻、叙事顺序
- 添加作者没写的场景、人物、例子
- 把文章改成"总分总"模板
- 压缩或删除作者的原文段落
- 把作者精心推敲过的句子换成"更规范"但更平庸的表达

输出JSON：{title, content_markdown, estimated_read_time}"""
        else:
            # Expand: preserve and enrich throughout
            polish_prompt = f"""你是微信公众号的资深编辑。以下是作者写的初稿（{seed_len}字）。保留原文的每一段、每一个场景、每一个人物、每一个观点，在此基础上：

1. 逐段精修：
   - 优化语句的节奏和韵律
   - 改进段落之间的过渡
   - 改写每段的开头，让第一句更有吸引力
   - 但每段的核心内容和位置不变

2. 逐段扩充（目标：成文 2000-3500 字）：
   - 场景：补充感官细节（光、声音、气味、触感）
   - 观点：往深处推一层
   - 例子：增加具体的、可感知的信息
   - 隐喻：让它自然流动到更多段落中

3. 禁止：
   - 删除或压缩作者的原文内容
   - 添加作者没暗示的观点或新的人物
   - 把文章改成"总分总"的模板结构

输出JSON：{{title, content_markdown, estimated_read_time}}"""
        parts = [p for p in [persona_injection, narrative_injection, polish_prompt] if p]
        full_system_prompt = "\n\n".join(parts)
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
        # Inject narrative injection into WRITER_SYSTEM_PROMPT template
        writer_system = WRITER_SYSTEM_PROMPT.replace("{narrative_injection}", narrative_injection)
        parts = [p for p in [persona_injection, writer_system] if p]
        full_system_prompt = "\n\n".join(parts)

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
    from content.narrative import assemble_narrative_prompt

    # Include narrative context so rewrite preserves the original shape intent
    narrative_injection = assemble_narrative_prompt(state.narrative_shape_config) if state.narrative_shape_config else ""

    prompt = REWRITE_SYSTEM_PROMPT
    prompt = prompt.replace("{critic_feedback}", state.critic_feedback)
    prompt = prompt.replace("{rewrite_instructions}", state.critic_rewrite_instructions)

    if narrative_injection:
        prompt = narrative_injection + "\n\n" + prompt

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
    if len(content) < 400:
        raise ValueError(f"Content too short: {len(content)} chars (min 400)")
    if len(title) > 100:
        draft["title"] = title[:64]
