"""Publisher — WeChat API integration: create draft → publish."""

import logging
from infra.wechat_api import WeChatClient, WeChatAPIError, WeChatAuthError
from infra.retry import run_with_retry, RetryableError

logger = logging.getLogger(__name__)


async def run_publisher(state, runtime) -> dict:
    """Execute WeChat publishing workflow.

    auto mode: create_draft → publish_draft
    semi-auto mode: create_draft → return awaiting_approval
    dry-run mode: skipped (handled in loop.py)

    Returns {status, draft_id, publish_id, error}
    """
    client = WeChatClient()

    # Step 1: Create draft
    async def _create():
        try:
            draft_id = await client.create_draft(
                title=state.formatted_title or state.draft_title,
                content=state.formatted_html or state.draft_content_markdown,
                digest=state.article_summary or "",
            )
            if not draft_id:
                raise RetryableError("WeChat returned empty draft_id")
            return draft_id
        except (WeChatAPIError, WeChatAuthError) as e:
            raise RetryableError(str(e)) from e

    draft_result = await run_with_retry("publisher", _create)

    if isinstance(draft_result, dict) and draft_result.get("status") == "fallback":
        return {
            "status": "failed",
            "draft_id": "",
            "publish_id": "",
            "error": draft_result.get("error", "Draft creation failed"),
        }

    draft_id = draft_result
    logger.info(f"Draft created: {draft_id}")

    # Step 2: Publish (auto mode only)
    if state.human_mode == "semi-auto":
        return {
            "status": "awaiting_approval",
            "draft_id": draft_id,
            "publish_id": "",
            "error": "",
        }

    # auto mode: try publish (may not be available for all account types)
    try:
        publish_id = await client.publish_draft(draft_id)
        if publish_id:
            logger.info(f"Published: draft={draft_id}, publish={publish_id}")
            return {
                "status": "published",
                "draft_id": draft_id,
                "publish_id": publish_id,
                "error": "",
            }
    except (WeChatAPIError, WeChatAuthError) as e:
        logger.warning(f"Publish API unavailable (account type restriction): {e}")
        return {
            "status": "draft_created",
            "draft_id": draft_id,
            "publish_id": "",
            "error": f"草稿已创建但API发布不可用，请手动发布。原因: {e}",
        }

    return {
        "status": "draft_created",
        "draft_id": draft_id,
        "publish_id": "",
        "error": "草稿已创建，请在微信后台手动发布",
    }
