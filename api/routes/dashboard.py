"""Dashboard support routes — aggregated config + pipeline trace viewer."""

import os
import json
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)

DEFAULT_SESSION_ID = os.getenv("DEFAULT_SESSION_ID", "default")
VALID_MODES = ("dry-run", "semi-auto", "auto")
VALID_PROVIDERS = ("deepseek", "openai", "claude")


# ============================================================
# Aggregated Config
# ============================================================

class ConfigPayload(BaseModel):
    mode: str | None = None
    provider: str | None = None
    threshold: float | None = None
    time_weight: float | None = None
    content_weight: float | None = None
    risk_weight: float | None = None
    topic_focus: str | None = None


@router.get("/config")
async def get_config(session_id: str = ""):
    """Return full config snapshot in a single call."""
    from db._config import RuntimeConfig
    sid = session_id or DEFAULT_SESSION_ID

    mode = await RuntimeConfig.get_human_mode(sid)
    provider = await RuntimeConfig.get_llm_provider(sid)
    threshold = await RuntimeConfig.get_publish_threshold(sid)
    weights = await RuntimeConfig.get_scoring_weights(sid)
    publish_cron = RuntimeConfig.get("publish_schedule_cron", "0 9 * * 1-5")
    topic_cron = RuntimeConfig.get("topic_refresh_cron", "0 8 * * 1-5")

    topic_focus = RuntimeConfig.get("topic_focus", "")

    return {
        "mode": mode,
        "provider": provider,
        "schedule": {"publish_cron": publish_cron, "topic_cron": topic_cron},
        "scoring": {"threshold": threshold, "weights": weights},
        "topic_focus": topic_focus,
    }


@router.put("/config")
async def set_config(payload: ConfigPayload):
    """Commit config changes in a single call. All fields optional."""
    from db._config import RuntimeConfig as RC

    changed = []

    if payload.mode is not None:
        if payload.mode not in VALID_MODES:
            raise HTTPException(status_code=400, detail=f"Invalid mode. Valid: {VALID_MODES}")
        RC.set("human_mode", payload.mode)
        changed.append("mode")

    if payload.provider is not None:
        if payload.provider not in VALID_PROVIDERS:
            raise HTTPException(status_code=400, detail=f"Invalid provider. Valid: {VALID_PROVIDERS}")
        RC.set("llm_provider", payload.provider)
        changed.append("provider")

    if payload.threshold is not None:
        if not 0.3 <= payload.threshold <= 0.95:
            raise HTTPException(status_code=400, detail="Threshold must be between 0.3 and 0.95")
        RC.set("publish_threshold", str(payload.threshold))
        changed.append("threshold")

    # Weights: normalize if any weight changed
    weight_fields = [payload.time_weight, payload.content_weight, payload.risk_weight]
    if any(w is not None for w in weight_fields):
        current = await RC.get_scoring_weights(DEFAULT_SESSION_ID)
        tw = payload.time_weight if payload.time_weight is not None else current["time"]
        cw = payload.content_weight if payload.content_weight is not None else current["content"]
        rw = payload.risk_weight if payload.risk_weight is not None else current["risk"]
        # Normalize to sum=1.0
        total = tw + cw + rw
        if total <= 0:
            raise HTTPException(status_code=400, detail="Weights must sum to a positive value")
        tw, cw, rw = tw / total, cw / total, rw / total
        RC.set("scoring_weight_time", str(round(tw, 4)))
        RC.set("scoring_weight_content", str(round(cw, 4)))
        RC.set("scoring_weight_risk", str(round(rw, 4)))
        changed.append("weights")

    if payload.topic_focus is not None:
        RC.set("topic_focus", payload.topic_focus)
        changed.append("topic_focus")

    # Return the canonical state after commit
    return await get_config()


# ============================================================
# Pipeline Trace Viewer
# ============================================================

@router.get("/pipeline/trace/{article_id}")
async def get_pipeline_trace(article_id: str):
    """Return unified pipeline trace for a single article run.

    Merges data from publish_log (structured trace) and
    decision_trace.jsonl (DECIDE/FEEDBACK detail).
    """
    from db._publish_log import get_publish_log

    # 1. Fetch from publish_log
    log = get_publish_log(article_id)
    if not log:
        raise HTTPException(status_code=404, detail=f"No trace found for article {article_id}")

    # 2. Build stage nodes
    macro_stages = _parse_json(log.get("macro_stages", "[]"))
    generate_trace = _parse_json(log.get("generate_trace", "{}"))
    review_trace = _parse_json(log.get("review_trace", "{}"))

    stages = []

    # DECIDE stage (from decision_trace.jsonl if available)
    decide_detail = _load_decide_from_jsonl(article_id)
    stages.append({
        "stage": "decide",
        "completed": "decide" in macro_stages,
        "detail": decide_detail or _build_decide_summary(log),
    })

    # GENERATE stage
    stages.append({
        "stage": "generate",
        "completed": "generate" in macro_stages,
        "sub_stages": {
            "persona": generate_trace.get("persona", {}),
            "topic": _clean_trace_dict(generate_trace.get("topic", {})),
            "research": _clean_trace_dict(generate_trace.get("research", {})),
            "writer": _clean_trace_dict(generate_trace.get("writer", {})),
        },
        "rewrite_count": log.get("rewrite_count", 0),
    })

    # REVIEW stage
    stages.append({
        "stage": "review",
        "completed": "review" in macro_stages,
        "sub_stages": {
            "primary": _clean_trace_dict(review_trace.get("primary", {})),
            "compliance": _clean_trace_dict(review_trace.get("compliance", {})),
            "adversarial": _clean_trace_dict(review_trace.get("adversarial", {})),
            "integrity": _clean_trace_dict(review_trace.get("integrity", {})),
        },
        "critic_overall_score": log.get("critic_overall_score", 0),
        "rewrite_count": log.get("rewrite_count", 0),
    })

    # PUBLISH stage
    stages.append({
        "stage": "publish",
        "completed": "publish" in macro_stages,
        "detail": {
            "mode": log.get("publish_mode", log.get("human_mode", "")),
            "wechat_draft_id": log.get("wechat_draft_id", ""),
            "wechat_publish_id": log.get("wechat_publish_id", ""),
            "publish_status": log.get("publish_status", ""),
            "publish_error": log.get("publish_error", ""),
        },
    })

    # FEEDBACK stage
    feedback_detail = _load_feedback_from_jsonl(article_id)
    stages.append({
        "stage": "feedback",
        "completed": "feedback" in macro_stages,
        "detail": feedback_detail if feedback_detail else "not recorded at this granularity",
    })

    return {
        "article_id": article_id,
        "session_id": log.get("session_id", ""),
        "final_stage": log.get("final_stage", ""),
        "failure_reason": log.get("failure_reason", ""),
        "human_mode": log.get("human_mode", ""),
        "llm_call_count": log.get("llm_call_count", 0),
        "created_at": log.get("created_at", ""),
        "stages": stages,
    }


# ---- Helpers ----

def _parse_json(val):
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return {}
    return val if val else {}


def _clean_trace_dict(d: dict) -> dict:
    """Remove noise fields from trace dicts for cleaner display."""
    if not d:
        return {}
    # Keep only meaningful keys; drop raw LLM responses and empty values
    skip = {"raw_response", "error_traceback"}
    return {k: v for k, v in d.items() if k not in skip and v not in (None, "", [], {})}


def _build_decide_summary(log: dict) -> dict:
    return {
        "publish_mode": log.get("publish_mode", ""),
        "failure_reason": log.get("failure_reason", ""),
        "critic_overall_score": log.get("critic_overall_score", 0),
    }


def _load_decide_from_jsonl(article_id: str) -> dict | None:
    """Extract DECIDE detail from decision_trace.jsonl."""
    try:
        trace_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "decision_trace.jsonl",
        )
        if not os.path.exists(trace_path):
            return None
        with open(trace_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("article_id") == article_id:
                        return {
                            "publish_score": entry.get("publish_score"),
                            "time_score": entry.get("time_score"),
                            "content_score": entry.get("content_score"),
                            "risk_score": entry.get("risk_score"),
                            "decide_reason": entry.get("decide_reason"),
                            "mode_override": entry.get("mode_override"),
                            "should_publish": entry.get("publish_score", 0) >= 0.5,
                        }
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.warning(f"Failed to read decision_trace for {article_id}: {e}")
    return None


def _load_feedback_from_jsonl(article_id: str) -> dict | None:
    """Extract FEEDBACK detail from decision_trace.jsonl."""
    try:
        trace_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "decision_trace.jsonl",
        )
        if not os.path.exists(trace_path):
            return None
        with open(trace_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("article_id") == article_id:
                        return {
                            "publish_error": entry.get("publish_error", ""),
                            "wechat_draft_id": entry.get("wechat_draft_id", ""),
                            "wechat_publish_id": entry.get("wechat_publish_id", ""),
                            "llm_call_count": entry.get("llm_call_count", 0),
                            "macro_stages": entry.get("macro_stages", []),
                        }
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return None
