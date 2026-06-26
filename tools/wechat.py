"""WeChat statistics tools."""

from db._articles import count_published_today, count_published_this_week


async def get_stats(session_id: str = "default") -> dict:
    """Get publish statistics."""
    return {
        "published_today": count_published_today(session_id),
        "published_this_week": count_published_this_week(session_id),
    }


async def get_draft_count(session_id: str = "default") -> dict:
    """Get draft statistics from DB."""
    from db._base import _db
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM articles WHERE session_id = ? AND status = 'draft' AND status_flag = 'active'",
            (session_id,),
        ).fetchone()
    return {"draft_count": row["cnt"] if row else 0}
