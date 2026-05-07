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

from . import __version__
from .auth import fetch_user_email, login_flow
from .client import APIError, Client, ToolError
from .config import Config, config_path, resolve_server_url

app = typer.Typer(
    name="mb",
    help="MemoryBot CLI — your personal knowledge graph from the command line.",
    no_args_is_help=True,
    add_completion=False,
)
memo_app = typer.Typer(name="memo", help="Search, get, and manage memos.", no_args_is_help=True)
app.add_typer(memo_app)

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
    pass


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
) -> None:
    """Authenticate via browser-based OAuth (authorization code + PKCE)."""
    cfg = Config.load()
    server_url = resolve_server_url(base_url, cfg)
    cfg.server_url = server_url

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


@app.command("query")
def query_cmd(
    sql: str = typer.Argument(..., help="A read-only SELECT against the v_* views."),
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


def main() -> int:
    app()
    return 0
