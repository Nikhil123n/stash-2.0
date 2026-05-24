from __future__ import annotations

from typing import Protocol

from stash.models import SavedCard


class MindGateway(Protocol):
    async def save_url(
        self, url: str, title: str, tags: list[str], space: str, note: str
    ) -> SavedCard: ...

    async def save_note(
        self, text: str, title: str, tags: list[str], space: str
    ) -> SavedCard: ...

    async def save_image(
        self, image_bytes: bytes, mime: str, title: str, tags: list[str], space: str, note: str
    ) -> SavedCard: ...

    async def get_spaces(self) -> list[dict]: ...

    async def get_tags(self) -> list[str]: ...

    async def create_space(self, name: str) -> dict: ...

    async def create_tag(self, name: str) -> str: ...
