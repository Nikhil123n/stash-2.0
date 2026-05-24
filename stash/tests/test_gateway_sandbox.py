import asyncio
import json
import os

import pytest

from stash.gateway.sandbox import SandboxGateway
from stash.models import SavedCard


@pytest.fixture
async def gateway(tmp_path):
    path = str(tmp_path / "sandbox.json")
    gw = SandboxGateway(path)
    await gw.initialize()
    return gw


class TestSandboxGateway:
    @pytest.mark.asyncio
    async def test_initialize_empty(self, gateway):
        assert await gateway.get_spaces() == []
        assert await gateway.get_tags() == []

    @pytest.mark.asyncio
    async def test_create_space(self, gateway):
        space = await gateway.create_space("Career")
        assert space["name"] == "Career"
        assert "id" in space
        spaces = await gateway.get_spaces()
        assert len(spaces) == 1
        assert spaces[0]["name"] == "Career"

    @pytest.mark.asyncio
    async def test_create_tag(self, gateway):
        tag = await gateway.create_tag("resume")
        assert tag == "resume"
        tags = await gateway.get_tags()
        assert "resume" in tags

    @pytest.mark.asyncio
    async def test_create_tag_idempotent(self, gateway):
        await gateway.create_tag("python")
        await gateway.create_tag("python")
        tags = await gateway.get_tags()
        assert tags.count("python") == 1

    @pytest.mark.asyncio
    async def test_save_url(self, gateway):
        card = await gateway.save_url(
            url="https://example.com/article",
            title="Great Article",
            tags=["tech", "web"],
            space="Tech",
            note="A summary.",
        )
        assert isinstance(card, SavedCard)
        assert card.title == "Great Article"
        assert card.category == "Tech"
        assert card.tags == ["tech", "web"]
        assert card.source_url == "https://example.com/article"

    @pytest.mark.asyncio
    async def test_save_note(self, gateway):
        card = await gateway.save_note(
            text="Buy groceries tomorrow",
            title="Grocery Reminder",
            tags=["personal"],
            space="Life",
        )
        assert card.title == "Grocery Reminder"
        assert card.source_url is None
        assert card.summary == "Buy groceries tomorrow"

    @pytest.mark.asyncio
    async def test_save_image(self, gateway):
        card = await gateway.save_image(
            image_bytes=b"\x89PNG" + b"\x00" * 100,
            mime="image/png",
            title="Screenshot",
            tags=["design"],
            space="Design",
            note="UI mockup",
        )
        assert card.title == "Screenshot"
        assert card.category == "Design"

    @pytest.mark.asyncio
    async def test_persistence(self, tmp_path):
        path = str(tmp_path / "persist.json")
        gw1 = SandboxGateway(path)
        await gw1.initialize()
        await gw1.create_space("Tech")
        await gw1.create_tag("python")
        await gw1.save_note("test", "Test Note", ["python"], "Tech")

        # New instance reads persisted data
        gw2 = SandboxGateway(path)
        await gw2.initialize()
        spaces = await gw2.get_spaces()
        tags = await gw2.get_tags()
        assert len(spaces) == 1
        assert spaces[0]["name"] == "Tech"
        assert "python" in tags

    @pytest.mark.asyncio
    async def test_json_structure(self, tmp_path):
        path = str(tmp_path / "struct.json")
        gw = SandboxGateway(path)
        await gw.initialize()
        await gw.save_url("https://x.com", "X", ["tag1"], "Space1", "note")

        with open(path) as f:
            data = json.load(f)

        assert "spaces" in data
        assert "tags" in data
        assert "cards" in data
        assert len(data["cards"]) == 1
        card = data["cards"][0]
        assert card["title"] == "X"
        assert card["source_url"] == "https://x.com"
        assert "saved_at" in card
