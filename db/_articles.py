"""Article CRUD operations."""

import json
import hashlib
from db._base import _db


def save_article(state_dict: dict) -> str:
    """Insert or update an article. Returns article_id."""
    with _db() as conn:
        existing = conn.execute(
            "SELECT id FROM articles WHERE id = ?", (state_dict.get("article_id", ""),)
        ).fetchone()

        if existing:
            fields = [
                "title", "content", "content_markdown", "summary", "tags", "topic", "angle",
                "status", "human_mode", "critic_overall_score", "critic_dimension_scores",
                "critic_adversarial_score", "integrity_style_drift", "integrity_contradiction",
                "integrity_template_risk", "embedding", "embedding_model", "content_hash",
                "wechat_draft_id", "wechat_publish_id", "published_at",
            ]
            sets = [f"{f} = ?" for f in fields]
            values = [_safe_get(state_dict, f) for f in fields]
            values.append(state_dict["article_id"])
            conn.execute(
                f"UPDATE articles SET {', '.join(sets)}, updated_at = datetime('now','localtime') WHERE id = ?",
                values,
            )
        else:
            conn.execute("""
                INSERT INTO articles (id, session_id, title, content, content_markdown, summary,
                    tags, topic, angle, status, human_mode, critic_overall_score,
                    critic_dimension_scores, critic_adversarial_score, integrity_style_drift,
                    integrity_contradiction, integrity_template_risk, embedding,
                    embedding_model, content_hash, wechat_draft_id, wechat_publish_id, published_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                state_dict["article_id"],
                state_dict.get("session_id", ""),
                state_dict.get("title", ""),
                state_dict.get("content", ""),
                state_dict.get("content_markdown", ""),
                state_dict.get("summary", ""),
                json.dumps(state_dict.get("tags", []), ensure_ascii=False),
                state_dict.get("topic", ""),
                state_dict.get("angle", ""),
                state_dict.get("status", "draft"),
                state_dict.get("human_mode", "auto"),
                state_dict.get("critic_overall_score", 0),
                json.dumps(state_dict.get("critic_dimension_scores", {}), ensure_ascii=False),
                state_dict.get("critic_adversarial_score", 0),
                state_dict.get("integrity_style_drift", 0),
                state_dict.get("integrity_contradiction", 0),
                state_dict.get("integrity_template_risk", "low"),
                json.dumps(state_dict.get("embedding", []), ensure_ascii=False),
                state_dict.get("embedding_model", ""),
                state_dict.get("content_hash", ""),
                state_dict.get("wechat_draft_id", ""),
                state_dict.get("wechat_publish_id", ""),
                state_dict.get("published_at", ""),
            ))

    return state_dict["article_id"]


def get_article(article_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM articles WHERE id = ? AND status_flag = 'active'", (article_id,)
        ).fetchone()
    return dict(row) if row else None


def list_articles(session_id: str = "", status: str = "", limit: int = 20, offset: int = 0) -> list[dict]:
    with _db() as conn:
        clauses = ["status_flag = 'active'"]
        params = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        params.extend([limit, offset])
        rows = conn.execute(
            f"SELECT * FROM articles WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_recently_published(session_id: str, limit: int = 10) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            """SELECT id, title, topic, summary, embedding, published_at
               FROM articles
               WHERE session_id = ? AND status = 'published' AND status_flag = 'active'
               ORDER BY published_at DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_embeddings(session_id: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, topic, summary, embedding FROM articles WHERE session_id = ? AND embedding != '[]' AND status_flag = 'active'",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_published_today(session_id: str) -> int:
    with _db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM articles
               WHERE session_id = ? AND status = 'published'
               AND date(published_at) = date('now','localtime')""",
            (session_id,),
        ).fetchone()
    return row["cnt"] if row else 0


def count_published_this_week(session_id: str) -> int:
    with _db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM articles
               WHERE session_id = ? AND status = 'published'
               AND published_at >= date('now','localtime','-6 days')""",
            (session_id,),
        ).fetchone()
    return row["cnt"] if row else 0


def soft_delete_article(article_id: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE articles SET status_flag = 'deleted', deleted_at = datetime('now','localtime') WHERE id = ?",
            (article_id,),
        )


def _safe_get(d: dict, key: str, default=""):
    val = d.get(key, default)
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    return val
