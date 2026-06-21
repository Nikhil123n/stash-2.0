"""Tests for the bonus tools: library_stats, cards_this_week, random_card."""

from datetime import datetime, timedelta, timezone

import pytest

from stash.gateway.sandbox import SandboxGateway
from stash.tools import _parse_dt, build_registry


@pytest.fixture
async def gateway(tmp_path):
    gw = SandboxGateway(str(tmp_path / "extra.json"))
    await gw.initialize()
    return gw


@pytest.fixture
async def registry(gateway):
    return build_registry(gateway)


class TestParseDt:
    def test_iso_with_z(self):
        dt = _parse_dt("2026-01-01T12:00:00Z")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_iso_with_offset(self):
        dt = _parse_dt("2026-01-01T12:00:00+00:00")
        assert dt is not None

    def test_naive_iso_assumed_utc(self):
        dt = _parse_dt("2026-01-01T12:00:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_empty_returns_none(self):
        assert _parse_dt("") is None
        assert _parse_dt(None) is None

    def test_garbage_returns_none(self):
        assert _parse_dt("not-a-date") is None


class TestLibraryStats:
    @pytest.mark.asyncio
    async def test_empty_library(self, registry):
        out = await registry["library_stats"].handler()
        assert out["ok"] is True
        assert out["space_count"] == 0
        assert out["tag_count"] == 0
        assert out["total_cards_seen"] == 0
        assert out["saved_this_week"] == 0

    @pytest.mark.asyncio
    async def test_with_content(self, gateway, registry):
        await gateway.create_space("Tech")
        await gateway.create_tag("python")
        await gateway.save_url("https://x.com", "Recent", ["python"], "Tech", "n")
        out = await registry["library_stats"].handler()
        assert out["space_count"] >= 1
        assert out["tag_count"] >= 1
        assert out["total_cards_seen"] >= 1
        assert out["saved_this_week"] >= 1


class TestCardsThisWeek:
    @pytest.mark.asyncio
    async def test_filters_to_window(self, gateway, registry):
        # Save one fresh card, then manually backdate one in the JSON.
        await gateway.save_note("fresh", "Fresh", [], "")
        old_iso = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        gateway._data["cards"].append({
            "id": "old1",
            "title": "Old card",
            "tags": [],
            "summary": "",
            "source_url": None,
            "saved_at": old_iso,
        })
        await gateway._save()

        out = await registry["cards_this_week"].handler(days=7)
        titles = [c["title"] for c in out["cards"]]
        assert "Fresh" in titles
        assert "Old card" not in titles

    @pytest.mark.asyncio
    async def test_empty(self, registry):
        out = await registry["cards_this_week"].handler()
        assert out["ok"] is True
        assert out["count"] == 0


class TestRandomCard:
    @pytest.mark.asyncio
    async def test_empty(self, registry):
        out = await registry["random_card"].handler()
        assert out["ok"] is False

    @pytest.mark.asyncio
    async def test_picks_one(self, gateway, registry):
        await gateway.save_note("a", "A", [], "")
        await gateway.save_note("b", "B", [], "")
        out = await registry["random_card"].handler()
        assert out["ok"] is True
        assert out["card"]["title"] in {"A", "B"}


class TestRegistryHasBonus:
    @pytest.mark.asyncio
    async def test_bonus_registered(self, registry):
        assert "library_stats" in registry
        assert "cards_this_week" in registry
        assert "random_card" in registry
        # None are destructive — all read-only.
        assert registry["library_stats"].destructive is False
        assert registry["cards_this_week"].destructive is False
        assert registry["random_card"].destructive is False
