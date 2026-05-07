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
block, and runs the code via `uv run --python $(which python3) <tmpfile>`
with `MEMORYBOT_TOKEN` and `MEMORYBOT_URL` set in the env. Stdout and
stderr stream live; the script's exit code propagates.

```bash
mb run eaQ5a4Hgxl              # run with whatever token the CLI is using
mb run eaQ5a4Hgxl --no-log     # skip writing the run-audit memo
```

Inside the script, `from memorybot.client import Client` picks up the env
vars set by the runner. Requires [`uv`](https://docs.astral.sh/uv/) on `PATH`.

The CLI does **not** mint its own per-run token. The script subprocess
inherits the same identity (`MEMORYBOT_TOKEN`) the CLI is using. To get a
narrower scope, shorter TTL, or `script_sid`-bound audit attribution, mint
a cli token via the **`mint_cli_token`** MCP tool (Claude Desktop /
claude.ai) first and set its output as `MEMORYBOT_TOKEN` before invoking
`mb run`.

### Audit trail

After the script exits, `mb run` posts a memo titled `Run of <script-title>
— rc=<N>` with rc, duration, started_at, and a captured stdout excerpt,
plus an `instance_of` ref pointing to the script. Pass `--no-log` to skip
the memo. If your token was minted with `script_sid=<sid>`, every API call
the script makes is also attributed in server logs.

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
