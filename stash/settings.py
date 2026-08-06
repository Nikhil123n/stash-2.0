"""Runtime preferences (persisted to disk).

Stores the user's chosen AI model and any future togglable settings.
Read once at startup, written whenever a /model command flips the value.
Safe to lose — defaults are sensible.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)


MODEL_FLASH = "llama-3.3-70b-versatile"  # Groq
MODEL_PRO = "openai/gpt-oss-20b:free"  # OpenRouter

MODEL_ALIASES: dict[str, str] = {
    "flash": MODEL_FLASH,
    "fast": MODEL_FLASH,
    "default": MODEL_FLASH,
    "llama-3.3-70b-versatile": MODEL_FLASH,
    "pro": MODEL_PRO,
    "accurate": MODEL_PRO,
    "smart": MODEL_PRO,
    "openai/gpt-oss-20b:free": MODEL_PRO,
}

AVAILABLE_MODELS = [MODEL_FLASH, MODEL_PRO]

MODEL_LABELS: dict[str, str] = {
    MODEL_FLASH: "Flash — Llama 3.3 70B via Groq (fast, default)",
    MODEL_PRO: "Pro — GPT-OSS 20B via OpenRouter (alternate provider)",
}


def fallback_for(model: str) -> str:
    """The other model — used when the chosen one times out / errors."""
    return MODEL_PRO if model == MODEL_FLASH else MODEL_FLASH


@dataclass
class Settings:
    agent_model: str = MODEL_FLASH
    _path: str = ""

    @classmethod
    def load(cls, path: str) -> "Settings":
        s = cls()
        s._path = path
        if not path or not os.path.exists(path):
            return s
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                model = data.get("agent_model")
                if model in AVAILABLE_MODELS:
                    s.agent_model = model
        except Exception as e:
            logger.warning("Could not read settings at %s: %s", path, e)
        return s

    def save(self) -> None:
        if not self._path:
            return
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            payload = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
            with open(self._path, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.warning("Could not save settings to %s: %s", self._path, e)

    def set_agent_model(self, alias_or_name: str) -> tuple[bool, str]:
        """Resolve an alias and persist. Returns (changed, message)."""
        key = (alias_or_name or "").strip().lower()
        if not key:
            return False, "Specify a model: flash | pro"
        if key not in MODEL_ALIASES:
            return False, f"Unknown model '{alias_or_name}'. Try: flash, pro"
        resolved = MODEL_ALIASES[key]
        if resolved == self.agent_model:
            return False, f"Already on {MODEL_LABELS[resolved]}."
        self.agent_model = resolved
        self.save()
        return True, f"Switched to {MODEL_LABELS[resolved]}."

    def describe(self) -> str:
        primary_label = MODEL_LABELS.get(self.agent_model, self.agent_model)
        fb = fallback_for(self.agent_model)
        fb_label = MODEL_LABELS.get(fb, fb)
        return (
            f"**Current model:** {primary_label}\n"
            f"**Fallback:** {fb_label}\n"
            f"Switch with `/model flash` or `/model pro`."
        )
