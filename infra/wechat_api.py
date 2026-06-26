"""WeChat Official Account API client with automatic token management."""

import os
import time
import json
import asyncio
import logging
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

WECHAT_API_BASE = "https://api.weixin.qq.com"
ACCESS_TOKEN_TTL = 5400  # 90 minutes (safe buffer for 2-hour expiry)


class WeChatAuthError(Exception):
    pass


class WeChatAPIError(Exception):
    def __init__(self, errcode: int, errmsg: str):
        self.errcode = errcode
        self.errmsg = errmsg
        super().__init__(f"WeChat API error [{errcode}]: {errmsg}")


class WeChatClient:
    """Official Account API client with automatic access_token refresh."""

    def __init__(self):
        self._app_id = os.getenv("WECHAT_APP_ID", "")
        self._app_secret = os.getenv("WECHAT_APP_SECRET", "")
        self._access_token = ""
        self._token_expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _ensure_token(self) -> str:
        """Get valid access_token with async lock for thread safety."""
        if time.time() < self._token_expires_at - 300:
            return self._access_token

        async with self._lock:
            # Double-check after acquiring lock
            if time.time() < self._token_expires_at - 300:
                return self._access_token

            logger.info("Refreshing WeChat access_token...")
            url = f"{WECHAT_API_BASE}/cgi-bin/token"
            params = {
                "grant_type": "client_credential",
                "appid": self._app_id,
                "secret": self._app_secret,
            }

            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params=params)
                data = resp.json()

            if "access_token" in data:
                self._access_token = data["access_token"]
                self._token_expires_at = time.time() + data.get("expires_in", 7200) - 300
                logger.info("Access token refreshed successfully")
                return self._access_token
            else:
                raise WeChatAuthError(f"Token fetch failed: {data.get('errmsg', 'unknown')}")

    async def _post(self, endpoint: str, body: dict) -> dict:
        """Make an authenticated POST request."""
        token = await self._ensure_token()
        url = f"{WECHAT_API_BASE}{endpoint}?access_token={token}"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body)
            data = resp.json()

        errcode = data.get("errcode", 0)
        if errcode not in (0,):
            # Token expired mid-request — retry once
            if errcode in (40001, 42001):
                logger.info("Token expired, retrying after refresh...")
                self._token_expires_at = 0  # force refresh
                token = await self._ensure_token()
                url = f"{WECHAT_API_BASE}{endpoint}?access_token={token}"
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(url, json=body)
                    data = resp.json()
                    errcode = data.get("errcode", 0)

            if errcode not in (0,):
                raise WeChatAPIError(errcode, data.get("errmsg", ""))

        return data

    async def _get(self, endpoint: str, params: dict | None = None) -> dict:
        """Make an authenticated GET request."""
        token = await self._ensure_token()
        url = f"{WECHAT_API_BASE}{endpoint}?access_token={token}"
        if params:
            url += "&" + urlencode(params)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            data = resp.json()

        if data.get("errcode", 0) not in (0,):
            raise WeChatAPIError(data.get("errcode", 0), data.get("errmsg", ""))
        return data

    # ---- Draft API ----

    async def create_draft(self, title: str, content: str,
                           digest: str = "", need_open_comment: int = 0,
                           only_fans_can_comment: int = 0) -> str:
        """Create a draft article. Returns media_id (draft_id)."""
        body = {
            "articles": [{
                "title": title[:64],
                "author": "",
                "digest": digest[:120] if digest else "",
                "content": content,
                "content_source_url": "",
                "thumb_media_id": "",
                "need_open_comment": need_open_comment,
                "only_fans_can_comment": only_fans_can_comment,
            }]
        }
        data = await self._post("/cgi-bin/draft/add", body)
        return data.get("media_id", "")

    async def update_draft(self, draft_id: str, title: str, content: str,
                           digest: str = "", index: int = 0) -> bool:
        """Update an existing draft."""
        body = {
            "media_id": draft_id,
            "index": index,
            "articles": {
                "title": title[:64],
                "content": content,
                "digest": digest[:120] if digest else "",
            }
        }
        await self._post("/cgi-bin/draft/update", body)
        return True

    async def delete_draft(self, draft_id: str) -> bool:
        """Delete a draft by media_id."""
        await self._post("/cgi-bin/draft/delete", {"media_id": draft_id})
        return True

    # ---- Publish API ----

    async def publish_draft(self, draft_id: str) -> str:
        """Submit a draft for publishing. Returns publish_id (task_id)."""
        data = await self._post("/cgi-bin/freepublish/submit", {"media_id": draft_id})
        return data.get("publish_id", "")

    async def get_publish_status(self, publish_id: str) -> dict:
        """Check publish task status."""
        return await self._post("/cgi-bin/freepublish/get", {"publish_id": publish_id})

    async def get_article_list(self, offset: int = 0, count: int = 20) -> list[dict]:
        """List published articles with item data."""
        data = await self._post("/cgi-bin/freepublish/batchget", {
            "offset": offset,
            "count": count,
            "no_content": 0,
        })
        return data.get("item", [])

    async def get_article_total(self) -> int:
        """Get total published article count."""
        data = await self._post("/cgi-bin/freepublish/batchget", {
            "offset": 0, "count": 1, "no_content": 1,
        })
        return data.get("total_count", 0)

    # ---- Media API ----

    async def upload_image(self, file_path: str) -> str:
        """Upload a permanent image to WeChat CDN. Returns URL for use in content."""
        token = await self._ensure_token()
        url = f"{WECHAT_API_BASE}/cgi-bin/media/uploadimg?access_token={token}"

        async with httpx.AsyncClient(timeout=60) as client:
            with open(file_path, "rb") as f:
                files = {"media": (os.path.basename(file_path), f, "image/png")}
                resp = await client.post(url, files=files)
                data = resp.json()

        if data.get("errcode", 0) not in (0,):
            raise WeChatAPIError(data.get("errcode", 0), data.get("errmsg", ""))
        return data.get("url", "")
