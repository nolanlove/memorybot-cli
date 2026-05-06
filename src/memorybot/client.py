"""Authenticated HTTP client for the MemoryBot tool-exec endpoint."""

from __future__ import annotations

from typing import Any

import httpx

from .auth import refresh_access_token
from .config import Config

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
    def __init__(self, cfg: Config, server_url: str) -> None:
        self.cfg = cfg
        self.server_url = server_url

    def _headers(self) -> dict[str, str]:
        if not self.cfg.access_token:
            raise RuntimeError("Not logged in. Run `mb login`.")
        return {
            "Authorization": f"Bearer {self.cfg.access_token}",
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
        if resp.status_code == 401 and refresh_access_token(self.cfg, self.server_url):
            resp = httpx.post(url, headers=self._headers(), json=body, timeout=60.0)
        if resp.status_code >= 400:
            raise APIError(resp.status_code, resp.text)

        data = resp.json()
        if isinstance(data, dict) and "error" in data and len(data) == 1:
            raise ToolError(data["error"])
        return data
