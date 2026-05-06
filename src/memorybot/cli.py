"""MemoryBot CLI — Typer app. All commands route through tool-exec."""

from __future__ import annotations

import json as json_module
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


def main() -> int:
    app()
    return 0
