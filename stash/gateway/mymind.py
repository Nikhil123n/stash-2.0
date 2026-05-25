from __future__ import annotations

import asyncio
import functools
import logging
import os
from datetime import datetime, timezone

import requests

from stash.models import SavedCard

logger = logging.getLogger(__name__)


class AuthError(Exception):
    pass


class MymindUnavailableError(Exception):
    pass


def _load_cookies_from_env() -> dict | None:
    """Load cookies from environment variables (Railway deployment)."""
    jwt = os.environ.get("MYMIND_JWT", "")
    cid = os.environ.get("MYMIND_CID", "")
    token = os.environ.get("MYMIND_AUTHENTICITY_TOKEN", "")
    if jwt and cid and token:
        return {"jwt": jwt, "cid": cid, "authenticity_token": token}
    return None


def _load_cookies_from_keyring() -> dict | None:
    """Load cookies from system keyring (local Windows)."""
    try:
        import keyring
        KEYRING_SERVICE = "mymind-api"
        jwt = keyring.get_password(KEYRING_SERVICE, "jwt")
        cid = keyring.get_password(KEYRING_SERVICE, "cid")
        token = keyring.get_password(KEYRING_SERVICE, "authenticity_token")
        if jwt and cid and token:
            return {"jwt": jwt, "cid": cid, "authenticity_token": token}
    except ImportError:
        pass
    return None


def _create_client_with_cookies(cookies: dict):
    """Create a MyMind client instance and inject cookies directly, bypassing keyring."""
    from mymind_api.client import MyMind

    client = object.__new__(MyMind)
    client._jwt = cookies["jwt"]
    client._cid = cookies["cid"]
    client._authenticity_token = cookies["authenticity_token"]
    return client


class MyMindGateway:
    def __init__(self) -> None:
        self._client = None
        self._spaces_cache: list[dict] | None = None
        self._auth_failed = False

    def _ensure_client(self) -> None:
        if self._client is not None:
            return

        # Priority 1: env vars (Railway)
        cookies = _load_cookies_from_env()
        if cookies:
            logger.info("  mymind auth: env vars")
            self._client = _create_client_with_cookies(cookies)
            return

        # Priority 2: keyring (local Windows)
        cookies = _load_cookies_from_keyring()
        if cookies:
            logger.info("  mymind auth: keyring")
            self._client = _create_client_with_cookies(cookies)
            return

        # Priority 3: browser login (first-time setup)
        raise AuthError(
            "No mymind cookies found. Run 'mymind login' locally, "
            "then 'python scripts/export_cookies.py' for Railway."
        )

    async def _run_sync(self, func, *args, **kwargs):
        """Run a sync mymind client call in a thread executor with timeout."""
        if self._auth_failed:
            raise AuthError("mymind cookies expired. Re-auth required.")

        loop = asyncio.get_running_loop()
        call = functools.partial(func, *args, **kwargs)
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, call),
                timeout=10.0,
            )
        except PermissionError as e:
            self._auth_failed = True
            raise AuthError(
                "mymind cookies expired. Run scripts/export_cookies.py on Windows, "
                "update Railway env vars, redeploy."
            ) from e

    async def test_connection(self) -> bool:
        try:
            self._ensure_client()
            result = await self._run_sync(self._client.test_connection)
            if not result:
                raise AuthError("mymind connection test failed")
            return True
        except (ValueError, AuthError) as e:
            raise AuthError(str(e)) from e

    async def _resolve_space_id(self, space_name: str) -> str | None:
        """Find space by name (fuzzy) or create it. Returns space ID or None if failed."""
        from difflib import SequenceMatcher

        if self._spaces_cache is None:
            self._spaces_cache = await self.get_spaces()

        proposed_norm = space_name.lower().strip()

        # Exact match first
        for s in self._spaces_cache:
            if s["name"].lower().strip() == proposed_norm:
                return s["id"]

        # Containment: "Tech" matches "Technology", "Career" matches "Career Development"
        for s in self._spaces_cache:
            existing_norm = s["name"].lower().strip()
            if proposed_norm in existing_norm or existing_norm in proposed_norm:
                logger.info("Substring matched '%s' -> '%s'", space_name, s["name"])
                return s["id"]

        # Fuzzy match (>80% similarity)
        for s in self._spaces_cache:
            ratio = SequenceMatcher(None, proposed_norm, s["name"].lower().strip()).ratio()
            if ratio > 0.80:
                logger.info("Fuzzy matched '%s' -> '%s' (%.0f%%)", space_name, s["name"], ratio * 100)
                return s["id"]

        new_space = await self.create_space(space_name)
        if not new_space.get("id"):
            logger.warning("Failed to create space '%s'", space_name)
            return None
        self._spaces_cache.append(new_space)
        return new_space["id"]

    async def post_verbatim_note(self, card_id: str, note_text: str) -> bool:
        """Post a user note to mymind's Mind Notes section. Returns True on success."""
        self._ensure_client()
        logger.debug("Posting user note to mymind Mind Notes")

        def _post():
            headers = self._client._headers()
            headers["content-type"] = "text/markdown"
            headers["origin"] = "https://access.mymind.com"
            resp = requests.post(
                f"https://access.mymind.com/objects/{card_id}/notes",
                headers=headers,
                data=note_text,
                allow_redirects=False,
            )
            if resp.status_code in (302, 401, 403):
                raise PermissionError("Auth failed posting note")
            resp.raise_for_status()
            return True

        return await self._run_sync(_post)

    async def save_url(
        self, url: str, title: str, tags: list[str], space: str, note: str
    ) -> SavedCard:
        self._ensure_client()

        result = await self._run_sync(self._client.save_url, url, tags=tags or None)
        card_id = result.get("id", "")

        if title:
            await self._run_sync(self._client.update_object, card_id, {"title": title})

        if space:
            space_id = await self._resolve_space_id(space)
            if space_id:
                await self._run_sync(
                    self._client._request, "PUT", f"/spaces/{space_id}/objects/{card_id}",
                    headers=self._client._headers_json(),
                )

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
        self._ensure_client()

        result = await self._run_sync(
            self._client.create_note, text, title=title, tags=tags or None
        )
        card_id = result.get("id", "")

        if space:
            space_id = await self._resolve_space_id(space)
            if space_id:
                await self._run_sync(
                    self._client._request, "PUT", f"/spaces/{space_id}/objects/{card_id}",
                    headers=self._client._headers_json(),
                )

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
        self._ensure_client()

        def _upload():
            headers = self._client._headers()
            files = {"file": ("image", image_bytes, mime)}
            data = {"title": title, "type": "Image"}
            resp = requests.post(
                "https://access.mymind.com/objects",
                headers=headers, files=files, data=data,
                allow_redirects=False,
            )
            if resp.status_code in (302, 401, 403):
                raise PermissionError("Auth failed during image upload")
            resp.raise_for_status()
            return resp.json()

        result = await self._run_sync(_upload)
        card_id = result.get("id", "")

        if tags:
            for tag in tags:
                await self._run_sync(self._client.add_tag, card_id, tag)

        if space:
            space_id = await self._resolve_space_id(space)
            if space_id:
                await self._run_sync(
                    self._client._request, "PUT", f"/spaces/{space_id}/objects/{card_id}",
                    headers=self._client._headers_json(),
                )

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
        self._ensure_client()
        spaces = await self._run_sync(self._client.get_spaces)
        self._spaces_cache = [{"id": s["id"], "name": s["name"]} for s in spaces]
        return self._spaces_cache

    async def get_tags(self) -> list[str]:
        self._ensure_client()
        tags = await self._run_sync(self._client.get_tags)
        return [t.get("name", t) if isinstance(t, dict) else str(t) for t in tags]

    async def create_space(self, name: str) -> dict:
        self._ensure_client()
        result = await self._run_sync(self._client.create_space, name)
        return {"id": result.get("id", ""), "name": name}

    async def create_tag(self, name: str) -> str:
        return name

    async def close(self) -> None:
        pass
