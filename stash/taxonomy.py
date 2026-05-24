from __future__ import annotations

from datetime import datetime, timedelta, timezone

from stash.gateway.interface import MindGateway


class TaxonomyCache:
    REFRESH_INTERVAL = timedelta(minutes=10)

    def __init__(self, gateway: MindGateway) -> None:
        self._gateway = gateway
        self.spaces: list[dict] = []
        self.tags: list[str] = []
        self.last_refreshed: datetime | None = None

    async def initialize(self) -> None:
        await self._fetch()

    async def _fetch(self) -> None:
        self.spaces = await self._gateway.get_spaces()
        self.tags = await self._gateway.get_tags()
        self.last_refreshed = datetime.now(timezone.utc)

    async def refresh_if_stale(self) -> None:
        if self.last_refreshed is None:
            await self._fetch()
            return
        elapsed = datetime.now(timezone.utc) - self.last_refreshed
        if elapsed >= self.REFRESH_INTERVAL:
            await self._fetch()

    def get_taxonomy_for_prompt(self) -> str:
        space_names = ", ".join(s["name"] for s in self.spaces) or "(none yet)"
        tag_names = ", ".join(self.tags) or "(none yet)"
        return f"SPACES (categories): {space_names}\nTAGS: {tag_names}"

    async def on_new_space_created(self, name: str) -> None:
        if not any(s["name"] == name for s in self.spaces):
            result = await self._gateway.create_space(name)
            self.spaces.append(result)

    async def on_new_tag_created(self, name: str) -> None:
        if name not in self.tags:
            await self._gateway.create_tag(name)
            self.tags.append(name)
