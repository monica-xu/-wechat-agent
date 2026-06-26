"""Publish log CRUD."""

import json
from db._base import _db


def save_publish_log(log_entry: dict) -> int:
    """Insert a publish log entry. Returns row id."""
    with _db() as conn:
        cursor = conn.execute("""
            INSERT INTO publish_log (article_id, session_id, macro_stages, generate_trace,
                review_trace, total_duration_ms, llm_call_count, rewrite_count,
                critic_overall_score, final_stage, failure_reason, publish_mode,
                wechat_draft_id, wechat_publish_id, publish_status, publish_error,
                human_mode, human_approved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            log_entry.get("article_id", ""),
            log_entry.get("session_id", ""),
            json.dumps(log_entry.get("macro_stages", []), ensure_ascii=False),
            json.dumps(log_entry.get("generate_trace", {}), ensure_ascii=False),
            json.dumps(log_entry.get("review_trace", {}), ensure_ascii=False),
            log_entry.get("total_duration_ms", 0),
            log_entry.get("llm_call_count", 0),
            log_entry.get("rewrite_count", 0),
            log_entry.get("critic_overall_score", 0),
            log_entry.get("final_stage", ""),
            log_entry.get("failure_reason", ""),
            log_entry.get("publish_mode", ""),
            log_entry.get("wechat_draft_id", ""),
            log_entry.get("wechat_publish_id", ""),
            log_entry.get("publish_status", ""),
            log_entry.get("publish_error", ""),
            log_entry.get("human_mode", "auto"),
            log_entry.get("human_approved_at", ""),
        ))
    return cursor.lastrowid


def get_publish_log(article_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM publish_log WHERE article_id = ?", (article_id,)
        ).fetchone()
    return dict(row) if row else None


def list_publish_logs(session_id: str = "", limit: int = 20) -> list[dict]:
    with _db() as conn:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM publish_log WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM publish_log ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]
