"""Persistent CLI config: server URL, OAuth client credentials, tokens."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_SERVER_URL = "https://www.memorybot.com"


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "memorybot"


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class Config:
    server_url: str = DEFAULT_SERVER_URL
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None  # epoch seconds
    user_email: Optional[str] = None

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if not path.exists():
            return cls()
        with path.open() as f:
            data = json.load(f)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(asdict(self), f, indent=2)
        tmp.chmod(0o600)
        tmp.replace(path)

    def clear_tokens(self) -> None:
        self.access_token = None
        self.refresh_token = None
        self.expires_at = None
        self.user_email = None
        self.server_url = DEFAULT_SERVER_URL


def resolve_server_url(cli_override: Optional[str], cfg: Config) -> str:
    """Precedence: --base-url flag > MEMORYBOT_URL env > config > default."""
    if cli_override:
        return cli_override.rstrip("/")
    env = os.environ.get("MEMORYBOT_URL")
    if env:
        return env.rstrip("/")
    return cfg.server_url.rstrip("/")
