"""Tests for the agent tool registry."""

import pytest

from stash.gateway.sandbox import SandboxGateway
from stash.tools import build_registry


@pytest.fixture
async def gateway(tmp_path):
    gw = SandboxGateway(str(tmp_path / "tools.json"))
    await gw.initialize()
    return gw


@pytest.fixture
async def registry(gateway):
    return build_registry(gateway)


class TestRegistryStructure:
    @pytest.mark.asyncio
    async def test_registry_contains_expected_tools(self, registry):
        names = set(registry.keys())
        expected = {
            "list_spaces", "list_cards_in_space", "search_cards",
            "recent_cards", "create_space", "save_note",
            "move_card_to_space", "delete_card",
        }
        assert expected.issubset(names)

    @pytest.mark.asyncio
    async def test_destructive_flag(self, registry):
        assert registry["delete_card"].destructive is True
        assert registry["move_card_to_space"].destructive is True
        assert registry["list_spaces"].destructive is False
        assert registry["search_cards"].destructive is False

    @pytest.mark.asyncio
    async def test_parameters_are_jsonschema(self, registry):
        for tool in registry.values():
            assert tool.parameters["type"] == "object"
            assert "properties" in tool.parameters


class TestToolHandlers:
    @pytest.mark.asyncio
    async def test_list_spaces_empty(self, registry):
        out = await registry["list_spaces"].handler()
        assert out["ok"] is True
        assert out["count"] == 0
        assert out["spaces"] == []

    @pytest.mark.asyncio
    async def test_list_spaces_with_data(self, gateway, registry):
        await gateway.create_space("Claude")
        await gateway.create_space("Tech")
        out = await registry["list_spaces"].handler()
        assert out["count"] == 2
        names = [s["name"] for s in out["spaces"]]
        assert "Claude" in names
        assert "Tech" in names

    @pytest.mark.asyncio
    async def test_create_space(self, gateway, registry):
        out = await registry["create_space"].handler(name="LinkedIn")
        assert out["ok"] is True
        assert out["created"] is True
        assert out["name"] == "LinkedIn"

        # Idempotent
        out2 = await registry["create_space"].handler(name="LinkedIn")
        assert out2["ok"] is True
        assert out2["created"] is False

    @pytest.mark.asyncio
    async def test_create_space_rejects_empty(self, registry):
        out = await registry["create_space"].handler(name="  ")
        assert out["ok"] is False

    @pytest.mark.asyncio
    async def test_list_cards_in_space_not_found(self, registry):
        out = await registry["list_cards_in_space"].handler(space_name="Ghost")
        assert out["ok"] is False
        assert "Ghost" in out["error"]
        assert out["available_spaces"] == []

    @pytest.mark.asyncio
    async def test_list_cards_in_space_with_data(self, gateway, registry):
        await gateway.save_url("https://example.com", "Hello", [], "Claude", "n")
        out = await registry["list_cards_in_space"].handler(space_name="Claude")
        assert out["ok"] is True
        assert out["space"] == "Claude"
        assert out["count"] == 1
        assert out["cards"][0]["title"] == "Hello"

    @pytest.mark.asyncio
    async def test_list_cards_in_space_fuzzy(self, gateway, registry):
        await gateway.save_url("https://example.com", "Hello", [], "Career Development", "n")
        out = await registry["list_cards_in_space"].handler(space_name="career")
        assert out["ok"] is True
        assert out["count"] == 1

    @pytest.mark.asyncio
    async def test_search_cards(self, gateway, registry):
        await gateway.save_note("python is great", "A", ["python"], "")
        await gateway.save_note("rust is great", "B", ["rust"], "")
        out = await registry["search_cards"].handler(query="python")
        assert out["ok"] is True
        assert any(c["title"] == "A" for c in out["cards"])
        assert not any(c["title"] == "B" for c in out["cards"])

    @pytest.mark.asyncio
    async def test_recent_cards(self, gateway, registry):
        await gateway.save_note("x", "Recent", [], "")
        out = await registry["recent_cards"].handler(limit=5)
        assert out["ok"] is True
        assert out["count"] == 1
        assert out["cards"][0]["title"] == "Recent"

    @pytest.mark.asyncio
    async def test_save_note(self, gateway, registry):
        out = await registry["save_note"].handler(
            text="Remember the milk", title="Groceries", space="Life", tags=["todo"]
        )
        assert out["ok"] is True
        assert out["space"] == "Life"
        assert out["title"] == "Groceries"

    @pytest.mark.asyncio
    async def test_move_card_to_space(self, gateway, registry):
        card = await gateway.save_url("https://x", "Card", [], "", "")
        out = await registry["move_card_to_space"].handler(
            card_id=card.mymind_id, space_name="Claude"
        )
        assert out["ok"] is True
        assert out["space_created"] is True

    @pytest.mark.asyncio
    async def test_delete_card(self, gateway, registry):
        card = await gateway.save_note("x", "Doomed", [], "")
        out = await registry["delete_card"].handler(card_id=card.mymind_id)
        assert out["ok"] is True
