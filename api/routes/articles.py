"""Article management routes — list, approve, reject."""

import os
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()
DEFAULT_SESSION_ID = os.getenv("DEFAULT_SESSION_ID", "default")


@router.get("/articles")
async def list_articles(
    session_id: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=20),
    offset: int = Query(default=0),
):
    """List articles with optional filtering."""
    from db._articles import list_articles as _list
    sid = session_id or DEFAULT_SESSION_ID
    articles = _list(sid, status=status, limit=limit, offset=offset)
    return {"articles": articles, "count": len(articles)}


@router.get("/articles/{article_id}")
async def get_article(article_id: str):
    """Get a single article by ID."""
    from db._articles import get_article as _get
    article = _get(article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"article": article}


@router.post("/articles/{article_id}/approve")
async def approve_article(article_id: str):
    """Approve a pending article for publish (semi-auto mode).

    This triggers the WeChat publish for an article that was created
    in semi-auto mode and is awaiting human approval.
    """
    from db._articles import get_article as _get, save_article
    from infra.wechat_api import WeChatClient

    article = _get(article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    if article.get("status") != "draft":
        raise HTTPException(status_code=400, detail="Article is not in draft status")

    # Create draft on WeChat
    try:
        client = WeChatClient()
        draft_id = article.get("wechat_draft_id", "")

        if not draft_id:
            # Create draft on WeChat
            draft_id = await client.create_draft(
                title=article.get("title", ""),
                content=article.get("content", ""),
                digest=article.get("summary", ""),
            )
            if draft_id:
                article["wechat_draft_id"] = draft_id
                save_article(article)
                return {
                    "status": "draft_created",
                    "draft_id": draft_id,
                    "message": "草稿已创建，请在微信公众平台后台手动发布"
                }
            else:
                raise HTTPException(status_code=500, detail="草稿创建失败，微信未返回 media_id")

        # Try publish (may fail for subscription accounts)
        try:
            publish_id = await client.publish_draft(draft_id)
            if publish_id:
                article["status"] = "published"
                article["wechat_publish_id"] = publish_id
                save_article(article)
                return {"status": "published", "draft_id": draft_id, "publish_id": publish_id}
        except Exception:
            pass

        # Publish not available — user must do it manually
        return {
            "status": "draft_created",
            "draft_id": draft_id,
            "message": "草稿已创建（API发布不可用），请在微信公众平台后台手动发布"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"操作失败: {e}")


@router.post("/articles/{article_id}/reject")
async def reject_article(article_id: str):
    """Reject a pending article (semi-auto mode)."""
    from db._articles import get_article as _get, save_article

    article = _get(article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    article["status"] = "rejected"
    save_article(article)

    return {"status": "rejected", "article_id": article_id}


@router.delete("/articles/{article_id}")
async def delete_article(article_id: str):
    """Soft-delete an article."""
    from db._articles import soft_delete_article
    soft_delete_article(article_id)
    return {"status": "deleted", "article_id": article_id}


class FeedbackRequest(BaseModel):
    status: str  # adopted | edited | rejected
    note: str = ""
    article_type: str = ""
    edit_type: str = ""
    core_opinion: str = ""
    core_conflict: str = ""
    metaphors_used: str = ""
    edited_markdown: str = ""
    read_count: int = 0
    share_count: int = 0
    like_count: int = 0
    comment_count: int = 0


@router.post("/articles/{article_id}/feedback")
async def save_feedback(article_id: str, req: FeedbackRequest):
    """Record human decision on an article."""
    if req.status not in ("adopted", "edited", "rejected"):
        raise HTTPException(status_code=400, detail="Invalid status. Use: adopted | edited | rejected")
    from db._base import _db
    with _db() as conn:
        conn.execute(
            "INSERT INTO article_feedback (article_id, status, note, article_type, edit_type, core_opinion, core_conflict, metaphors_used, edited_markdown) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (article_id, req.status, req.note, req.article_type, req.edit_type, req.core_opinion, req.core_conflict, req.metaphors_used, req.edited_markdown),
        )
        # Also save metrics if provided
        if req.read_count > 0 or req.share_count > 0 or req.like_count > 0 or req.comment_count > 0:
            conn.execute(
                "INSERT OR REPLACE INTO article_metrics (article_id, read_count, share_count, like_count, comment_count, fetched_at) VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))",
                (article_id, req.read_count, req.share_count, req.like_count, req.comment_count),
            )
    return {"status": "saved", "article_id": article_id, "feedback": req.status}


@router.get("/articles/{article_id}/feedback")
async def get_feedback(article_id: str):
    """Get feedback history for an article."""
    from db._base import _db
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM article_feedback WHERE article_id = ? ORDER BY created_at DESC",
            (article_id,),
        ).fetchall()
    history = [dict(r) for r in rows]
    latest = history[0] if history else None
    return {"article_id": article_id, "latest": latest, "history": history}


@router.get("/stats/feedback")
async def get_feedback_stats():
    """Get today's feedback summary for dashboard overview."""
    from db._base import _db
    with _db() as conn:
        today = conn.execute("SELECT date('now','localtime')").fetchone()[0]
        row = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'adopted' THEN 1 ELSE 0 END) as adopted,
                SUM(CASE WHEN status = 'edited' THEN 1 ELSE 0 END) as edited,
                SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected
            FROM article_feedback
            WHERE date(created_at) = ?
        """, (today,)).fetchone()
    total = row["total"] or 0
    adopted = row["adopted"] or 0
    edited = row["edited"] or 0
    rejected = row["rejected"] or 0
    rate = round((adopted + edited) / total * 100) if total > 0 else 0
    return {
        "today": today,
        "total": total,
        "adopted": adopted,
        "edited": edited,
        "rejected": rejected,
        "adoption_rate": rate,
    }
