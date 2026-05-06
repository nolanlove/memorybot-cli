# MemoryBot CLI

> Your personal knowledge graph from the command line.

## Install

```bash
pipx install memorybot
```

(or `pip install memorybot` inside a venv).

## Quick start

```bash
mb login              # opens browser, OAuth flow
mb memo search "..."  # full-text + semantic search
mb memo get <SID>     # fetch a memo by sid
mb run <SID>          # execute a Python script memo
```

`--json` on the read commands emits machine-readable output for piping into `jq`.

## Running script memos

`mb run <sid>` fetches the memo, extracts the first ` ```python ` fenced
block, mints a fresh short-TTL session token (read-only by default), and
runs the code via `uv run --python $(which python3) <tmpfile>` with
`MEMORYBOT_TOKEN` and `MEMORYBOT_URL` set in the env. Stdout and stderr
stream live; the script's exit code propagates.

```bash
mb run eaQ5a4Hgxl              # default: read-only token, 5min TTL
mb run eaQ5a4Hgxl --write      # mint a write-capable token
mb run eaQ5a4Hgxl --ttl 600    # 10-minute TTL
```

Inside the script, `from memorybot.client import Client` picks up the env
vars set by the runner. Requires [`uv`](https://docs.astral.sh/uv/) on `PATH`.

## Configuration

- **`MEMORYBOT_URL`** — server URL (default `https://www.memorybot.com`).
- **`--base-url`** — per-command override.

Credentials are stored at `~/.config/memorybot/config.json` (mode 0600).

## Auth

`mb login` runs the OAuth 2.0 authorization-code flow with PKCE: it registers a
client via Dynamic Client Registration (RFC 7591), opens your browser to the
authorize endpoint, and captures the callback on a one-shot loopback server.
Tokens auto-refresh on 401.

`mb logout` clears stored credentials.

## License

MIT
