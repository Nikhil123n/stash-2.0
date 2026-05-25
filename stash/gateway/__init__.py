from __future__ import annotations

from stash.config import ConfigError, StashConfig
from stash.gateway.interface import MindGateway
from stash.gateway.sandbox import SandboxGateway


def create_gateway(config: StashConfig) -> MindGateway:
    if config.ENV == "sandbox":
        return SandboxGateway(config.SANDBOX_FILE)
    elif config.ENV == "production":
        from stash.gateway.mymind import MyMindGateway
        return MyMindGateway()
    else:
        raise ConfigError(f"Unknown STASH_ENV: {config.ENV}")
