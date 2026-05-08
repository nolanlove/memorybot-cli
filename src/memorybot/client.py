"""Authenticated HTTP client for the MemoryBot tool-exec endpoint."""

from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Any, Optional, Union

import httpx

from .auth import refresh_access_token
from .config import Config, resolve_access_token, resolve_server_url

TOOL_EXEC_PATH = "/memory/api/tool-exec/"

# Streaming chunk size for hashing the file before upload. 1MB is a good
# default: small enough to keep memory bounded, large enough that hashing a
# multi-GB file doesn't drown in syscall overhead.
_HASH_CHUNK_SIZE = 1024 * 1024


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

    # ---- Media upload (presigned-PUT, three-step dance) -----------------

    # Generous timeout for the actual S3 PUT. Defaults to 5 min; bump for
    # very large files. Connect timeout is short to fail fast on egress
    # block-style errors before we've burned bandwidth.
    UPLOAD_PUT_TIMEOUT = httpx.Timeout(300.0, connect=30.0)

    def upload_media(
        self,
        path: Union[str, Path],
        title: Optional[str] = None,
        tag_sids: Optional[list[str]] = None,
        content_type: Optional[str] = None,
    ) -> dict:
        """Upload a file to MemoryBot via the presigned-PUT path.

        Up to three round trips, none of which carry the file bytes through
        MB. Bytes go direct to S3 via a presigned PUT.

        1. ``manage_media:request_upload`` (with content_hash for dedup)
           -> ``{upload_url, upload_id, ...}`` on miss, or
              ``{memo_sid, duplicate, dedup}`` on hit (steps 2-3 skipped)
        2. ``PUT`` raw bytes to ``upload_url`` (S3, presigned) -- skipped
           on dedup hit
        3. ``manage_media:finalize_upload`` -> ``{memo_sid, ...}`` --
           skipped on dedup hit

        The full file is read once locally to compute its SHA-256, used
        both for the dedup short-circuit and (on miss) sent to
        ``finalize_upload`` so the server doesn't have to round-trip the
        bytes through MB just to hash them.

        Args:
            path: Path to the local file.
            title: Optional memo title (defaults to filename server-side).
            tag_sids: Optional list of leaf tag SIDs (10-char base62).
            content_type: Optional MIME override; auto-detected from the
                filename if omitted. The PUT request MUST send this exact
                value or S3 rejects the signed request.

        Returns the parsed ``finalize_upload`` response (memo_sid, s3_key,
        size, mime_type, category, plus skipped_tags / face-detection bits
        when applicable). On dedup hit, returns the request_upload
        response shape: ``{success, memo_sid, duplicate, dedup}``.

        Raises:
            FileNotFoundError: ``path`` does not exist.
            APIError: HTTP error talking to MB (steps 1, 3) or S3 (step 2).
            ToolError: server returned a structured error (e.g. file too
                large, upload_id expired, upload_not_completed).
        """
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(p)

        size = p.stat().st_size
        if not content_type:
            content_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"

        # Stream-hash the file. One full read; bounded memory. Hashing is
        # ~3 GB/s on modern hardware -- effectively free vs. the network
        # round trips we save when this hash lets the server short-circuit.
        hasher = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(_HASH_CHUNK_SIZE), b""):
                hasher.update(chunk)
        content_hash = hasher.hexdigest()

        # Step 1: ask MB for a presigned PUT URL. If the server finds an
        # existing memo with this content_hash, it returns the dedup
        # response immediately; we skip steps 2 and 3.
        req = self.tool_exec(
            "manage_media",
            {"operations": [{
                "action": "request_upload",
                "filename": p.name,
                "content_type": content_type,
                "size": size,
                "content_hash": content_hash,
            }]},
        )
        if req.get("dedup"):
            return req
        upload_url = req["upload_url"]
        upload_id = req["upload_id"]

        # Step 2: PUT raw bytes directly to S3. No Authorization header --
        # the presigned URL carries auth via query-string sig. The
        # Content-Type header MUST match what was bound into the signature.
        with p.open("rb") as fh:
            put_resp = httpx.put(
                upload_url,
                content=fh,
                headers={"Content-Type": content_type},
                timeout=self.UPLOAD_PUT_TIMEOUT,
            )
        if put_resp.status_code >= 400:
            raise APIError(put_resp.status_code, put_resp.text)

        # Step 3: tell MB the bytes are in place, get back a memo SID.
        finalize_args: dict[str, Any] = {
            "action": "finalize_upload",
            "upload_id": upload_id,
            "content_hash": content_hash,
        }
        if title is not None:
            finalize_args["title"] = title
        if tag_sids:
            finalize_args["tag_sids"] = tag_sids

        return self.tool_exec(
            "manage_media",
            {"operations": [finalize_args]},
        )

