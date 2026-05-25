from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from datetime import datetime, timezone

import httpx
import jwt

from stash.config import StashConfig
from stash.models import SavedCard

logger = logging.getLogger(__name__)

BASE_URL = "https://api.mymind.com"


class AuthError(Exception):
    pass


class RateLimitError(Exception):
    pass


class MymindUnavailableError(Exception):
    pass


def _parse_rate_limit_headers(resp: httpx.Response) -> dict:
    """Parse RateLimit headers from response."""
    info = {"cost": 0, "burst_remaining": None, "sustained_remaining": None, "retry_after": None}

    cost_header = resp.headers.get("ratelimit-cost", "")
    if cost_header:
        try:
            info["cost"] = int(cost_header)
        except ValueError:
            pass

    rl_header = resp.headers.get("ratelimit", "")
    if rl_header:
        policies = re.split(r",\s*(?=r=)", rl_header)
        for policy in policies:
            policy = policy.strip()
            r_match = re.search(r"r=(\d+)", policy)
            t_match = re.search(r"t=(\d+)", policy)
            remaining = int(r_match.group(1)) if r_match else None

            if remaining is not None:
                if "burst" in policy.lower() or "w=300" in policy:
                    info["burst_remaining"] = remaining
                elif "sustained" in policy.lower() or "w=2592000" in policy:
                    info["sustained_remaining"] = remaining

            if remaining == 0 and t_match:
                retry = int(t_match.group(1))
                if info["retry_after"] is None or retry > info["retry_after"]:
                    info["retry_after"] = retry

    return info


class MyMindGateway:
    def __init__(self, config: StashConfig) -> None:
        self._kid = config.MYMIND_KID
        self._secret = base64.b64decode(config.MYMIND_SECRET)
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)
        self._last_call: float = 0
        self._lock = asyncio.Lock()
        self._spaces_cache: list[dict] | None = None

    def _sign_request(self, method: str, path: str) -> str:
        now = int(time.time())
        payload = {
            "method": method.upper(),
            "path": path,
            "iat": now,
            "exp": now + 300,
        }
        headers = {"alg": "HS256", "kid": self._kid}
        return jwt.encode(payload, self._secret, algorithm="HS256", headers=headers)

    async def _rate_limit(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < 0.5:
                await asyncio.sleep(0.5 - elapsed)
            self._last_call = time.monotonic()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        await self._rate_limit()

        token = self._sign_request(method, path)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "stash/1.0",
        }

        resp = await self._client.request(method, path, headers=headers, **kwargs)

        rl = _parse_rate_limit_headers(resp)
        logger.debug(
            "mymind credit used: %d, burst remaining: %s, sustained remaining: %s",
            rl["cost"], rl["burst_remaining"], rl["sustained_remaining"],
        )
        if rl["sustained_remaining"] is not None and rl["sustained_remaining"] < 50:
            logger.warning("mymind credits running low: %d sustained remaining", rl["sustained_remaining"])

        if resp.status_code in (401, 403):
            raise AuthError("mymind auth failed. Check MYMIND_KID and MYMIND_SECRET in .env")

        if resp.status_code == 429:
            retry_after = rl["retry_after"] or 5
            logger.warning("mymind rate limited, sleeping %ds", retry_after)
            await asyncio.sleep(retry_after)
            token = self._sign_request(method, path)
            headers["Authorization"] = f"Bearer {token}"
            resp = await self._client.request(method, path, headers=headers, **kwargs)
            if resp.status_code == 429:
                raise RateLimitError("mymind rate limit exceeded after retry")

        if resp.status_code >= 500:
            raise MymindUnavailableError(f"mymind returned {resp.status_code}")

        resp.raise_for_status()
        return resp

    async def test_connection(self) -> bool:
        try:
            await self._request("GET", "/spaces")
            return True
        except AuthError:
            raise
        except Exception as e:
            logger.error("mymind connection test failed: %s", e)
            return False

    async def _resolve_space_id(self, space_name: str) -> str:
        """Find space by name or create it. Returns space ID."""
        if self._spaces_cache is None:
            self._spaces_cache = await self.get_spaces()

        for s in self._spaces_cache:
            if s["name"].lower() == space_name.lower():
                return s["id"]

        new_space = await self.create_space(space_name)
        self._spaces_cache.append(new_space)
        return new_space["id"]

    async def save_url(
        self, url: str, title: str, tags: list[str], space: str, note: str
    ) -> SavedCard:
        payload: dict = {"url": url, "type": "WebPage", "title": title}

        if tags:
            payload["tags"] = [{"name": t} for t in tags]

        if space:
            space_id = await self._resolve_space_id(space)
            payload["spaces"] = [{"id": space_id}]

        resp = await self._request("POST", "/objects", json=payload)
        result = resp.json()
        card_id = result.get("id", "")

        return SavedCard(
            mymind_id=card_id,
            title=title,
            category=space,
            tags=tags,
            summary=note,
            source_url=url,
            saved_at=datetime.now(timezone.utc),
        )

    async def save_note(
        self, text: str, title: str, tags: list[str], space: str
    ) -> SavedCard:
        payload: dict = {
            "title": title,
            "type": "Note",
            "content": {"type": "text/markdown", "body": text},
        }

        if tags:
            payload["tags"] = [{"name": t} for t in tags]

        if space:
            space_id = await self._resolve_space_id(space)
            payload["spaces"] = [{"id": space_id}]

        resp = await self._request("POST", "/objects", json=payload)
        result = resp.json()
        card_id = result.get("id", "")

        return SavedCard(
            mymind_id=card_id,
            title=title,
            category=space,
            tags=tags,
            summary=text[:200],
            source_url=None,
            saved_at=datetime.now(timezone.utc),
        )

    async def save_image(
        self, image_bytes: bytes, mime: str, title: str, tags: list[str], space: str, note: str
    ) -> SavedCard:
        # Image upload requires multipart — tags/spaces added as separate POST after
        token = self._sign_request("POST", "/objects")
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "stash/1.0",
        }
        files = {"file": ("image", image_bytes, mime)}
        data = {"title": title, "type": "Image"}

        await self._rate_limit()
        resp = await self._client.post("/objects", headers=headers, files=files, data=data)

        if resp.status_code in (401, 403):
            raise AuthError("mymind auth failed. Check MYMIND_KID and MYMIND_SECRET in .env")
        if resp.status_code >= 500:
            raise MymindUnavailableError(f"mymind returned {resp.status_code}")
        resp.raise_for_status()

        result = resp.json()
        card_id = result.get("id", "")

        # For images, add tags and space via PATCH since multipart can't carry JSON nested objects
        updates: dict = {}
        if tags:
            updates["tags"] = [{"name": t} for t in tags]
        if space:
            space_id = await self._resolve_space_id(space)
            updates["spaces"] = [{"id": space_id}]
        if updates:
            await self._request("PATCH", f"/objects/{card_id}", json=updates)

        return SavedCard(
            mymind_id=card_id,
            title=title,
            category=space,
            tags=tags,
            summary=note,
            source_url=None,
            saved_at=datetime.now(timezone.utc),
        )

    async def get_spaces(self) -> list[dict]:
        resp = await self._request("GET", "/spaces")
        data = resp.json()
        if isinstance(data, list):
            spaces = [{"id": s.get("id", ""), "name": s.get("name", "")} for s in data]
            self._spaces_cache = spaces
            return spaces
        return []

    async def get_tags(self) -> list[str]:
        resp = await self._request("GET", "/tags")
        data = resp.json()
        if isinstance(data, list):
            return [t.get("name", t) if isinstance(t, dict) else str(t) for t in data]
        return []

    async def create_space(self, name: str) -> dict:
        resp = await self._request("POST", "/spaces", json={"name": name, "color": "#fdf06f"})
        result = resp.json()
        return {"id": result.get("id", ""), "name": name}

    async def create_tag(self, name: str) -> str:
        return name

    async def close(self) -> None:
        await self._client.aclose()
