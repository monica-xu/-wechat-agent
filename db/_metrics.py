"""WeChat article metrics CRUD for feedback loop."""

from db._base import _db


def save_metrics(article_id: str, read_count: int = 0, share_count: int = 0,
                 like_count: int = 0, completion_rate: float = 0,
                 title_click_rate: float = 0) -> None:
    with _db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO article_metrics (article_id, read_count, share_count,
                like_count, completion_rate, title_click_rate, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))
        """, (article_id, read_count, share_count, like_count, completion_rate, title_click_rate))


def get_metrics(article_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM article_metrics WHERE article_id = ?", (article_id,)
        ).fetchone()
    return dict(row) if row else None


def get_unfetched_articles(session_id: str, limit: int = 10) -> list[dict]:
    """Return published articles that haven't had metrics fetched yet."""
    with _db() as conn:
        rows = conn.execute("""
            SELECT a.id, a.topic, a.published_at
            FROM articles a
            LEFT JOIN article_metrics m ON a.id = m.article_id
            WHERE a.session_id = ? AND a.status = 'published' AND a.status_flag = 'active'
              AND (m.article_id IS NULL OR m.feedback_applied = 0)
            ORDER BY a.published_at DESC LIMIT ?
        """, (session_id, limit)).fetchall()
    return [dict(r) for r in rows]


def mark_feedback_applied(article_id: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE article_metrics SET feedback_applied = 1 WHERE article_id = ?",
            (article_id,),
        )
