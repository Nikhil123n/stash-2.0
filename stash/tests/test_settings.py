import json

import pytest

from stash.settings import (
    AVAILABLE_MODELS,
    MODEL_FLASH,
    MODEL_PRO,
    Settings,
    fallback_for,
)


class TestSettings:
    def test_defaults(self, tmp_path):
        s = Settings.load(str(tmp_path / "missing.json"))
        assert s.agent_model == MODEL_FLASH

    def test_set_by_alias(self, tmp_path):
        path = str(tmp_path / "s.json")
        s = Settings.load(path)
        changed, msg = s.set_agent_model("pro")
        assert changed
        assert s.agent_model == MODEL_PRO
        assert "Pro" in msg

        with open(path) as f:
            data = json.load(f)
        assert data["agent_model"] == MODEL_PRO

    def test_set_unchanged_when_same(self, tmp_path):
        s = Settings.load(str(tmp_path / "s.json"))
        changed, msg = s.set_agent_model("flash")
        assert changed is False
        assert "Already" in msg

    def test_set_unknown(self, tmp_path):
        s = Settings.load(str(tmp_path / "s.json"))
        changed, _ = s.set_agent_model("turbo")
        assert changed is False

    def test_set_empty(self, tmp_path):
        s = Settings.load(str(tmp_path / "s.json"))
        changed, _ = s.set_agent_model("")
        assert changed is False

    def test_persistence_round_trip(self, tmp_path):
        path = str(tmp_path / "s.json")
        Settings.load(path).set_agent_model("pro")
        s2 = Settings.load(path)
        assert s2.agent_model == MODEL_PRO

    def test_load_ignores_invalid_model(self, tmp_path):
        path = str(tmp_path / "s.json")
        with open(path, "w") as f:
            json.dump({"agent_model": "made-up-model"}, f)
        s = Settings.load(path)
        assert s.agent_model == MODEL_FLASH

    def test_describe_contains_current_and_fallback(self, tmp_path):
        s = Settings.load(str(tmp_path / "s.json"))
        out = s.describe()
        assert "Flash" in out
        assert "Pro" in out

    def test_fallback_is_other(self):
        assert fallback_for(MODEL_FLASH) == MODEL_PRO
        assert fallback_for(MODEL_PRO) == MODEL_FLASH

    def test_available_models(self):
        assert MODEL_FLASH in AVAILABLE_MODELS
        assert MODEL_PRO in AVAILABLE_MODELS
