"""Authenticated HTTP client for the MemoryBot tool-exec endpoint."""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from .auth import refresh_access_token
from .config import Config, resolve_access_token, resolve_server_url

TOOL_EXEC_PATH = "/memory/api/tool-exec/"


class APIError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class ToolError(RuntimeError):
    """Raised when the server returns 200 with a {'error': ...} body."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Client:
    def __init__(self, cfg: Optional[Config] = None, server_url: Optional[str] = None) -> None:
        self.cfg = cfg if cfg is not None else Config.load()
        self.server_url = server_url or resolve_server_url(None, self.cfg)

    def _token(self) -> str:
        token = resolve_access_token(self.cfg)
        if not token:
            raise RuntimeError(
                "No credentials. Run `mb login`, or set MEMORYBOT_TOKEN."
            )
        return token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
        }

    def tool_exec(self, tool: str, arguments: dict[str, Any]) -> dict:
        """Call POST /api/tool-exec/ with {tool, arguments}. Returns parsed JSON.

        Raises APIError on HTTP error, ToolError if the response body has
        {'error': ...} (the executor's own validation/errors).
        """
        url = f"{self.server_url}{TOOL_EXEC_PATH}"
        body = {"tool": tool, "arguments": arguments}

        resp = httpx.post(url, headers=self._headers(), json=body, timeout=60.0)
        # Only attempt refresh when using the saved-config token; env-var
        # tokens are short-lived by design and the caller is expected to
        # re-mint via mint_session_token instead.
        env_token = bool(os.environ.get("MEMORYBOT_TOKEN"))
        if resp.status_code == 401 and not env_token and refresh_access_token(self.cfg, self.server_url):
            resp = httpx.post(url, headers=self._headers(), json=body, timeout=60.0)
        if resp.status_code >= 400:
            raise APIError(resp.status_code, resp.text)

        data = resp.json()
        if isinstance(data, dict) and "error" in data and len(data) == 1:
            raise ToolError(data["error"])
        return data

    # ---- High-level helpers (auto-batching, one round-trip per chunk) ----

    GET_BATCH_SIZE = 50

    def get_memos(self, sids: list[str], full: bool = True) -> list[dict]:
        """Fetch memos by sid, chunking at GET_BATCH_SIZE.

        Returns a flat list of memo dicts. Missing sids are silently dropped
        (matches server behavior on action='get' with non-existent sids).
        """
        out: list[dict] = []
        for i in range(0, len(sids), self.GET_BATCH_SIZE):
            chunk = sids[i : i + self.GET_BATCH_SIZE]
            res = self.tool_exec(
                "manage_memos",
                {"operations": [{"action": "get", "memo_sids": chunk, "full": full}]},
            )
            out.extend(res.get("memos", []))
        return out

    def get_refs(self, sids: list[str], direction: str = "both") -> list[dict]:
        """Fetch refs for memos by sid, chunking at GET_BATCH_SIZE."""
        out: list[dict] = []
        for i in range(0, len(sids), self.GET_BATCH_SIZE):
            chunk = sids[i : i + self.GET_BATCH_SIZE]
            res = self.tool_exec(
                "manage_memos",
                {"operations": [{"action": "get_refs", "memo_sids": chunk, "direction": direction}]},
            )
            out.extend(res.get("refs", []))
        return out

    def mint_session_token(self, scope: str = "read", ttl_seconds: int = 300) -> dict:
        """Mint a short-lived OAuth bearer scoped to ``scope`` (default read-only).

        Returns ``{token, expires_at, scope, usage}``. Raises ToolError if the
        requested scope exceeds the caller's, or APIError on HTTP failure.
        """
        return self.tool_exec(
            "mint_session_token",
            {"ttl_seconds": ttl_seconds, "scope": scope},
        )
