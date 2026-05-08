"""Persistent CLI config: server URL, OAuth client credentials, tokens.

Secret fields (access_token, refresh_token, expires_at, client_secret)
persist via the OS keyring when ``use_keyring`` is True. The on-disk
file then holds only the lookup identity (server_url, client_id,
user_email) and the storage policy flag itself. Callers continue to
read/write via the dataclass attributes — the persistence split is
hidden inside ``load()`` / ``save()``.

Existing users on file storage keep working until they explicitly run
``mb migrate-keyring`` (or re-login). New ``mb login`` runs default to
keyring mode where the OS supports it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from . import secret_store

DEFAULT_SERVER_URL = "https://www.memorybot.com"

# Fields persisted via the OS keyring when use_keyring is True. Anything
# else stays in the JSON file at config_path().
_SECRET_FIELDS = ("access_token", "refresh_token", "expires_at", "client_secret")


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
    use_keyring: bool = False

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if not path.exists():
            return cls()
        with path.open() as f:
            data = json.load(f)
        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        if cfg.use_keyring and cfg.client_id:
            blob = secret_store.load(cfg.client_id, cfg.server_url)
            if blob:
                for key in _SECRET_FIELDS:
                    setattr(cfg, key, blob.get(key))
        return cfg

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.use_keyring and self.client_id:
            secret_store.save(
                self.client_id,
                self.server_url,
                {k: getattr(self, k) for k in _SECRET_FIELDS},
            )
            payload = {k: v for k, v in asdict(self).items() if k not in _SECRET_FIELDS}
        else:
            payload = asdict(self)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(payload, f, indent=2)
        tmp.chmod(0o600)
        tmp.replace(path)

    def clear_tokens(self) -> None:
        if self.use_keyring and self.client_id:
            secret_store.clear(self.client_id, self.server_url)
        self.access_token = None
        self.refresh_token = None
        self.expires_at = None
        self.user_email = None
        self.server_url = DEFAULT_SERVER_URL


def has_legacy_file_secrets() -> bool:
    """True iff config.json holds OAuth secrets in plaintext (pre-keyring layout).

    Drives the one-line nudge that suggests ``mb migrate-keyring``.
    """
    path = config_path()
    if not path.exists():
        return False
    try:
        with path.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if data.get("use_keyring"):
        return False
    return any(data.get(k) for k in _SECRET_FIELDS)


def resolve_server_url(cli_override: Optional[str], cfg: Config) -> str:
    """Precedence: --base-url flag > MEMORYBOT_URL env > config > default."""
    if cli_override:
        return cli_override.rstrip("/")
    env = os.environ.get("MEMORYBOT_URL")
    if env:
        return env.rstrip("/")
    return cfg.server_url.rstrip("/")


def resolve_access_token(cfg: Config) -> Optional[str]:
    """Precedence: MEMORYBOT_TOKEN env > config.

    Env-var path is used by LLM Python sandboxes that bootstrap auth via
    the MCP `mint_session_token` tool — no `mb login` needed.
    """
    env = os.environ.get("MEMORYBOT_TOKEN")
    if env:
        return env.strip()
    return cfg.access_token
