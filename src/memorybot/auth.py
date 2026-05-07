"""OAuth 2.0 authorization-code flow with PKCE and a one-shot local callback server."""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from typing import Optional

import httpx

from .config import Config

CLIENT_NAME = "MemoryBot CLI"
# `cli` is the capability scope that authenticates the HTTP tool-exec API.
# The CLI never talks the MCP transport — it talks tool-exec — so we never
# request `mcp` here. The server's per-Application policy will also enforce
# this and strip any `mcp` that snuck in.
SCOPES = "read write cli"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _make_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _pick_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    captured: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        type(self).captured = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<html><body style='font-family:system-ui;padding:2rem;'>"
            "<h2>MemoryBot CLI — login complete</h2>"
            "<p>You can close this tab and return to your terminal.</p>"
            "</body></html>"
        )
        self.wfile.write(body.encode())

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


def _register_client(server_url: str, redirect_uri: str) -> tuple[str, str]:
    """Dynamic client registration (RFC 7591). Returns (client_id, client_secret)."""
    resp = httpx.post(
        f"{server_url}/oauth/register/",
        json={
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["client_id"], data["client_secret"]


def _exchange_code(
    server_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    verifier: str,
) -> dict:
    resp = httpx.post(
        f"{server_url}/oauth/token/",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": verifier,
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(cfg: Config, server_url: str) -> bool:
    """Use refresh_token to get a new access_token. Returns True on success."""
    if not (cfg.refresh_token and cfg.client_id and cfg.client_secret):
        return False
    try:
        resp = httpx.post(
            f"{server_url}/oauth/token/",
            data={
                "grant_type": "refresh_token",
                "refresh_token": cfg.refresh_token,
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        return False
    data = resp.json()
    cfg.access_token = data["access_token"]
    cfg.refresh_token = data.get("refresh_token", cfg.refresh_token)
    cfg.expires_at = time.time() + int(data.get("expires_in", 36000))
    cfg.save()
    return True


def login_flow(server_url: str, timeout_seconds: int = 300) -> dict:
    """Run the full auth-code + PKCE flow. Returns the token response dict.

    Side effect: opens the user's browser to the authorize URL.
    """
    port = _pick_free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    client_id, client_secret = _register_client(server_url, redirect_uri)

    verifier, challenge = _make_pkce()
    state = secrets.token_urlsafe(16)

    authorize_url = (
        f"{server_url}/oauth/authorize/?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": SCOPES,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )

    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    _CallbackHandler.captured = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        webbrowser.open(authorize_url)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline and not _CallbackHandler.captured:
            time.sleep(0.1)
    finally:
        server.shutdown()

    captured = _CallbackHandler.captured
    if not captured:
        raise TimeoutError("Timed out waiting for OAuth callback.")
    if captured.get("state") != state:
        raise RuntimeError("OAuth state mismatch — possible CSRF.")
    if "error" in captured:
        raise RuntimeError(f"Authorization denied: {captured['error']}")
    if "code" not in captured:
        raise RuntimeError("No authorization code received.")

    token_resp = _exchange_code(
        server_url=server_url,
        client_id=client_id,
        client_secret=client_secret,
        code=captured["code"],
        redirect_uri=redirect_uri,
        verifier=verifier,
    )
    token_resp["_client_id"] = client_id
    token_resp["_client_secret"] = client_secret
    return token_resp


def fetch_user_email(server_url: str, access_token: str) -> Optional[str]:
    try:
        resp = httpx.get(
            f"{server_url}/api/auth/user/",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("email")
    except httpx.HTTPError:
        return None
