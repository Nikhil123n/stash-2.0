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

    async def get_space_cards(self, space_id: str) -> list[dict]: ...

    async def search_cards(
        self,
        query: str | None = None,
        tags: list[str] | None = None,
        card_type: str | None = None,
        domain: str | None = None,
        limit: int = 25,
    ) -> list[dict]: ...

    async def assign_to_space(self, card_id: str, space_id: str) -> bool: ...

    async def delete_card(self, card_id: str) -> bool: ...

    async def resolve_space(self, name: str) -> tuple[str | None, bool]:
        """Find a space by name (fuzzy) or create it. Returns (space_id, created)."""
        ...
