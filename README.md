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
```

`--json` on any command emits machine-readable output for piping into `jq`.

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
