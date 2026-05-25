import asyncio
from unittest.mock import MagicMock, patch

import pytest

from stash.gateway.mymind import AuthError, MyMindGateway


def _make_gw_with_mock():
    """Create a gateway with a mocked client injected directly."""
    gw = MyMindGateway()
    mock_client = MagicMock()
    gw._client = mock_client
    return gw, mock_client


class TestMyMindGateway:
    @pytest.mark.asyncio
    async def test_test_connection_success(self):
        gw, mock_client = _make_gw_with_mock()
        mock_client.test_connection.return_value = True
        result = await gw.test_connection()
        assert result is True

    @pytest.mark.asyncio
    async def test_test_connection_no_tokens_raises_auth_error(self):
        gw = MyMindGateway()
        with patch("mymind_api.client._load_tokens", return_value=None):
            with pytest.raises(AuthError):
                gw._client = None
                await gw.test_connection()

    @pytest.mark.asyncio
    async def test_get_spaces(self):
        gw, mock_client = _make_gw_with_mock()
        mock_client.get_spaces.return_value = [
            {"id": "abc", "name": "Tech", "color": "#fff", "card_count": 5},
            {"id": "def", "name": "Career", "color": "#000", "card_count": 2},
        ]
        spaces = await gw.get_spaces()
        assert len(spaces) == 2
        assert spaces[0]["name"] == "Tech"
        assert spaces[1]["id"] == "def"

    @pytest.mark.asyncio
    async def test_get_tags(self):
        gw, mock_client = _make_gw_with_mock()
        mock_client.get_tags.return_value = [
            {"name": "python", "count": 10},
            {"name": "ai", "count": 5},
        ]
        tags = await gw.get_tags()
        assert tags == ["python", "ai"]

    @pytest.mark.asyncio
    async def test_save_url_existing_space(self):
        gw, mock_client = _make_gw_with_mock()
        mock_client.save_url.return_value = {"id": "card123"}
        mock_client.update_object.return_value = {}
        mock_client.get_spaces.return_value = [
            {"id": "sp1", "name": "Tech", "color": "#fff", "card_count": 3},
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client._request.return_value = mock_resp
        mock_client._headers_json.return_value = {}

        card = await gw.save_url(
            url="https://example.com",
            title="Test",
            tags=["python"],
            space="Tech",
            note="A note",
        )
        assert card.mymind_id == "card123"
        assert card.category == "Tech"
        mock_client.save_url.assert_called_once_with("https://example.com", tags=["python"])

    @pytest.mark.asyncio
    async def test_save_url_no_space(self):
        gw, mock_client = _make_gw_with_mock()
        mock_client.save_url.return_value = {"id": "card456"}
        mock_client.update_object.return_value = {}

        card = await gw.save_url(
            url="https://example.com",
            title="No Space",
            tags=[],
            space="",
            note="",
        )
        assert card.mymind_id == "card456"
        mock_client._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_space(self):
        gw, mock_client = _make_gw_with_mock()
        mock_client.create_space.return_value = {"id": "new_sp", "name": "New Space"}

        space = await gw.create_space("New Space")
        assert space["id"] == "new_sp"
        assert space["name"] == "New Space"

    @pytest.mark.asyncio
    async def test_create_tag_is_noop(self):
        gw = MyMindGateway()
        result = await gw.create_tag("test-tag")
        assert result == "test-tag"

    @pytest.mark.asyncio
    async def test_save_note(self):
        gw, mock_client = _make_gw_with_mock()
        mock_client.create_note.return_value = {"id": "note789"}
        mock_client.get_spaces.return_value = [
            {"id": "sp2", "name": "Ideas", "color": "#abc", "card_count": 1},
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client._request.return_value = mock_resp
        mock_client._headers_json.return_value = {}

        card = await gw.save_note(
            text="My idea content",
            title="My Idea",
            tags=["brainstorm"],
            space="Ideas",
        )
        assert card.mymind_id == "note789"
        assert card.category == "Ideas"
        mock_client.create_note.assert_called_once_with(
            "My idea content", title="My Idea", tags=["brainstorm"]
        )
