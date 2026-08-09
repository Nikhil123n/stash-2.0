"""Tests for the gateway methods added for the agent layer:
resolve_space, assign_to_space, get_space_cards, search_cards, delete_card.
"""

from unittest.mock import MagicMock

import pytest

from stash.gateway.mymind import MyMindGateway
from stash.gateway.sandbox import SandboxGateway


@pytest.fixture
async def sandbox(tmp_path):
    gw = SandboxGateway(str(tmp_path / "agent.json"))
    await gw.initialize()
    return gw


class TestSandboxAgentMethods:
    @pytest.mark.asyncio
    async def test_resolve_creates_missing(self, sandbox):
        space_id, created = await sandbox.resolve_space("Claude")
        assert space_id
        assert created is True
        spaces = await sandbox.get_spaces()
        assert any(s["name"] == "Claude" for s in spaces)

    @pytest.mark.asyncio
    async def test_resolve_existing_no_create(self, sandbox):
        await sandbox.create_space("Tech")
        space_id, created = await sandbox.resolve_space("tech")
        assert created is False
        spaces = await sandbox.get_spaces()
        assert len(spaces) == 1
        assert spaces[0]["id"] == space_id

    @pytest.mark.asyncio
    async def test_resolve_fuzzy_match(self, sandbox):
        await sandbox.create_space("Career Development")
        space_id, created = await sandbox.resolve_space("Career")
        assert created is False
        assert space_id

    @pytest.mark.asyncio
    async def test_assign_and_list_cards_in_space(self, sandbox):
        card = await sandbox.save_url(
            "https://example.com", "Hello", ["x"], "Claude", "summary"
        )
        # Saving with a space auto-creates and auto-assigns
        assert getattr(card, "_space_assigned", None) is True
        spaces = await sandbox.get_spaces()
        space_id = next(s["id"] for s in spaces if s["name"] == "Claude")
        listed = await sandbox.get_space_cards(space_id)
        assert len(listed) == 1
        assert listed[0]["title"] == "Hello"

    @pytest.mark.asyncio
    async def test_search_cards_by_text(self, sandbox):
        await sandbox.save_note("python is great", "Note A", ["python"], "")
        await sandbox.save_note("javascript is fun", "Note B", ["js"], "")
        out = await sandbox.search_cards(query="python")
        titles = [c["title"] for c in out]
        assert "Note A" in titles
        assert "Note B" not in titles

    @pytest.mark.asyncio
    async def test_search_cards_by_tag(self, sandbox):
        await sandbox.save_note("a", "A", ["alpha"], "")
        await sandbox.save_note("b", "B", ["beta"], "")
        out = await sandbox.search_cards(tags=["alpha"])
        assert len(out) == 1
        assert out[0]["title"] == "A"

    @pytest.mark.asyncio
    async def test_delete_card(self, sandbox):
        card = await sandbox.save_note("x", "ToDelete", [], "")
        ok = await sandbox.delete_card(card.mymind_id)
        assert ok is True
        out = await sandbox.search_cards(query="ToDelete")
        assert out == []

    @pytest.mark.asyncio
    async def test_assign_to_space_moves_not_duplicates(self, sandbox):
        """A card assigned to a second space must leave the first — mymind
        allows multi-space membership, Stash enforces single-space."""
        card = await sandbox.save_note("x", "Movable", [], "Claude")
        spaces = await sandbox.get_spaces()
        claude_id = next(s["id"] for s in spaces if s["name"] == "Claude")
        tech_id, _ = await sandbox.resolve_space("Tech")

        await sandbox.assign_to_space(card.mymind_id, tech_id)

        claude_cards = await sandbox.get_space_cards(claude_id)
        tech_cards = await sandbox.get_space_cards(tech_id)
        assert claude_cards == []
        assert [c["id"] for c in tech_cards] == [card.mymind_id]


class TestMyMindGatewayAgentMethods:
    def _make(self):
        gw = MyMindGateway()
        mock = MagicMock()
        gw._client = mock
        return gw, mock

    @pytest.mark.asyncio
    async def test_resolve_space_existing(self):
        gw, mock = self._make()
        mock.get_spaces.return_value = [{"id": "sp1", "name": "Claude"}]
        sid, created = await gw.resolve_space("claude")
        assert sid == "sp1"
        assert created is False

    @pytest.mark.asyncio
    async def test_resolve_space_creates(self):
        gw, mock = self._make()
        mock.get_spaces.return_value = []
        mock.create_space.return_value = {"id": "sp_new", "name": "Claude"}
        sid, created = await gw.resolve_space("Claude")
        assert sid == "sp_new"
        assert created is True

    @pytest.mark.asyncio
    async def test_assign_to_space_success(self):
        gw, mock = self._make()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock._request.return_value = mock_resp
        mock._headers_json.return_value = {}
        ok = await gw.assign_to_space("card1", "sp1")
        assert ok is True
        mock._request.assert_called_once()

    @pytest.mark.asyncio
    async def test_assign_to_space_failure_returns_false(self):
        gw, mock = self._make()
        mock._headers_json.return_value = {}
        mock._request.side_effect = RuntimeError("nope")
        ok = await gw.assign_to_space("card1", "sp1")
        assert ok is False

    @pytest.mark.asyncio
    async def test_assign_to_space_removes_from_other_spaces(self):
        """mymind allows a card in multiple spaces; Stash must not."""
        gw, mock = self._make()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock._request.return_value = mock_resp
        mock._headers_json.return_value = {}
        mock.get_object.return_value = {
            "spaces": [{"id": "sp_old_1"}, {"id": "sp1"}, {"id": "sp_old_2"}],
        }
        ok = await gw.assign_to_space("card1", "sp1")
        assert ok is True

        methods_and_urls = [
            (call.args[0], call.args[1]) for call in mock._request.call_args_list
        ]
        assert ("PUT", "/spaces/sp1/objects/card1") in methods_and_urls
        assert ("DELETE", "/spaces/sp_old_1/objects/card1") in methods_and_urls
        assert ("DELETE", "/spaces/sp_old_2/objects/card1") in methods_and_urls
        assert ("DELETE", "/spaces/sp1/objects/card1") not in methods_and_urls

    @pytest.mark.asyncio
    async def test_save_url_sets_assignment_flag(self):
        gw, mock = self._make()
        mock.save_url.return_value = {"id": "card1"}
        mock.update_object.return_value = {}
        mock.get_spaces.return_value = [{"id": "sp1", "name": "Claude"}]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock._request.return_value = mock_resp
        mock._headers_json.return_value = {}
        card = await gw.save_url("https://x.com", "T", [], "Claude", "n")
        assert getattr(card, "_space_assigned", None) is True
        assert card.category == "Claude"

    @pytest.mark.asyncio
    async def test_save_url_assignment_failure_reports_false(self):
        gw, mock = self._make()
        mock.save_url.return_value = {"id": "card1"}
        mock.update_object.return_value = {}
        mock.get_spaces.return_value = [{"id": "sp1", "name": "Claude"}]
        mock._headers_json.return_value = {}
        mock._request.side_effect = RuntimeError("fail")
        card = await gw.save_url("https://x.com", "T", [], "Claude", "n")
        assert getattr(card, "_space_assigned", None) is False

    @pytest.mark.asyncio
    async def test_get_space_cards_passthrough(self):
        gw, mock = self._make()
        mock.get_space_cards.return_value = [
            {"id": "c1", "title": "Hello", "type": "WebPage",
             "description": "d", "tags": [], "source_url": "https://x",
             "created": "", "modified": ""}
        ]
        out = await gw.get_space_cards("sp1")
        assert len(out) == 1
        assert out[0]["title"] == "Hello"

    @pytest.mark.asyncio
    async def test_search_cards_with_query_uses_server_search(self):
        gw, mock = self._make()
        mock.search.return_value = {"matches": [{"id": "c1"}, {"id": "c2"}]}

        class FakeCard:
            def __init__(self, slug, title):
                self.slug = slug
                self.title = title
                self.card_type = ""
                self.description = ""
                self.tags = []
                self.source_url = ""

        mock.get_all_cards.return_value = [
            FakeCard("c1", "Match A"),
            FakeCard("cX", "Other"),
            FakeCard("c2", "Match B"),
        ]
        out = await gw.search_cards(query="hello")
        titles = [c["title"] for c in out]
        assert titles == ["Match A", "Match B"]

    @pytest.mark.asyncio
    async def test_search_cards_with_tags_uses_filter(self):
        gw, mock = self._make()

        class FakeCard:
            def __init__(self, slug, title, tags):
                self.slug = slug
                self.title = title
                self.card_type = ""
                self.description = ""
                self.tags = tags
                self.source_url = ""

        mock.filter_cards.return_value = [FakeCard("c1", "A", ["python"])]
        out = await gw.search_cards(tags=["python"])
        assert len(out) == 1
        mock.filter_cards.assert_called_once()
