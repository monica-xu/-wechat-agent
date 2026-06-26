"""Utility functions: time, IDs, trace logging."""

import os
import uuid
import json
import time as _time
from datetime import datetime, timezone, timedelta

TZ_SHANGHAI = timezone(timedelta(hours=8))

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DECISION_TRACE_PATH = os.path.join(DATA_DIR, "decision_trace.jsonl")


def _now() -> str:
    """ISO8601 timestamp in Asia/Shanghai."""
    return datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _today() -> str:
    """YYYY-MM-DD in Asia/Shanghai."""
    return datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d")


def _generate_id() -> str:
    """Short UUID4 hex for article_id."""
    return uuid.uuid4().hex[:12]


def _append_trace(state, message: str) -> None:
    """Append timestamped trace entry to state.trace."""
    state.trace.append(f"[{_now()}] [{state.stage}] {message}")


def log_decision_trace(state) -> None:
    """Write one complete pipeline trace line to decision_trace.jsonl."""
    entry = {
        "article_id": state.article_id,
        "session_id": state.session_id,
        "human_mode": state.human_mode,
        "final_stage": state.stage,
        "publish_score": state.publish_score,
        "time_score": state.time_score,
        "content_score": state.content_score,
        "risk_score": state.risk_score,
        "mode_override": state.mode_override,
        "decide_reason": state.decide_reason,
        "selected_topic": state.selected_topic,
        "selected_angle": state.selected_angle,
        "draft_title": state.draft_title,
        "rewrite_count": state.rewrite_count,
        "critic_primary_score": state.critic_primary_score,
        "critic_compliance_score": state.critic_compliance_score,
        "critic_adversarial_score": state.critic_adversarial_score,
        "integrity_style_drift": state.integrity_style_drift,
        "integrity_contradiction": state.integrity_contradiction,
        "integrity_template_risk": state.integrity_template_risk,
        "wechat_draft_id": state.wechat_draft_id,
        "wechat_publish_id": state.wechat_publish_id,
        "publish_error": state.publish_error,
        "llm_call_count": state.llm_call_count,
        "macro_stages": state.macro_stages_completed,
        "generate_trace": state.generate_trace,
        "review_trace": state.review_trace,
        "created_at": state.created_at,
        "trace": state.trace,
        "timestamp": _now(),
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DECISION_TRACE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _truncate(text: str, limit: int = 5000) -> str:
    """Safe text truncation with Chinese character awareness."""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"
