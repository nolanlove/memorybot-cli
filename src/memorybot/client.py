"""Authenticated HTTP client with auto-refresh on 401."""

from __future__ import annotations

from typing import Any, Optional

import httpx

from .auth import refresh_access_token
from .config import Config


class APIError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class Client:
    def __init__(self, cfg: Config, server_url: str) -> None:
        self.cfg = cfg
        self.server_url = server_url

    def _headers(self) -> dict[str, str]:
        if not self.cfg.access_token:
            raise RuntimeError("Not logged in. Run `mb login`.")
        return {"Authorization": f"Bearer {self.cfg.access_token}"}

    def get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict:
        url = f"{self.server_url}{path}"
        resp = httpx.get(url, headers=self._headers(), params=params, timeout=30.0)
        if resp.status_code == 401 and refresh_access_token(self.cfg, self.server_url):
            resp = httpx.get(url, headers=self._headers(), params=params, timeout=30.0)
        if resp.status_code >= 400:
            raise APIError(resp.status_code, resp.text)
        return resp.json()
