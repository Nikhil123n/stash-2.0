from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiofiles

from stash.models import SavedCard


class SandboxGateway:
    def __init__(self, sandbox_file: str) -> None:
        self._path = Path(sandbox_file)
        self._data: dict = {"spaces": [], "tags": [], "cards": []}

    async def _load(self) -> None:
        if self._path.exists():
            async with aiofiles.open(self._path, "r") as f:
                self._data = json.loads(await f.read())
        else:
            self._data = {"spaces": [], "tags": [], "cards": []}

    async def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(self._path, "w") as f:
            await f.write(json.dumps(self._data, indent=2, default=str))

    async def initialize(self) -> None:
        await self._load()

    async def save_url(
        self, url: str, title: str, tags: list[str], space: str, note: str
    ) -> SavedCard:
        card = SavedCard(
            mymind_id=str(uuid.uuid4()),
            title=title,
            category=space,
            tags=tags,
            summary=note,
            source_url=url,
            saved_at=datetime.now(timezone.utc),
        )
        self._data["cards"].append({
            "id": card.mymind_id,
            "title": card.title,
            "category": card.category,
            "tags": card.tags,
            "summary": card.summary,
            "source_url": card.source_url,
            "saved_at": card.saved_at.isoformat(),
        })
        await self._save()
        return card

    async def save_note(
        self, text: str, title: str, tags: list[str], space: str
    ) -> SavedCard:
        card = SavedCard(
            mymind_id=str(uuid.uuid4()),
            title=title,
            category=space,
            tags=tags,
            summary=text,
            source_url=None,
            saved_at=datetime.now(timezone.utc),
        )
        self._data["cards"].append({
            "id": card.mymind_id,
            "title": card.title,
            "category": card.category,
            "tags": card.tags,
            "summary": card.summary,
            "source_url": None,
            "saved_at": card.saved_at.isoformat(),
        })
        await self._save()
        return card

    async def save_image(
        self, image_bytes: bytes, mime: str, title: str, tags: list[str], space: str, note: str
    ) -> SavedCard:
        card = SavedCard(
            mymind_id=str(uuid.uuid4()),
            title=title,
            category=space,
            tags=tags,
            summary=note,
            source_url=None,
            saved_at=datetime.now(timezone.utc),
        )
        self._data["cards"].append({
            "id": card.mymind_id,
            "title": card.title,
            "category": card.category,
            "tags": card.tags,
            "summary": card.summary,
            "source_url": None,
            "image_mime": mime,
            "image_size_bytes": len(image_bytes),
            "saved_at": card.saved_at.isoformat(),
        })
        await self._save()
        return card

    async def get_spaces(self) -> list[dict]:
        return list(self._data["spaces"])

    async def get_tags(self) -> list[str]:
        return list(self._data["tags"])

    async def create_space(self, name: str) -> dict:
        space = {"id": str(uuid.uuid4()), "name": name}
        self._data["spaces"].append(space)
        await self._save()
        return space

    async def create_tag(self, name: str) -> str:
        if name not in self._data["tags"]:
            self._data["tags"].append(name)
            await self._save()
        return name
