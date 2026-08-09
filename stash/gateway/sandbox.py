from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
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
        self._data.setdefault("spaces", [])
        self._data.setdefault("tags", [])
        self._data.setdefault("cards", [])
        self._data.setdefault("space_objects", {})

    async def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(self._path, "w") as f:
            await f.write(json.dumps(self._data, indent=2, default=str))

    async def initialize(self) -> None:
        await self._load()

    def _persist_card(self, card: SavedCard, *, extra: dict | None = None) -> None:
        entry = {
            "id": card.mymind_id,
            "title": card.title,
            "category": card.category,
            "tags": card.tags,
            "summary": card.summary,
            "source_url": card.source_url,
            "saved_at": card.saved_at.isoformat(),
        }
        if extra:
            entry.update(extra)
        self._data["cards"].append(entry)

    async def _ensure_space(self, name: str) -> tuple[str, bool]:
        for s in self._data["spaces"]:
            if s["name"].lower().strip() == name.lower().strip():
                return s["id"], False
        space = {"id": str(uuid.uuid4()), "name": name}
        self._data["spaces"].append(space)
        return space["id"], True

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
        self._persist_card(card)
        space_assigned = None
        if space:
            space_id, _ = await self._ensure_space(space)
            await self.assign_to_space(card.mymind_id, space_id)
            space_assigned = True
        await self._save()
        card._space_assigned = space_assigned
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
        self._persist_card(card)
        space_assigned = None
        if space:
            space_id, _ = await self._ensure_space(space)
            await self.assign_to_space(card.mymind_id, space_id)
            space_assigned = True
        await self._save()
        card._space_assigned = space_assigned
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
        self._persist_card(
            card,
            extra={"image_mime": mime, "image_size_bytes": len(image_bytes)},
        )
        space_assigned = None
        if space:
            space_id, _ = await self._ensure_space(space)
            await self.assign_to_space(card.mymind_id, space_id)
            space_assigned = True
        await self._save()
        card._space_assigned = space_assigned
        return card

    async def get_spaces(self) -> list[dict]:
        return [dict(s) for s in self._data["spaces"]]

    async def get_tags(self) -> list[str]:
        return list(self._data["tags"])

    async def create_space(self, name: str) -> dict:
        for s in self._data["spaces"]:
            if s["name"].lower().strip() == name.lower().strip():
                return dict(s)
        space = {"id": str(uuid.uuid4()), "name": name}
        self._data["spaces"].append(space)
        await self._save()
        return space

    async def create_tag(self, name: str) -> str:
        if name not in self._data["tags"]:
            self._data["tags"].append(name)
            await self._save()
        return name

    async def resolve_space(self, name: str) -> tuple[str | None, bool]:
        norm = name.lower().strip()
        for s in self._data["spaces"]:
            if s["name"].lower().strip() == norm:
                return s["id"], False
        for s in self._data["spaces"]:
            existing = s["name"].lower().strip()
            if norm in existing or existing in norm:
                return s["id"], False
        for s in self._data["spaces"]:
            ratio = SequenceMatcher(None, norm, s["name"].lower().strip()).ratio()
            if ratio > 0.80:
                return s["id"], False
        space = await self.create_space(name)
        return space["id"], True

    async def assign_to_space(self, card_id: str, space_id: str) -> bool:
        """Assign a card to exactly one space — mymind allows multi-space
        membership, Stash enforces single-space by removing the card from
        every other space first."""
        if not card_id or not space_id:
            return False
        space_objects = self._data.setdefault("space_objects", {})
        for sid, members in space_objects.items():
            if sid != space_id and card_id in members:
                members.remove(card_id)
        members = space_objects.setdefault(space_id, [])
        if card_id not in members:
            members.append(card_id)
        await self._save()
        return True

    async def get_space_cards(self, space_id: str) -> list[dict]:
        space_objects = self._data.get("space_objects", {})
        ids = set(space_objects.get(space_id, []))
        if not ids:
            return []
        cards_by_id = {c["id"]: c for c in self._data["cards"]}
        out = []
        for cid in ids:
            c = cards_by_id.get(cid)
            if c:
                out.append({
                    "id": c["id"],
                    "title": c.get("title", ""),
                    "type": c.get("type", ""),
                    "description": c.get("summary", ""),
                    "tags": c.get("tags", []),
                    "source_url": c.get("source_url"),
                })
        return out

    async def search_cards(
        self,
        query: str | None = None,
        tags: list[str] | None = None,
        card_type: str | None = None,
        domain: str | None = None,
        limit: int = 25,
    ) -> list[dict]:
        cards = list(self._data["cards"])
        cards.sort(key=lambda c: c.get("saved_at", ""), reverse=True)

        tags_lower = [t.lower() for t in tags] if tags else None
        query_lower = query.lower() if query else None
        domain_lower = domain.lower() if domain else None
        type_lower = card_type.lower() if card_type else None

        out = []
        for c in cards:
            if tags_lower:
                card_tags = [t.lower() for t in c.get("tags", [])]
                if not all(t in card_tags for t in tags_lower):
                    continue
            if domain_lower:
                src = (c.get("source_url") or "").lower()
                if domain_lower not in src:
                    continue
            if type_lower and c.get("type", "").lower() != type_lower:
                continue
            if query_lower:
                hay = " ".join([
                    str(c.get("title", "")),
                    str(c.get("summary", "")),
                ]).lower()
                if query_lower not in hay:
                    continue
            out.append({
                "id": c["id"],
                "title": c.get("title", ""),
                "type": c.get("type", ""),
                "description": c.get("summary", ""),
                "tags": c.get("tags", []),
                "source_url": c.get("source_url"),
                "created": c.get("saved_at", ""),
                "modified": c.get("saved_at", ""),
            })
            if len(out) >= limit:
                break
        return out

    async def delete_card(self, card_id: str) -> bool:
        before = len(self._data["cards"])
        self._data["cards"] = [c for c in self._data["cards"] if c.get("id") != card_id]
        space_objects = self._data.get("space_objects", {})
        for sid, members in list(space_objects.items()):
            space_objects[sid] = [m for m in members if m != card_id]
        await self._save()
        return len(self._data["cards"]) < before
