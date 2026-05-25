from __future__ import annotations

import asyncio
import functools
import logging
from datetime import datetime, timezone

import requests

from stash.models import SavedCard

logger = logging.getLogger(__name__)


class AuthError(Exception):
    pass


class MymindUnavailableError(Exception):
    pass


class MyMindGateway:
    def __init__(self) -> None:
        self._client = None
        self._spaces_cache: list[dict] | None = None

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        from mymind_api import MyMind
        self._client = MyMind()

    async def _run_sync(self, func, *args, **kwargs):
        """Run a sync mymind client call in a thread executor with timeout."""
        loop = asyncio.get_running_loop()
        call = functools.partial(func, *args, **kwargs)
        return await asyncio.wait_for(
            loop.run_in_executor(None, call),
            timeout=10.0,
        )

    async def test_connection(self) -> bool:
        try:
            self._ensure_client()
            result = await self._run_sync(self._client.test_connection)
            if not result:
                raise AuthError("mymind auth failed. Run: mymind login")
            return True
        except ValueError as e:
            raise AuthError(str(e)) from e
        except PermissionError as e:
            raise AuthError(str(e)) from e

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
        self._ensure_client()

        result = await self._run_sync(self._client.save_url, url, tags=tags or None)
        card_id = result.get("id", "")

        if title:
            await self._run_sync(self._client.update_object, card_id, {"title": title})

        if space:
            space_id = await self._resolve_space_id(space)
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
