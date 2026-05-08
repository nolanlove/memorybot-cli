"""OS keyring storage for OAuth secrets (access/refresh tokens, client secret).

Splits identity from authority: the file at ``~/.config/memorybot/config.json``
keeps non-secret fields (server URL, user email, client_id, the lookup key)
while real credentials live in the OS keyring — macOS Keychain, Linux Secret
Service / KWallet, Windows Credential Manager. A process running as the user
can no longer just ``cat`` the config file to walk away with a token.

The ``keyring`` package picks the backend automatically. On macOS the user
sees a Touch ID / password prompt the first time the entry is read in a new
keychain-unlock session; subsequent reads are cached by the OS. We detect the
``null`` / ``fail`` backends (Linux without a keyring daemon) and refuse to
use keyring mode in that case so secrets aren't silently dropped.
"""

from __future__ import annotations

import json
from typing import Any, Optional

# Service-name namespace. Per-server suffix lets a user keep tokens for
# self-hosted MemoryBot installs side-by-side with the production one.
_SERVICE_PREFIX = "memorybot"

# Keys persisted in the keyring blob. Mirrors the secret half of Config.
_SECRET_KEYS = ("access_token", "refresh_token", "expires_at", "client_secret")


def _service_name(server_url: str) -> str:
    return f"{_SERVICE_PREFIX}:{server_url.rstrip('/')}"


def keyring_available() -> bool:
    """True if a real keyring backend is wired up on this machine.

    Returns False when the active backend is the no-op fallback (Linux
    without Secret Service, headless CI, etc.) — callers should refuse
    keyring mode in that case rather than silently dropping secrets.
    """
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return False
    backend = keyring.get_keyring()
    if isinstance(backend, fail.Keyring):
        return False
    # The `null` backend reports itself by class name; importing it
    # directly is version-dependent so match by qualname.
    if type(backend).__name__ == "Keyring" and "null" in type(backend).__module__.lower():
        return False
    return True


def load(client_id: str, server_url: str) -> Optional[dict[str, Any]]:
    """Read the secret blob for (client_id, server_url). None if absent."""
    if not client_id:
        return None
    import keyring
    raw = keyring.get_password(_service_name(server_url), client_id)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return {k: data.get(k) for k in _SECRET_KEYS}


def save(client_id: str, server_url: str, secrets: dict[str, Any]) -> None:
    """Write the secret blob. Overwrites any existing entry for the same key."""
    if not client_id:
        raise ValueError("client_id is required to store keyring secrets")
    import keyring
    payload = {k: secrets.get(k) for k in _SECRET_KEYS}
    keyring.set_password(_service_name(server_url), client_id, json.dumps(payload))


def clear(client_id: str, server_url: str) -> None:
    """Best-effort delete. Silently succeeds if no entry exists."""
    if not client_id:
        return
    import keyring
    import keyring.errors
    try:
        keyring.delete_password(_service_name(server_url), client_id)
    except keyring.errors.PasswordDeleteError:
        pass
