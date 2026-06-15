from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich import box

from .connection import build_conninfo
from .diff import DiffKind, compute_diff
from .introspect import introspect_db
from .parser import parse_directory

app = typer.Typer(name="pgdb", help="PostgreSQL database schema tools")
console = Console()
err_console = Console(stderr=True)


@app.command()
def compare(
    scripts_dir: Path = typer.Argument(..., help="Directory containing SQL scripts"),
    url: str = typer.Option(..., "--url", help="PostgreSQL DSN (postgresql://user:pass@host:port/db)"),
    entra_user: str | None = typer.Option(None, "--entra-user", help="Azure Entra user (triggers token auth)"),
    report_extra_db: bool = typer.Option(False, "--report-extra-db", help="Report objects in DB but not in scripts"),
) -> None:
    """Compare SQL scripts to a live PostgreSQL database and report differences."""
    if not scripts_dir.is_dir():
        err_console.print(f"[red]Error:[/red] {scripts_dir} is not a directory")
        raise typer.Exit(2)

    conninfo = build_conninfo(url, entra_user)

    with console.status("Parsing SQL scripts..."):
        scripts_schema = parse_directory(scripts_dir)

    with console.status("Introspecting database..."):
        db_schema = introspect_db(conninfo)

    diffs = compute_diff(scripts_schema, db_schema, report_extra_db=report_extra_db)

    if not diffs:
        console.print("[green]No differences found.[/green]")
        return

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("Kind", style="cyan", min_width=20)
    table.add_column("Type", style="magenta", min_width=15)
    table.add_column("Object")
    table.add_column("Detail", style="dim")

    kind_style = {
        DiffKind.MISSING_IN_DB: "[yellow]missing in DB[/yellow]",
        DiffKind.MISSING_IN_SCRIPTS: "[blue]missing in scripts[/blue]",
        DiffKind.MISMATCH: "[red]mismatch[/red]",
    }
    for d in diffs:
        table.add_row(kind_style[d.kind], d.object_type, d.object_name, d.detail)

    console.print(table)
    console.print(f"\n[bold red]{len(diffs)} difference(s) found.[/bold red]")
    raise typer.Exit(1)
