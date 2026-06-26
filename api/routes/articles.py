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

    # Publish to WeChat
    try:
        client = WeChatClient()
        draft_id = article.get("wechat_draft_id", "")

        if not draft_id:
            # Create draft first
            draft_id = await client.create_draft(
                title=article.get("title", ""),
                content=article.get("content", ""),
                digest=article.get("summary", ""),
            )

        publish_id = await client.publish_draft(draft_id)

        # Update article status
        article["status"] = "published"
        article["wechat_draft_id"] = draft_id
        article["wechat_publish_id"] = publish_id
        save_article(article)

        return {"status": "published", "draft_id": draft_id, "publish_id": publish_id}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Publish failed: {e}")


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
