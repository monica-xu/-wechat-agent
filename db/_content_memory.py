"""Content memory and topic pool CRUD with bandit statistics."""

import json
import math
from datetime import datetime
from db._base import _db

# Timezone constant
TZ_NAME = "Asia/Shanghai"


def save_topic_memory(session_id: str, topic: str, embedding: list, summary: str,
                      key_points: list, writing_style: str = "", category: str = "") -> None:
    with _db() as conn:
        existing = conn.execute(
            "SELECT id, times_published, avg_read_count FROM content_memory WHERE session_id = ? AND topic = ?",
            (session_id, topic),
        ).fetchone()

        if existing:
            conn.execute("""
                UPDATE content_memory SET
                    last_published_at = datetime('now','localtime'),
                    embedding = ?, embedding_model = ?, summary = ?, key_points = ?,
                    writing_style = ?, topic_category = ?, times_published = times_published + 1,
                    updated_at = datetime('now','localtime')
                WHERE session_id = ? AND topic = ?
            """, (
                json.dumps(embedding),
                "text-embedding-3-small",
                summary,
                json.dumps(key_points, ensure_ascii=False),
                writing_style,
                category,
                session_id,
                topic,
            ))
        else:
            conn.execute("""
                INSERT INTO content_memory (session_id, topic, topic_category, times_shown,
                    times_published, avg_read_count, avg_share_rate, ucb_score,
                    last_published_at, embedding, embedding_model, summary, key_points, writing_style)
                VALUES (?, ?, ?, 1, 1, 0, 0, 0, datetime('now','localtime'), ?, ?, ?, ?, ?)
            """, (
                session_id, topic, category,
                json.dumps(embedding), "text-embedding-3-small",
                summary, json.dumps(key_points, ensure_ascii=False), writing_style,
            ))

        _update_ucb_scores(conn, session_id)


def get_topic_memory(session_id: str, topic: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM content_memory WHERE session_id = ? AND topic = ?",
            (session_id, topic),
        ).fetchone()
    return dict(row) if row else None


def list_topic_memories(session_id: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM content_memory WHERE session_id = ? ORDER BY ucb_score DESC",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_embeddings(session_id: str) -> list[dict]:
    """Return stored embeddings for similarity search."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT topic, summary, embedding FROM content_memory WHERE session_id = ? AND embedding != '[]'",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_topic_pool(session_id: str) -> None:
    """Deactivate all topics in the pool for a session (before refresh)."""
    with _db() as conn:
        conn.execute(
            "UPDATE topic_pool SET is_active = 0 WHERE session_id = ?",
            (session_id,),
        )


def save_topic_pool(session_id: str, topics: list[dict]) -> None:
    """Batch-insert candidate topics."""
    with _db() as conn:
        for t in topics:
            conn.execute("""
                INSERT OR IGNORE INTO topic_pool (session_id, topic, reason, source, category, trend_score)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                session_id,
                t.get("topic", ""),
                t.get("reason", ""),
                t.get("source", "auto"),
                t.get("category", ""),
                t.get("trend_score", 0),
            ))


def get_active_topics(session_id: str, min_score: float = 0.0) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM topic_pool
               WHERE session_id = ? AND is_active = 1 AND trend_score >= ?
               ORDER BY trend_score DESC""",
            (session_id, min_score),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_topic_used(session_id: str, topic: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE topic_pool SET is_active = 0, used_at = datetime('now','localtime') WHERE session_id = ? AND topic = ?",
            (session_id, topic),
        )


def get_topic_cooldown_days(session_id: str, topic: str) -> int:
    with _db() as conn:
        row = conn.execute(
            "SELECT last_published_at FROM content_memory WHERE session_id = ? AND topic = ?",
            (session_id, topic),
        ).fetchone()
    if not row or not row["last_published_at"]:
        return 999
    try:
        last = datetime.fromisoformat(row["last_published_at"])
        return (datetime.now().replace(tzinfo=None) - last.replace(tzinfo=None)).days
    except (ValueError, TypeError):
        return 999


def update_bandit_stats(session_id: str, topic: str, read_count: int, share_rate: float) -> None:
    """Update bandit statistics after fetching WeChat metrics."""
    with _db() as conn:
        existing = conn.execute(
            "SELECT times_published, avg_read_count FROM content_memory WHERE session_id = ? AND topic = ?",
            (session_id, topic),
        ).fetchone()
        if not existing:
            return
        n = existing["times_published"] or 1
        old_avg = existing["avg_read_count"] or 0
        new_avg = old_avg + (read_count - old_avg) / n
        conn.execute(
            "UPDATE content_memory SET avg_read_count = ?, avg_share_rate = ?, updated_at = datetime('now','localtime') WHERE session_id = ? AND topic = ?",
            (new_avg, share_rate, session_id, topic),
        )
        _update_ucb_scores(conn, session_id)


def _update_ucb_scores(conn, session_id: str) -> None:
    """Recalculate UCB scores for all topics of a session."""
    total = conn.execute(
        "SELECT COALESCE(SUM(times_published), 0) as total FROM content_memory WHERE session_id = ?",
        (session_id,),
    ).fetchone()["total"]

    rows = conn.execute(
        "SELECT id, times_published, avg_read_count FROM content_memory WHERE session_id = ?",
        (session_id,),
    ).fetchall()

    for row in rows:
        n = row["times_published"] or 1
        avg_reward = row["avg_read_count"] or 0
        # Normalize reward to 0-1 range (assuming max 10000 reads)
        normalized_reward = min(avg_reward / 1000.0, 1.0)
        exploration = math.sqrt(2 * math.log(max(total, 1) + 1) / n)
        ucb = normalized_reward + exploration
        conn.execute(
            "UPDATE content_memory SET ucb_score = ? WHERE id = ?",
            (round(ucb, 4), row["id"]),
        )


def get_bandit_topics(session_id: str, limit: int = 5) -> list[dict]:
    """Get top topics by UCB score (explore/exploit balance)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT topic, topic_category, times_published, avg_read_count, ucb_score FROM content_memory WHERE session_id = ? ORDER BY ucb_score DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
