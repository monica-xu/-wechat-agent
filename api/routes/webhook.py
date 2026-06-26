"""Webhook routes — placeholder for WeChat callback and custom integrations."""

import logging
from fastapi import APIRouter, Request

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/webhook/publish-status")
async def publish_status_webhook(request: Request):
    """Receive publish status callbacks.

    WeChat does not natively push publish status to a callback URL.
    This endpoint is a placeholder for:
    1. Custom polling-based status checks
    2. Third-party webhook integrations
    3. Manual status update hooks
    """
    try:
        body = await request.json()
        logger.info(f"Webhook received: {body}")
        return {"status": "received"}
    except Exception as e:
        logger.warning(f"Webhook parse error: {e}")
        return {"status": "error", "message": str(e)}
