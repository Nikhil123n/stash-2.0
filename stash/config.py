from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ValidationError, field_validator


class ConfigError(Exception):
    pass


class StashConfig(BaseModel):
    DISCORD_TOKEN: str
    STASH_OWNER_ID: int
    STASH_ENV: str
    MYMIND_KID: str
    MYMIND_SECRET: str
    GOOGLE_APPLICATION_CREDENTIALS: str
    GROQ_API_KEY: str
    GCP_PROJECT_ID: str = ""
    GCP_LOCATION: str = "us-central1"
    SANDBOX_FILE: str = "./sandbox_data.json"
    TMP_DIR: str = "/tmp/stash"

    @field_validator("STASH_ENV")
    @classmethod
    def validate_env(cls, v: str) -> str:
        if v not in ("sandbox", "production"):
            raise ValueError(f"STASH_ENV must be 'sandbox' or 'production', got '{v}'")
        return v

    @field_validator("STASH_OWNER_ID", mode="before")
    @classmethod
    def parse_owner_id(cls, v: str | int) -> int:
        return int(v)

    @property
    def ENV(self) -> str:
        return self.STASH_ENV

    @property
    def OWNER_ID(self) -> int:
        return self.STASH_OWNER_ID


def load_config() -> StashConfig:
    from dotenv import load_dotenv

    load_dotenv(override=True)

    required_keys = [
        "DISCORD_TOKEN",
        "STASH_OWNER_ID",
        "STASH_ENV",
        "MYMIND_KID",
        "MYMIND_SECRET",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GROQ_API_KEY",
    ]

    missing = [k for k in required_keys if not os.environ.get(k)]
    if missing:
        raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

    env_data = {k: os.environ[k] for k in required_keys}
    env_data["SANDBOX_FILE"] = os.environ.get("SANDBOX_FILE", "./sandbox_data.json")
    env_data["TMP_DIR"] = os.environ.get("TMP_DIR", "/tmp/stash")
    env_data["GCP_PROJECT_ID"] = os.environ.get("GCP_PROJECT_ID", "")
    env_data["GCP_LOCATION"] = os.environ.get("GCP_LOCATION", "us-central1")

    try:
        return StashConfig(**env_data)
    except ValidationError as e:
        raise ConfigError(f"Invalid configuration: {e}") from e
