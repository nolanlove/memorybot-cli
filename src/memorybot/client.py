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
