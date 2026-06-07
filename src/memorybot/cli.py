"""MemoryBot CLI — Typer app. All commands route through tool-exec."""

from __future__ import annotations

import json as json_module
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__, secret_store
from .auth import fetch_user_email, login_flow
from .client import APIError, Client, ToolError
from .config import Config, config_path, has_legacy_file_secrets, resolve_server_url

app = typer.Typer(
    name="mb",
    help="MemoryBot CLI — your personal knowledge graph from the command line.",
    no_args_is_help=True,
    add_completion=False,
)
memo_app = typer.Typer(name="memo", help="Search, get, and manage memos.", no_args_is_help=True)
app.add_typer(memo_app)
media_app = typer.Typer(name="media", help="Upload and manage media files.", no_args_is_help=True)
app.add_typer(media_app)

console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"mb {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Optional[bool] = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    if has_legacy_file_secrets():
        err_console.print(
            "[yellow]Tip:[/yellow] your OAuth tokens are stored in plaintext at "
            f"{config_path()}. Run [bold]mb migrate-keyring[/bold] to move them "
            "into the OS keyring."
        )


def _client(base_url: Optional[str]) -> Client:
    cfg = Config.load()
    server_url = resolve_server_url(base_url, cfg)
    return Client(cfg, server_url)


def _unwrap_single_op(result: dict) -> dict:
    """manage_* responses come wrapped as {results: [op_result]}.

    Single-op CLI commands want the inner result directly.
    """
    if isinstance(result, dict) and "results" in result and isinstance(result["results"], list):
        items = result["results"]
        if len(items) == 1:
            return items[0]
    return result


@app.command()
def login(
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Override server URL for this run."),
    no_keyring: bool = typer.Option(
        False,
        "--no-keyring",
        help="Store OAuth secrets as plaintext in config.json instead of the OS keyring. "
             "Only use this on systems without a working keyring backend (some headless Linux).",
    ),
) -> None:
    """Authenticate via browser-based OAuth (authorization code + PKCE)."""
    cfg = Config.load()
    server_url = resolve_server_url(base_url, cfg)
    cfg.server_url = server_url

    if no_keyring:
        cfg.use_keyring = False
    elif secret_store.keyring_available():
        cfg.use_keyring = True
    else:
        err_console.print(
            "[yellow]No OS keyring backend available; falling back to plaintext file storage.[/yellow] "
            "Pass --no-keyring to silence this warning."
        )
        cfg.use_keyring = False

    err_console.print(f"Logging in to [bold]{server_url}[/bold]...")
    err_console.print("Opening browser for authorization. Waiting for callback...")

    try:
        token_resp = login_flow(server_url)
    except Exception as e:
        err_console.print(f"[red]Login failed:[/red] {e}")
        raise typer.Exit(code=1)

    cfg.client_id = token_resp["_client_id"]
    cfg.client_secret = token_resp["_client_secret"]
    cfg.access_token = token_resp["access_token"]
    cfg.refresh_token = token_resp.get("refresh_token")
    cfg.expires_at = time.time() + int(token_resp.get("expires_in", 36000))

    cfg.user_email = fetch_user_email(server_url, cfg.access_token)
    cfg.save()

    who = cfg.user_email or "(email not available)"
    err_console.print(f"[green]Logged in as[/green] [bold]{who}[/bold]")
    if cfg.use_keyring:
        err_console.print(f"Identity saved to {config_path()}; secrets stored in OS keyring.")
    else:
        err_console.print(f"Credentials saved to {config_path()}")


@app.command()
def logout() -> None:
    """Clear stored credentials."""
    cfg = Config.load()
    cfg.clear_tokens()
    cfg.client_id = None
    cfg.client_secret = None
    cfg.save()
    err_console.print("Logged out.")


@app.command(name="migrate-keyring")
def migrate_keyring() -> None:
    """Move existing plaintext OAuth secrets from config.json into the OS keyring."""
    cfg = Config.load()
    if cfg.use_keyring:
        err_console.print("[green]Already using the OS keyring.[/green] Nothing to migrate.")
        return
    if not (cfg.access_token or cfg.refresh_token or cfg.client_secret):
        err_console.print("No stored credentials to migrate. Run [bold]mb login[/bold] first.")
        raise typer.Exit(code=1)
    if not cfg.client_id:
        err_console.print(
            "[red]Cannot migrate:[/red] config.json is missing the OAuth client_id "
            "(needed as the keyring lookup key). Re-run [bold]mb login[/bold]."
        )
        raise typer.Exit(code=1)
    if not secret_store.keyring_available():
        err_console.print(
            "[red]No OS keyring backend available on this system.[/red] "
            "Install / start your keyring daemon (e.g. gnome-keyring on Linux), "
            "or stay on file storage and rely on the 0600 permissions."
        )
        raise typer.Exit(code=1)

    cfg.use_keyring = True
    cfg.save()  # writes secrets to keyring, scrubs them from the file
    err_console.print(
        f"[green]Migrated.[/green] Secrets moved to OS keyring; {config_path()} "
        "now holds only identity fields."
    )


@app.command()
def whoami() -> None:
    """Show the currently logged-in user."""
    cfg = Config.load()
    if not cfg.access_token:
        err_console.print("Not logged in. Run `mb login`.")
        raise typer.Exit(code=1)
    who = cfg.user_email or "(unknown)"
    typer.echo(who)


def _print_memos_table(memos: list[dict]) -> None:
    if not memos:
        err_console.print("[dim](no results)[/dim]")
        return
    table = Table(show_lines=False)
    table.add_column("SID", style="cyan", no_wrap=True)
    table.add_column("Title")
    table.add_column("Tags", style="magenta")
    for m in memos:
        title = m.get("title") or m.get("structured_data", {}).get("memo", {}).get("title") or "(untitled)"
        tag_field = m.get("tags") or []
        if tag_field and isinstance(tag_field[0], dict):
            tags = ", ".join(t.get("name", "") for t in tag_field if t.get("name"))
        else:
            tags = ", ".join(str(t) for t in tag_field)
        table.add_row(m.get("sid", ""), title, tags)
    console.print(table)


@memo_app.command("search")
def memo_search(
    query: str = typer.Argument(..., help="Search query."),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results."),
    tag_sid: Optional[str] = typer.Option(None, "--tag-sid", help="Filter under tag sid(s), comma-separated."),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Override server URL for this run."),
) -> None:
    """Search memos via manage_memos action=search."""
    op: dict[str, Any] = {"action": "search", "query": query, "limit": limit}
    if tag_sid:
        op["tag_sids"] = [s.strip() for s in tag_sid.split(",") if s.strip()]

    try:
        result = _client(base_url).tool_exec("manage_memos", {"operations": [op]})
    except APIError as e:
        err_console.print(f"[red]API error:[/red] {e}")
        raise typer.Exit(code=1)
    except ToolError as e:
        err_console.print(f"[red]Tool error:[/red] {e.message}")
        raise typer.Exit(code=1)

    inner = _unwrap_single_op(result)

    if json:
        typer.echo(json_module.dumps(inner, indent=2))
        return

    memos = inner.get("memos") if isinstance(inner, dict) else None
    if memos is None and isinstance(inner, list):
        memos = inner
    _print_memos_table(memos or [])
    if isinstance(inner, dict):
        count = inner.get("count", len(memos or []))
        total = inner.get("total_count", count)
        err_console.print(f"[dim]{count} of {total} results[/dim]")


@memo_app.command("get")
def memo_get(
    sid: str = typer.Argument(..., help="Memo sid (10-char base62)."),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Override server URL for this run."),
) -> None:
    """Fetch a single memo via manage_memos action=get."""
    op = {"action": "get", "memo_sids": [sid], "full": True}

    try:
        result = _client(base_url).tool_exec("manage_memos", {"operations": [op]})
    except APIError as e:
        err_console.print(f"[red]API error:[/red] {e}")
        raise typer.Exit(code=1)
    except ToolError as e:
        err_console.print(f"[red]Tool error:[/red] {e.message}")
        raise typer.Exit(code=1)

    inner = _unwrap_single_op(result)
    memos = inner.get("memos") if isinstance(inner, dict) else (inner if isinstance(inner, list) else [])
    if not memos:
        err_console.print(f"[red]No memo found with sid {sid}.[/red]")
        raise typer.Exit(code=1)
    memo = memos[0]

    if json:
        typer.echo(json_module.dumps(memo, indent=2))
        return

    sd = memo.get("structured_data", {}) or {}
    title = memo.get("title") or sd.get("memo", {}).get("title") or "(untitled)"
    body = sd.get("memo", {}).get("content", "")
    tag_field = memo.get("tags") or []
    if tag_field and isinstance(tag_field[0], dict):
        tags = ", ".join(t.get("name", "") for t in tag_field if t.get("name"))
    else:
        tags = ", ".join(str(t) for t in tag_field)

    console.print(f"[bold cyan]{memo.get('sid', '')}[/bold cyan]  [bold]{title}[/bold]")
    if tags:
        console.print(f"[magenta]tags:[/magenta] {tags}")
    if body:
        console.print()
        console.print(body)


@app.command("schema")
def schema_cmd(
    json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Override server URL for this run."),
) -> None:
    """Print the read-only SQL schema (views, columns, example queries)."""
    try:
        result = _client(base_url).tool_exec("describe_schema", {})
    except APIError as e:
        err_console.print(f"[red]API error:[/red] {e}")
        raise typer.Exit(code=1)
    except ToolError as e:
        err_console.print(f"[red]{e.message}[/red]")
        raise typer.Exit(code=1)

    if json:
        typer.echo(json_module.dumps(result, indent=2))
        return

    description = result.get("description") or ""
    typer.echo(description)


@app.command("query")
def query_cmd(
    sql: str = typer.Argument(..., help="A read-only SELECT against the v_* views. Run `mb schema` to see what's queryable."),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Override server URL for this run."),
) -> None:
    """Run a read-only SQL query against the user's data (run_query tool)."""
    try:
        result = _client(base_url).tool_exec("run_query", {"sql": sql})
    except APIError as e:
        err_console.print(f"[red]API error:[/red] {e}")
        raise typer.Exit(code=1)
    except ToolError as e:
        err_console.print(f"[red]Query error:[/red] {e.message}")
        raise typer.Exit(code=1)

    if json:
        typer.echo(json_module.dumps(result, indent=2))
        return

    columns = result.get("columns", [])
    rows = result.get("rows", [])
    if not rows:
        err_console.print("[dim](no rows)[/dim]")
        return

    table = Table(show_lines=False)
    for col in columns:
        table.add_column(col, overflow="fold")
    for row in rows:
        if isinstance(row, dict):
            table.add_row(*[str(row.get(c, "")) for c in columns])
        else:
            table.add_row(*[str(v) for v in row])
    console.print(table)

    suffix = " (truncated at 200)" if result.get("truncated") else ""
    err_console.print(f"[dim]{result.get('row_count', len(rows))} rows{suffix}[/dim]")


def _inbox_cursor_path(agent_sid: str):
    """Per-agent cursor file, next to config.json (e.g.
    ~/.config/memorybot/inbox-cursor-<sid>.txt). Keeps each message surfacing
    exactly once across `mb inbox` calls without the caller tracking state."""
    return config_path().parent / f"inbox-cursor-{agent_sid}.txt"


def _format_inbox(messages: list[dict]) -> str:
    """Plain-text (no ANSI) rendering of new inbox messages, designed to be
    injected verbatim into a UserPromptSubmit hook's additionalContext."""
    lines = [f"📨 {len(messages)} new agent DM(s):", ""]
    for m in messages:
        sender = m.get("sender_name") or m.get("sender_sid") or "?"
        thread = m.get("thread_title") or "(thread)"
        sent = (m.get("sent_at") or "")[11:16]  # HH:MM slice of the ISO ts
        when = f" {sent}" if sent else ""
        lines.append(f'[{sender} → "{thread}"]{when}')
        body = (m.get("body") or "").strip()
        for bl in body.splitlines() or [""]:
            lines.append(f"  {bl}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@app.command("inbox")
def inbox_cmd(
    agent: Optional[str] = typer.Option(
        None, "--agent", "-a", help="Your agent memo sid (10-char base62)."
    ),
    since: Optional[str] = typer.Option(
        None, "--since", help="ISO-8601 cursor; overrides the stored per-agent cursor."
    ),
    no_cursor: bool = typer.Option(
        False, "--no-cursor", help="Don't read or write the persisted per-agent cursor file."
    ),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON {messages, cursor, threads}."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Override server URL for this run."),
) -> None:
    """Pull new agent-to-agent DMs (instant, non-blocking).

    Hits /api/agent/<sid>/inbox/poll, prints any NEW messages, and advances a
    per-agent cursor so each surfaces exactly once. Prints NOTHING to stdout
    when the inbox is empty — so the output can be dropped straight into a
    UserPromptSubmit hook. This replaces the retired persistent inbox Monitor.

    Exit codes: 0 = success (with or without messages), 1 = API/network error,
    2 = bad arguments (missing --agent).
    """
    if not agent:
        err_console.print("[red]--agent <your agent sid> is required.[/red]")
        raise typer.Exit(code=2)

    cursor_file = None if no_cursor else _inbox_cursor_path(agent)
    effective_since = since
    if effective_since is None and cursor_file is not None and cursor_file.exists():
        effective_since = cursor_file.read_text().strip() or None

    try:
        result = _client(base_url).inbox_poll(agent, since=effective_since)
    except APIError as e:
        err_console.print(f"[red]API error:[/red] {e}")
        raise typer.Exit(code=1)
    except RuntimeError as e:  # no credentials
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)

    messages = result.get("messages") or []
    new_cursor = result.get("cursor")

    # Persist the advanced cursor BEFORE printing, so a crash mid-print can
    # never replay a message — at worst it's dropped (the cursor moved). The
    # server stream uses the same "advance only on return" model.
    if cursor_file is not None and new_cursor:
        cursor_file.parent.mkdir(parents=True, exist_ok=True)
        cursor_file.write_text(new_cursor)

    if json:
        typer.echo(json_module.dumps(result, indent=2))
        return

    if messages:
        # Plain stdout (no rich markup) so a hook can inject it verbatim.
        sys.stdout.write(_format_inbox(messages))
        sys.stdout.flush()
    # Empty inbox → print nothing, exit 0.


_PYTHON_BLOCK_RE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL)
_RUN_LOG_MAX_BYTES = 8000


def _truncate_for_log(text: str, limit: int = _RUN_LOG_MAX_BYTES) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n\n... [truncated {len(text) - limit} bytes] ...\n\n{tail}"


def _post_script_run_memo(
    client: "Client",
    *,
    script_sid: str,
    script_title: str,
    rc: int,
    duration_ms: int,
    started_iso: str,
    stdout_text: str,
) -> None:
    """Write a memo recording this run, with an instance_of ref to the script.

    Best-effort: a failure here (most often a read-only auth token) is
    surfaced as a warning but does not change the run's exit code.
    """
    excerpt = _truncate_for_log(stdout_text)
    body = (
        f"- Script: `{script_sid}` ({script_title})\n"
        f"- Exit code: {rc}\n"
        f"- Duration: {duration_ms} ms\n"
        f"- Started: {started_iso}\n\n"
        f"## Output\n\n```\n{excerpt}\n```\n"
    )
    payload = {
        "operations": [
            {
                "action": "create",
                "memos": [
                    {
                        "title": f"Run of {script_title} — rc={rc}",
                        "structured_data": {
                            "memo": {"content": body, "content_type": "markdown"},
                        },
                        "refs": [{"to_memo_sid": script_sid, "ref_type": "instance_of"}],
                    }
                ],
            }
        ]
    }
    try:
        client.tool_exec("manage_memos", payload)
    except (APIError, ToolError) as e:
        err_console.print(
            f"[yellow]warn:[/yellow] script_run memo write failed: {e}\n"
            "[dim]Your CLI token may be read-only. Mint a write-capable "
            "cli token (or run `mb login`) if you want runs logged.[/dim]"
        )


@app.command("run")
def run_cmd(
    sid: str = typer.Argument(..., help="Script memo sid (10-char base62)."),
    no_log: bool = typer.Option(
        False, "--no-log", help="Skip writing a script_run memo on completion."
    ),
    base_url: Optional[str] = typer.Option(
        None, "--base-url", help="Override server URL for this run."
    ),
) -> None:
    """Execute a Python script memo: fetch by sid, run via uv, stream stdout.

    Extracts the first ```python``` fenced block from the memo's content,
    runs the code via `uv run --python $(which python3) <tmpfile>` with
    the same MEMORYBOT_TOKEN and MEMORYBOT_URL the CLI is currently
    authenticated with. Stdout and stderr stream live; exit code
    propagates.

    The CLI does NOT mint a per-run token. The script subprocess
    inherits the same identity the CLI is using. To narrow scope or
    bind the token to this script's sid for audit attribution, mint a
    cli token via the `mint_cli_token` MCP tool first and set its
    output as `MEMORYBOT_TOKEN` before invoking `mb run`.

    On completion, posts a script_run memo with an `instance_of` ref to
    the script (suppressed by --no-log).
    """
    import datetime as _dt
    import time as _time

    if shutil.which("uv") is None:
        err_console.print(
            "[red]uv not found on PATH.[/red] Install: https://docs.astral.sh/uv/"
        )
        raise typer.Exit(code=127)
    python3 = shutil.which("python3")
    if python3 is None:
        err_console.print("[red]python3 not found on PATH.[/red]")
        raise typer.Exit(code=127)

    client = _client(base_url)

    try:
        res = client.tool_exec(
            "manage_memos",
            {"operations": [{"action": "get", "memo_sids": [sid], "full": True}]},
        )
    except APIError as e:
        err_console.print(f"[red]Fetch error:[/red] {e}")
        raise typer.Exit(code=1)
    except ToolError as e:
        err_console.print(f"[red]Tool error:[/red] {e.message}")
        raise typer.Exit(code=1)

    inner = _unwrap_single_op(res)
    memos = inner.get("memos") if isinstance(inner, dict) else []
    if not memos:
        err_console.print(f"[red]No memo found with sid {sid}.[/red]")
        raise typer.Exit(code=1)

    sd = memos[0].get("structured_data", {}) or {}
    content = sd.get("memo", {}).get("content", "")
    script_title = (
        memos[0].get("title") or sd.get("memo", {}).get("title") or sid
    )
    match = _PYTHON_BLOCK_RE.search(content)
    if not match:
        err_console.print(
            f"[red]No fenced ```python``` block found in memo {sid}.[/red]"
        )
        raise typer.Exit(code=1)
    code = match.group(1)

    env = os.environ.copy()
    # Pass through whatever token the CLI is using. If MEMORYBOT_TOKEN is
    # already in the env (typical for agent-launched runs), it'll already
    # be set; if the CLI is using a config token, surface it explicitly
    # so the subprocess sees the same identity.
    try:
        env["MEMORYBOT_TOKEN"] = client._token()
    except RuntimeError as e:
        err_console.print(f"[red]No CLI token available:[/red] {e}")
        raise typer.Exit(code=1)
    env["MEMORYBOT_URL"] = client.server_url

    captured: list[str] = []
    started_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    started_at = _time.monotonic()

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    try:
        tmp.write(code)
        tmp.close()
        proc = subprocess.Popen(
            ["uv", "run", "--python", python3, tmp.name],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            captured.append(line)
        rc = proc.wait()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    duration_ms = int((_time.monotonic() - started_at) * 1000)

    if not no_log:
        _post_script_run_memo(
            client,
            script_sid=sid,
            script_title=script_title,
            rc=rc,
            duration_ms=duration_ms,
            started_iso=started_iso,
            stdout_text="".join(captured),
        )
    raise typer.Exit(code=rc)


@media_app.command("upload")
def media_upload(
    path: str = typer.Argument(..., help="Path to the file to upload."),
    title: Optional[str] = typer.Option(None, "--title", help="Memo title (defaults to filename)."),
    tag: list[str] = typer.Option(
        [],
        "--tag",
        help="Tag SID to attach (10-char base62). Repeat for multiple tags.",
    ),
    content_type: Optional[str] = typer.Option(
        None,
        "--content-type",
        help="MIME type override. Auto-detected from filename if omitted.",
    ),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON server response."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Override server URL for this run."),
) -> None:
    """Upload a local file to MemoryBot via the presigned-PUT path.

    Three round trips: ``request_upload`` (mint a presigned S3 URL),
    ``PUT`` raw bytes directly to S3, ``finalize_upload`` (create the
    memo). Bytes never traverse the MemoryBot server.

    Exit codes: 0 success, 1 server/network/tool error, 2 file-not-found
    or other arg validation failure.
    """
    from pathlib import Path as _Path

    p = _Path(path)
    if not p.is_file():
        err_console.print(f"[red]File not found:[/red] {path}")
        raise typer.Exit(code=2)

    client = _client(base_url)
    tag_sids = [t.strip() for t in tag if t.strip()] or None

    try:
        result = client.upload_media(
            p,
            title=title,
            tag_sids=tag_sids,
            content_type=content_type,
        )
    except FileNotFoundError:
        err_console.print(f"[red]File not found:[/red] {path}")
        raise typer.Exit(code=2)
    except APIError as e:
        err_console.print(f"[red]API error:[/red] {e}")
        raise typer.Exit(code=1)
    except ToolError as e:
        err_console.print(f"[red]Tool error:[/red] {e.message}")
        raise typer.Exit(code=1)

    if json:
        typer.echo(json_module.dumps(result, indent=2))
        return

    inner = _unwrap_single_op(result)
    sid = inner.get("memo_sid") or inner.get("sid") or ""
    size = inner.get("size")
    mime = inner.get("mime_type") or "?"
    duplicate = inner.get("duplicate")

    parts = [f"uploaded → memo [bold cyan]{sid}[/bold cyan]"]
    if size is not None:
        parts.append(f"({size} bytes, {mime})")
    if duplicate:
        parts.append("[yellow](duplicate of existing memo)[/yellow]")
    console.print(" ".join(parts))

    skipped = inner.get("skipped_tags") or []
    if skipped:
        names = ", ".join(t.get("name", t.get("sid", "?")) for t in skipped)
        err_console.print(
            f"[yellow]warn:[/yellow] skipped branch tag(s) {names} -- "
            "tag a leaf instead."
        )


def main() -> int:
    app()
    return 0
