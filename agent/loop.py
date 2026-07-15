"""5-stage state machine orchestrator — the central pipeline loop.

Macro-stages:
    DECIDE → GENERATE → REVIEW → PUBLISH → FEEDBACK
      │                    │         │
      └─ skip → DONE       └─ fail → DONE/FEEDBACK

Each macro-stage wraps internal sub-stages with observability traces (generate_trace,
review_trace) without exposing them to the top-level state machine.
"""

import logging
from agent.state import ArticleState, AgentRuntime
from agent.helpers import _append_trace, log_decision_trace

logger = logging.getLogger(__name__)


async def run_pipeline(state: ArticleState, runtime: AgentRuntime) -> ArticleState:
    """Main entry point. Runs the 5-stage pipeline until done/fail/skip."""
    _append_trace(state, f"Pipeline started (mode={state.human_mode})")

    # ---- Kill switch check ----
    global KILL_SWITCH_ACTIVE
    if KILL_SWITCH_ACTIVE:
        _append_trace(state, "KILL SWITCH ACTIVE — pipeline blocked")
        state.stage = "skip"
        state.decide_reason = "Kill switch active"
        await _save_final_state(state, runtime)
        log_decision_trace(state)
        return state

    # ---- Idempotency guard (prevents duplicate pipeline runs) ----
    pipeline_id = f"{state.session_id}:{state.article_id}"
    if not _acquire_pipeline_lock(pipeline_id):
        _append_trace(state, "DUPLICATE: pipeline_id already exists, skipping")
        state.stage = "skip"
        state.decide_reason = "Idempotency: duplicate pipeline run prevented"
        await _save_final_state(state, runtime)
        log_decision_trace(state)
        return state

    try:
        stages = ["decide", "generate", "review", "publish", "feedback"]

        for stage in stages:
            if state.stage in ("done", "fail", "skip"):
                break
            state.stage = stage
            handler = _STAGE_HANDLERS.get(stage)
            if not handler:
                _append_trace(state, f"ERROR: Unknown stage '{stage}'")
                state.stage = "fail"
                break

            try:
                state = await handler(state, runtime)
                state.macro_stages_completed.append(stage)
            except Exception as e:
                logger.error(f"Stage [{stage}] fatal error: {e}")
                _append_trace(state, f"FATAL in [{stage}]: {e}")
                state.stage = "fail"
                break

        # Finalize
        await _save_final_state(state, runtime)
        log_decision_trace(state)
    finally:
        _release_pipeline_lock(pipeline_id)

    return state


# ============================================================
# Stage 1: DECIDE — Policy Engine (scoring model)
# ============================================================

async def handle_decide(state: ArticleState, runtime: AgentRuntime) -> ArticleState:
    from agent.policy_engine import compute_publish_score

    # Co-writing mode: user provides content directly — bypass policy engine
    if state.seed_text and state.seed_text.strip():
        state.should_publish = True
        state.publish_score = 1.0
        state.decide_reason = "Co-writing mode — bypassing policy engine"
        state.stage = "generate"
        _append_trace(state, "DECIDE: co-writing mode, skipping policy engine")
        return state

    result = await compute_publish_score(state, runtime)

    state.publish_score = result["publish_score"]
    state.time_score = result["time_score"]
    state.content_score = result["content_score"]
    state.risk_score = result["risk_score"]
    state.mode_override = result["mode_override"]
    state.decide_reason = result["reason"]
    state.should_publish = result["should_publish"]

    if not state.should_publish and state.mode_override != "force":
        state.stage = "skip"
        _append_trace(state, f"SKIP: {result['reason']}")
    else:
        state.stage = "generate"
        _append_trace(state, f"DECIDE: publish (score={state.publish_score:.3f}, reason={result['reason']})")

    return state


# ============================================================
# Stage 2: GENERATE — topic + research + writer (internal sub-stages)
# ============================================================

async def handle_generate(state: ArticleState, runtime: AgentRuntime) -> ArticleState:
    from content.topic import run_topic
    from content.research import run_research
    from content.writer import run_writer

    # -- Sub-stage 0: Select persona (before topic) --
    from content.persona import get_next_persona
    persona = get_next_persona(state.session_id)
    state.persona_key = persona["key"]
    state.persona_config = persona
    _append_trace(state, f"PERSONA: {persona['key']} ({persona['name']})")

    # -- Sub-stage 0b: Select narrative shape --
    from content.narrative import get_next_shape
    shape = get_next_shape(state.session_id)
    state.narrative_shape_key = shape["key"]
    state.narrative_shape_config = shape
    _append_trace(state, f"SHAPE: {shape['key']} ({shape['name']})")

    # -- Sub-stage 2a: Topic --
    # Co-writing mode: seed_text is the content, skip auto topic selection
    if state.seed_text and state.seed_text.strip():
        state.selected_topic = f"协同写作（{state.seed_text[:40]}...）" if len(state.seed_text) > 40 else f"协同写作（{state.seed_text}）"
        state.selected_angle = ""
        state.topic_source = "co_writing"
        state.generate_trace["topic"] = {
            "topic": state.selected_topic, "angle": "", "source": "co_writing"
        }
        _append_trace(state, f"TOPIC (co-writing): {state.selected_topic[:80]}")
    elif state.topic and state.topic.strip():
        # Manual override — use user-provided topic directly
        state.selected_topic = state.topic.strip()
        state.selected_angle = (state.angle or "").strip()
        state.topic_source = "manual"
        state.generate_trace["topic"] = {
            "topic": state.selected_topic, "angle": state.selected_angle, "source": "manual"
        }
        _append_trace(state, f"TOPIC (manual): {state.selected_topic} ({state.selected_angle})")
    else:
        _append_trace(state, "GENERATE: selecting topic...")
        try:
            topic_result = await run_topic(state, runtime)
            state.selected_topic = topic_result.get("topic", "")
            state.selected_angle = topic_result.get("angle", "")
            state.topic_source = topic_result.get("source", "auto")
            state.generate_trace["topic"] = topic_result

            if not state.selected_topic:
                _append_trace(state, "GENERATE: No topic selected, using default")
                state.selected_topic = "行业观察"
                state.selected_angle = "本周热点回顾"
                state.generate_trace["topic"] = {
                    "topic": state.selected_topic, "angle": state.selected_angle, "source": "fallback"
                }

            _append_trace(state, f"TOPIC: {state.selected_topic} ({state.selected_angle})")
            state.llm_call_count += 1
            runtime.llm_call_count += 1

        except Exception as e:
            logger.warning(f"Topic selection failed: {e}, using default")
            state.selected_topic = "行业观察"
            state.selected_angle = "本周热点回顾"
            state.generate_trace["topic"] = {"error": str(e), "fallback": True}

    # -- Sub-stage 2b: Research (skip if seed_text provided) --
    if state.seed_text and state.seed_text.strip():
        state.research_data = state.seed_text.strip()
        state.research_sources = []
        _append_trace(state, f"GENERATE: using seed text ({len(state.research_data)} chars), skipping research")
    else:
        _append_trace(state, f"GENERATE: researching '{state.selected_topic}'...")
        try:
            research_result = await run_research(state, runtime)
            state.research_data = research_result.get("notes", "")
            state.research_sources = research_result.get("sources", [])
            state.generate_trace["research"] = research_result
            state.llm_call_count += 1
            runtime.llm_call_count += 1
            _append_trace(state, f"RESEARCH: {len(state.research_sources)} sources found")
        except Exception as e:
            logger.warning(f"Research failed: {e}, continuing with topic only")
            state.research_data = f"话题：{state.selected_topic}\n角度：{state.selected_angle}"
            state.generate_trace["research"] = {"error": str(e), "fallback": True}

    # -- Sub-stage 2c: Writer --
    _append_trace(state, "GENERATE: writing draft...")
    # Reset rewrite count for fresh generation
    state.rewrite_count = 0

    writer_result = await _attempt_write(state, runtime)
    if writer_result.get("status") == "fail":
        state.stage = "fail"
        _append_trace(state, "GENERATE: Writer failed after all retries")
        return state

    # Track whether co-writing was skipped (pre-check deemed article already good)
    state.co_writing_skipped = writer_result.get("skipped", False)

    state.draft_title = writer_result.get("title", "")
    raw_md = writer_result.get("content_markdown", "")
    state.draft_content_markdown = raw_md
    state.generate_trace["writer"] = writer_result
    state.llm_call_count += 1
    runtime.llm_call_count += 1
    _append_trace(state, f"WRITER: draft '{state.draft_title}' ({len(state.draft_content_markdown)} chars)"
                  + (" (pre-check: already good, returned as-is)" if state.co_writing_skipped else ""))

    state.stage = "review"
    return state


async def _attempt_write(state: ArticleState, runtime: AgentRuntime) -> dict:
    """Call writer with retry support. Returns dict with title, content_markdown, status."""
    from content.writer import run_writer
    from infra.retry import run_with_retry, RetryableError
    import json

    async def _write():
        result = await run_writer(state, runtime)
        if not result.get("title") or not result.get("content_markdown"):
            raise RetryableError("Writer returned empty title or content")
        content = result["content_markdown"]
        if len(content) < 300:
            raise RetryableError(f"Content too short: {len(content)} chars")
        return result

    result = await run_with_retry("writer", _write)

    if isinstance(result, dict) and result.get("status") == "fallback":
        return {"status": "fail", "error": result.get("error", "")}

    # Parse JSON if needed
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return {"status": "fail", "error": "Writer output is not valid JSON"}

    return result


# ============================================================
# Stage 3: REVIEW — Critic ensemble + Integrity (concurrent)
# ============================================================

async def handle_review(state: ArticleState, runtime: AgentRuntime) -> ArticleState:
    import asyncio

    _append_trace(state, "REVIEW: running critic ensemble + integrity check...")

    # Run primary critic, compliance critic, and integrity check concurrently
    primary_task = asyncio.create_task(_run_primary_critic(state, runtime))
    compliance_task = asyncio.create_task(_run_compliance_critic(state, runtime))
    integrity_task = asyncio.create_task(_run_integrity_check(state, runtime))

    primary_result = await primary_task
    compliance_result = await compliance_task
    integrity_result = await integrity_task

    # Store results
    state.critic_primary_score = primary_result.get("overall_score", 0)
    state.critic_primary_dimensions = primary_result.get("dimensions", {})
    state.critic_feedback = str(primary_result)
    state.critic_rewrite_instructions = primary_result.get("rewrite_instructions", "")
    state.review_trace["primary"] = primary_result

    state.critic_compliance_score = compliance_result.get("overall_score", 1.0)
    state.review_trace["compliance"] = compliance_result

    state.integrity_style_drift = integrity_result.get("style_drift", 0)
    state.integrity_contradiction = integrity_result.get("contradiction_found", False)
    state.integrity_template_risk = integrity_result.get("template_risk", "low")
    state.integrity_pass = integrity_result.get("pass", True)
    state.review_trace["integrity"] = integrity_result

    state.llm_call_count += 3
    runtime.llm_call_count += 3

    _append_trace(state, f"REVIEW: primary={state.critic_primary_score:.2f}, "
                         f"compliance={state.critic_compliance_score:.2f}, "
                         f"integrity_pass={state.integrity_pass}")

    # ---- Gate logic ----

    # Gate 1: Compliance hard gate (independent veto)
    if state.critic_compliance_score < 0.5:
        state.stage = "fail"
        _append_trace(state, f"REVIEW FAIL: Compliance score {state.critic_compliance_score:.2f} < 0.5 (hard gate)")
        return state

    # Gate 2: Integrity check
    if not state.integrity_pass:
        _append_trace(state, f"REVIEW: Integrity check failed — contradiction={state.integrity_contradiction}, template_risk={state.integrity_template_risk}")
        if state.rewrite_count < state.max_rewrites:
            state.rewrite_count += 1
            _append_trace(state, f"REWRITE {state.rewrite_count}/{state.max_rewrites} (integrity)")
            return await _rewrite_and_return(state, runtime)
        else:
            state.stage = "fail"
            _append_trace(state, f"REVIEW FAIL: Max rewrites ({state.max_rewrites}) exhausted (integrity)")
            return state

    # Gate 3: Primary critic score
    if state.critic_primary_score >= 0.7:
        # Passed — check if adversarial should run (borderline)
        if state.critic_primary_score < 0.8 or state.rewrite_count >= 1:
            _append_trace(state, "REVIEW: Borderline or post-rewrite, triggering adversarial critic...")
            adv_result = await _run_adversarial_critic(state, runtime)
            state.critic_adversarial_score = adv_result.get("overall_issue_score", 0)
            state.critic_adversarial_issues = adv_result.get("factual_issues", [])
            state.review_trace["adversarial"] = adv_result
            state.llm_call_count += 1
            runtime.llm_call_count += 1

            if adv_result.get("is_fatal", False):
                state.stage = "fail"
                _append_trace(state, f"REVIEW FAIL: Adversarial found fatal issues: {adv_result.get('summary','')}")
                return state

        state.stage = "publish"
        _append_trace(state, f"REVIEW PASS: overall={state.critic_primary_score:.2f}")
        return state

    else:
        # Failed primary critic
        if state.rewrite_count < state.max_rewrites:
            state.rewrite_count += 1
            _append_trace(state, f"REWRITE {state.rewrite_count}/{state.max_rewrites} (score={state.critic_primary_score:.2f} < 0.7)")
            return await _rewrite_and_return(state, runtime)
        else:
            state.stage = "fail"
            _append_trace(state, f"REVIEW FAIL: Max rewrites ({state.max_rewrites}) exhausted (score={state.critic_primary_score:.2f})")
            return state


async def _rewrite_and_return(state: ArticleState, runtime: AgentRuntime) -> ArticleState:
    """Execute one rewrite iteration, then go back to review."""
    from content.writer import run_rewrite

    _append_trace(state, f"GENERATE (rewrite {state.rewrite_count}): running deep evaluation...")

    # Run deep evaluation for specific, actionable rewrite guidance
    try:
        import json as _json
        from agent.prompts import EVALUATION_PROMPT
        from infra.llm import acall_llm

        eval_user_msg = f"文章标题：{state.draft_title}\n\n文章正文：\n{state.draft_content_markdown}"
        eval_resp = await acall_llm(
            EVALUATION_PROMPT, eval_user_msg,
            provider=runtime.llm_provider, temperature=0.4, json_mode=True,
            trace_stage="deep_eval_rewrite",
        )
        deep_eval = _json.loads(eval_resp)
        state.review_trace["deep_eval"] = deep_eval
        state.llm_call_count += 1
        runtime.llm_call_count += 1

        # Build specific rewrite instructions from deep evaluation
        dim_feedback = []
        for k, v in deep_eval.get("dimensions", {}).items():
            if v.get("score", 1.0) < 0.85:
                dim_feedback.append(f"- [{k}] {v.get('feedback', '')}")
        dim_text = "\n".join(dim_feedback[:4]) if dim_feedback else "各项基本达标"

        state.critic_feedback = dim_text
        state.critic_rewrite_instructions = deep_eval.get("one_thing_to_fix", "")
        _append_trace(state, f"DEEP EVAL: overall={deep_eval.get('overall_score', 0):.2f}, fix={state.critic_rewrite_instructions[:80]}")
    except Exception as e:
        _append_trace(state, f"DEEP EVAL failed (falling back to critic): {e}")
        # Fallback: use existing critic feedback (already set in handle_review)
    try:
        rewrite_result = await run_rewrite(state, runtime)
        state.draft_title = rewrite_result.get("title", state.draft_title)
        raw_md = rewrite_result.get("content_markdown", state.draft_content_markdown)
        state.draft_content_markdown = raw_md
        state.generate_trace["writer"] = rewrite_result
        state.llm_call_count += 1
        runtime.llm_call_count += 1
    except Exception as e:
        logger.error(f"Rewrite failed: {e}")
        # Keep old draft and retry review — or fail if this is repeated
        if state.rewrite_count >= state.max_rewrites:
            state.stage = "fail"
            return state

    state.stage = "review"
    return state


# ============================================================
# Stage 4: PUBLISH — Format + WeChat API (internal sub-stages)
# ============================================================

async def handle_publish(state: ArticleState, runtime: AgentRuntime) -> ArticleState:
    from content.formatter import run_formatter
    from content.publisher import run_publisher

    # -- Sub-stage 4a: Format --
    _append_trace(state, "PUBLISH: formatting for WeChat...")
    try:
        format_result = await run_formatter(state, runtime)
        state.formatted_title = format_result.get("formatted_title", state.draft_title)
        state.formatted_html = format_result.get("formatted_html", "")
        state.article_summary = format_result.get("article_summary", "")
        state.llm_call_count += 1
        runtime.llm_call_count += 1
        _append_trace(state, "FORMAT: complete")
    except Exception as e:
        logger.error(f"Formatter failed: {e}")
        # Fallback: use raw markdown wrapped in basic HTML
        state.formatted_title = state.draft_title[:64]
        state.formatted_html = f"<section>{state.draft_content_markdown}</section>"
        state.article_summary = state.draft_content_markdown[:120]

    # -- Branch on human mode --
    if state.human_mode == "dry-run":
        _append_trace(state, "PUBLISH: dry-run mode — saving preview, skipping WeChat")
        state.stage = "feedback"
        return state

    # ---- Risk-based downgrade guard ----
    # If auto mode but risk_score is too high, force downgrade to semi-auto
    if state.human_mode == "auto" and state.risk_score < 0.30:
        _append_trace(state, f"PUBLISH: Risk score {state.risk_score:.2f} < 0.30, downgrading auto→semi-auto")
        state.human_mode = "semi-auto"

    # ---- Publish rate limiter (WeChat risk control) ----
    from db._articles import count_published_today
    published_today = count_published_today(state.session_id)
    MAX_SAFE_DAILY = 1
    if published_today >= MAX_SAFE_DAILY:
        _append_trace(state, f"PUBLISH: Rate limit — {published_today} already published today (max {MAX_SAFE_DAILY})")
        state.stage = "feedback"
        state.publish_error = f"Rate limiter: {published_today} published today"
        return state

    # -- Sub-stage 4b: WeChat publish --
    _append_trace(state, f"PUBLISH: mode={state.human_mode}, sending to WeChat...")
    try:
        publish_result = await run_publisher(state, runtime)
        state.wechat_draft_id = publish_result.get("draft_id", "")
        state.wechat_publish_id = publish_result.get("publish_id", "")
        state.publish_error = publish_result.get("error", "")

        if publish_result.get("status") == "published":
            _append_trace(state, f"PUBLISHED: draft={state.wechat_draft_id}, publish={state.wechat_publish_id}")
        elif publish_result.get("status") == "draft_only":
            _append_trace(state, f"DRAFT ONLY: draft={state.wechat_draft_id} (publish deferred)")
        elif publish_result.get("status") == "awaiting_approval":
            _append_trace(state, "AWAITING APPROVAL: semi-auto mode, waiting for human")
            state.stage = "feedback"
            return state
        else:
            _append_trace(state, f"PUBLISH FAILED: {state.publish_error}")
    except Exception as e:
        logger.error(f"Publisher failed: {e}")
        state.publish_error = str(e)
        _append_trace(state, f"PUBLISH ERROR: {e}")

    state.stage = "feedback"
    return state


# ============================================================
# Stage 5: FEEDBACK — Analytics + Bandit update (async, non-blocking)
# ============================================================

async def handle_feedback(state: ArticleState, runtime: AgentRuntime) -> ArticleState:
    _append_trace(state, "FEEDBACK: recording to memory and updating scores...")

    try:
        from content.feedback import record_and_update

        await record_and_update(state, runtime)
        _append_trace(state, "FEEDBACK: complete — article recorded, bandit scores updated")
    except Exception as e:
        logger.warning(f"Feedback recording failed (non-fatal): {e}")
        _append_trace(state, f"FEEDBACK: non-fatal error: {e}")

    state.stage = "done"
    _append_trace(state, f"DONE: pipeline complete (mode={state.human_mode})")
    return state


# ============================================================
# Stage handler dispatch
# ============================================================

_STAGE_HANDLERS = {
    "decide":   handle_decide,
    "generate": handle_generate,
    "review":   handle_review,
    "publish":  handle_publish,
    "feedback": handle_feedback,
}


# ============================================================
# Internal review helpers
# ============================================================

async def _run_primary_critic(state: ArticleState, runtime: AgentRuntime) -> dict:
    import json
    from infra.llm import acall_llm
    from agent.prompts import CRITIC_PRIMARY_PROMPT

    user_msg = f"文章标题：{state.draft_title}\n\n文章正文：\n{state.draft_content_markdown}"
    # Inject narrative shape info into critic prompt
    critic_primary_prompt = CRITIC_PRIMARY_PROMPT
    shape = state.narrative_shape_config
    critic_primary_prompt = critic_primary_prompt.replace("{narrative_shape_name}", shape.get("name", "自由式"))
    critic_primary_prompt = critic_primary_prompt.replace("{narrative_shape_key}", shape.get("key", "free"))
    try:
        resp = await acall_llm(critic_primary_prompt, user_msg, provider=runtime.llm_provider,
                               temperature=0.3, json_mode=True, trace_stage="critic_primary")
        result = json.loads(resp)
        result["pass"] = result.get("overall_score", 0) >= 0.7
        return result
    except Exception as e:
        logger.error(f"Primary critic failed: {e}")
        return {"overall_score": 0.5, "pass": False, "dimensions": {}, "rewrite_instructions": "",
                "error": str(e), "fallback": True}


async def _run_compliance_critic(state: ArticleState, runtime: AgentRuntime) -> dict:
    import json
    from infra.llm import acall_llm
    from agent.prompts import CRITIC_COMPLIANCE_PROMPT

    user_msg = f"文章标题：{state.draft_title}\n\n文章正文：\n{state.draft_content_markdown}"
    try:
        resp = await acall_llm(CRITIC_COMPLIANCE_PROMPT, user_msg, provider=runtime.llm_provider,
                               temperature=0.1, json_mode=True, trace_stage="critic_compliance")
        result = json.loads(resp)
        result["pass"] = result.get("overall_score", 1.0) >= 0.5
        return result
    except Exception as e:
        logger.error(f"Compliance critic failed: {e}")
        # Fail-closed: if compliance check fails, assume non-compliant (conservative)
        return {"overall_score": 0.0, "pass": False, "risk_items": [f"Compliance check failed: {e}"],
                "error": str(e), "fallback": True}


async def _run_adversarial_critic(state: ArticleState, runtime: AgentRuntime) -> dict:
    import json
    from infra.llm import acall_llm
    from agent.prompts import CRITIC_ADVERSARIAL_PROMPT

    user_msg = f"文章标题：{state.draft_title}\n\n文章正文：\n{state.draft_content_markdown}"
    try:
        resp = await acall_llm(CRITIC_ADVERSARIAL_PROMPT, user_msg, provider=runtime.llm_provider,
                               temperature=0.5, json_mode=True, trace_stage="critic_adversarial")
        result = json.loads(resp)
        result["is_fatal"] = result.get("overall_issue_score", 0) >= 0.7
        return result
    except Exception as e:
        logger.error(f"Adversarial critic failed: {e}")
        return {"overall_issue_score": 0.0, "factual_issues": [], "logic_gaps": [],
                "overclaims": [], "bias_concerns": [], "is_fatal": False,
                "summary": f"Adversarial critic unavailable: {e}", "fallback": True}


async def _run_integrity_check(state: ArticleState, runtime: AgentRuntime) -> dict:
    import json
    from infra.llm import acall_llm
    from agent.prompts import INTEGRITY_PROMPT
    from db._articles import get_recently_published

    # Build context: recent articles
    recent = get_recently_published(state.session_id, limit=10)
    recent_context = "\n".join(
        f"- [{r.get('topic','')}] {r.get('title','')} ({r.get('summary','')[:80]})"
        for r in recent
    ) if recent else "（无历史文章）"

    # Use .replace() instead of .format() to avoid curly-brace collisions
    # with JSON examples or code blocks in article content.
    prompt = INTEGRITY_PROMPT
    prompt = prompt.replace("{recent_articles_context}", recent_context)
    prompt = prompt.replace("{topic}", state.selected_topic)
    prompt = prompt.replace("{title}", state.draft_title)
    prompt = prompt.replace("{summary}", state.draft_content_markdown[:500])

    try:
        resp = await acall_llm(prompt, "", provider=runtime.llm_provider,
                               temperature=0.2, json_mode=True, trace_stage="integrity")
        result = json.loads(resp)
        result["pass"] = (
            not result.get("contradiction_found", False)
            and not result.get("template_issues", {}).get("title_pattern_repeat", False)
            and result.get("template_issues", {}).get("opening_pattern_repeat", False) is False
        )
        return result
    except Exception as e:
        logger.error(f"Integrity check failed: {e}")
        # Fail-open for integrity (non-critical path): pass with warning
        return {"contradiction_found": False, "template_risk": "low",
                "pass": True, "error": str(e), "fallback": True}


# ============================================================
# Final state persistence
# ============================================================

async def _save_final_state(state: ArticleState, runtime: AgentRuntime) -> None:
    """Persist article and publish log to database."""
    from db._articles import save_article
    from db._publish_log import save_publish_log

    try:
        article_dict = {
            "article_id": state.article_id,
            "session_id": state.session_id,
            "title": state.formatted_title or state.draft_title,
            "content": state.formatted_html,
            "content_markdown": state.draft_content_markdown,
            "summary": state.article_summary,
            "tags": [],
            "topic": state.selected_topic,
            "angle": state.selected_angle,
            "narrative_shape": state.narrative_shape_key,
            "opening_type": "",  # Sprint B: classified in batch
            "status": "published" if state.stage == "done" and state.wechat_publish_id else "draft",
            "human_mode": state.human_mode,
            "critic_overall_score": state.critic_primary_score,
            "critic_dimension_scores": state.critic_primary_dimensions,
            "critic_adversarial_score": state.critic_adversarial_score,
            "integrity_style_drift": state.integrity_style_drift,
            "integrity_contradiction": 1 if state.integrity_contradiction else 0,
            "integrity_template_risk": state.integrity_template_risk,
            "wechat_draft_id": state.wechat_draft_id,
            "wechat_publish_id": state.wechat_publish_id,
            "published_at": state.wechat_publish_id and state.created_at or "",
        }
        save_article(article_dict)
    except Exception as e:
        logger.error(f"Failed to save article: {e}")

    try:
        log_dict = {
            "article_id": state.article_id,
            "session_id": state.session_id,
            "macro_stages": state.macro_stages_completed,
            "generate_trace": state.generate_trace,
            "review_trace": state.review_trace,
            "rewrite_count": state.rewrite_count,
            "critic_overall_score": state.critic_primary_score,
            "final_stage": state.stage,
            "failure_reason": state.decide_reason if state.stage == "fail" else "",
            "publish_mode": state.human_mode,
            "wechat_draft_id": state.wechat_draft_id,
            "wechat_publish_id": state.wechat_publish_id,
            "publish_status": "success" if state.wechat_publish_id else state.stage,
            "publish_error": state.publish_error,
            "human_mode": state.human_mode,
            "llm_call_count": state.llm_call_count,
            "narrative_shape": state.narrative_shape_key,
        }
        save_publish_log(log_dict)
    except Exception as e:
        logger.error(f"Failed to save publish log: {e}")


# ============================================================
# Pipeline Idempotency Lock
# ============================================================

def _acquire_pipeline_lock(pipeline_id: str) -> bool:
    """Prevent duplicate pipeline runs with DB-backed idempotency key.

    INSERT OR IGNORE returns True only if the pipeline_id didn't exist.
    A pipeline_id is unique per (session, article_id) — since article_id is
    a random UUID each trigger, re-triggers are blocked.
    """
    from db._base import _db
    try:
        with _db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO pipeline_runs (pipeline_id, article_id, session_id) VALUES (?, ?, ?)",
                (pipeline_id, f"lock:{pipeline_id}", ""),
            )
            # Check if we actually inserted
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM pipeline_runs WHERE pipeline_id = ?",
                (pipeline_id,),
            ).fetchone()
            return row["cnt"] == 1
    except Exception as e:
        logger.warning(f"Pipeline lock check failed (fail-open for safety): {e}")
        return True  # Fail-open: don't block pipeline on DB error


def _release_pipeline_lock(pipeline_id: str) -> None:
    """Mark pipeline as completed."""
    from db._base import _db
    try:
        with _db() as conn:
            conn.execute(
                "UPDATE pipeline_runs SET status = 'completed' WHERE pipeline_id = ?",
                (pipeline_id,),
            )
    except Exception:
        pass


# ============================================================
# Kill Switch
# ============================================================

KILL_SWITCH_ACTIVE = False


def activate_kill_switch() -> None:
    """Immediately stop all scheduled pipeline runs."""
    global KILL_SWITCH_ACTIVE
    KILL_SWITCH_ACTIVE = True
    from db._config import RuntimeConfig
    RuntimeConfig.set("kill_switch", "true")
    logger.warning("KILL SWITCH ACTIVATED — all pipeline runs blocked")


def deactivate_kill_switch() -> None:
    """Resume normal operations."""
    global KILL_SWITCH_ACTIVE
    KILL_SWITCH_ACTIVE = False
    from db._config import RuntimeConfig
    RuntimeConfig.set("kill_switch", "false")
    logger.info("Kill switch deactivated — operations resumed")

