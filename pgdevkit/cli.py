from __future__ import annotations

import os
from pathlib import Path

import psycopg
import typer
from rich.console import Console
from rich.table import Table
from rich import box

from . import migrate, testdb
from .backends import get_backend
from .connection import build_conninfo
from .diff import DiffKind, compute_diff
from .fetch_missing import SUBFOLDER, find_missing_objects, layer_folder_for, reconstruct_ddl
from .parser import parse_directory

app = typer.Typer(name="pgdb", help="PostgreSQL database schema tools")
console = Console()
err_console = Console(stderr=True)

testdb_app = typer.Typer(name="testdb", help="Manage the shared local Postgres test container")
app.add_typer(testdb_app, name="testdb")

migrate_app = typer.Typer(
    name="migrate", help="Apply numbered, forward-only SQL migration files, tracked in a DB table"
)
app.add_typer(migrate_app, name="migrate")


@app.command()
def compare(
    url: str = typer.Option(..., "--url", help="PostgreSQL DSN (postgresql://user:pass@host:port/db)"),
    entra_user: str | None = typer.Option(None, "--entra-user", help="Azure Entra user (triggers token auth)"),
    databricks_workspace_host: str | None = typer.Option(
        None,
        "--databricks-workspace-host",
        help="Databricks workspace URL, e.g. https://adb-....azuredatabricks.net (required for Lakebase hosts)",
    ),
    databricks_instance: str | None = typer.Option(
        None, "--databricks-instance", help="Lakebase instance name (required for Lakebase hosts)"
    ),
    report_extra_db: bool = typer.Option(False, "--report-extra-db", help="Report objects in DB but not in scripts"),
    dialect: str = typer.Option("postgres", "--dialect", help="postgres (default) or mssql"),
    scripts_dir: Path = typer.Argument(..., help="Directory containing SQL scripts"),
) -> None:
    """Compare SQL scripts to a live database and report differences."""
    if not scripts_dir.is_dir():
        err_console.print(f"[red]Error:[/red] {scripts_dir} is not a directory")
        raise typer.Exit(2)

    try:
        conninfo = build_conninfo(
            url,
            entra_user,
            databricks_workspace_host=databricks_workspace_host,
            databricks_instance=databricks_instance,
        )
    except ValueError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(2)

    try:
        backend = get_backend(dialect)
    except ValueError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(2)

    with console.status("Parsing SQL scripts..."):
        scripts_schema = parse_directory(scripts_dir, dialect=backend.dialect)

    with console.status("Introspecting database..."):
        db_schema = backend.introspect(conninfo)

    diffs = compute_diff(scripts_schema, db_schema, report_extra_db=report_extra_db, dialect=backend.dialect)

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


@app.command("fetch-missing")
def fetch_missing(
    scripts_dir: Path = typer.Argument(..., help="The database/ folder to compare against and write into"),
    url: str = typer.Option(..., "--url", help="PostgreSQL DSN (postgresql://user:pass@host:port/db)"),
    entra_user: str | None = typer.Option(None, "--entra-user", help="Azure Entra user (triggers token auth)"),
    write: bool = typer.Option(False, "--write", help="Write the reconstructed .sql files (default: dry run)"),
    only: list[str] = typer.Option([], "--only", help="Only fetch schema.name (repeatable); default is everything"),
) -> None:
    """Find tables/views/functions that exist in the database but aren't
    tracked under scripts_dir, and reverse-engineer their DDL into new files."""
    if not scripts_dir.is_dir():
        err_console.print(f"[red]Error:[/red] {scripts_dir} is not a directory")
        raise typer.Exit(2)

    conninfo = build_conninfo(url, entra_user)

    with console.status("Comparing database/ against the live schema..."):
        missing = find_missing_objects(scripts_dir, conninfo)

    if only:
        wanted = set(only)
        missing = [m for m in missing if m.qualified_name in wanted]

    if not missing:
        console.print("[green]No missing objects.[/green]")
        return

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("Type", style="magenta")
    table.add_column("Object")
    table.add_column("Destination", style="dim")
    for m in missing:
        dest = layer_folder_for(scripts_dir, m.schema) / SUBFOLDER[m.object_type] / f"{m.name}.sql"
        table.add_row(m.object_type, m.qualified_name, str(dest))
    console.print(table)

    if not write:
        console.print("\n[yellow]Dry run[/yellow] — pass --write to create these files.")
        return

    written = 0
    with psycopg.connect(conninfo) as conn:
        for m in missing:
            dest_dir = layer_folder_for(scripts_dir, m.schema) / SUBFOLDER[m.object_type]
            dest = dest_dir / f"{m.name}.sql"
            if dest.exists():
                console.print(f"  [yellow]SKIP[/yellow] {dest} (already exists)")
                continue
            try:
                ddl = reconstruct_ddl(conn, m)
            except Exception as e:  # noqa: BLE001
                err_console.print(f"[red]Error[/red] reconstructing {m.qualified_name}: {e}")
                continue
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest.write_text(ddl, encoding="utf-8")
            console.print(f"  [green]WROTE[/green] {dest}")
            written += 1

    console.print(f"\nWrote {written} file(s).")


@testdb_app.command("up")
def testdb_up() -> None:
    """Ensure the container is running, the workspace DB exists, and schema is applied."""
    testdb.ensure_testdb()
    info = testdb.status()
    console.print(f"[green]Test DB ready:[/green] {info['database']} ({info['dsn']})")


@testdb_app.command("reset")
def testdb_reset() -> None:
    """Drop and recreate only this workspace's database, then reapply schema + seed data."""
    testdb.reset_testdb()
    info = testdb.status()
    console.print(f"[green]Test DB reset:[/green] {info['database']}")


@testdb_app.command("run-sql")
def testdb_run_sql(
    file: Path | None = typer.Argument(None, help="Path to a .sql file"),
    sql: str | None = typer.Option(None, "--sql", help="Inline SQL string"),
    results: bool = typer.Option(False, "--results", help="Print query results as a table"),
) -> None:
    """Run SQL against this workspace's database."""
    if (file is None) == (sql is None):
        err_console.print("[red]Error:[/red] pass exactly one of FILE or --sql")
        raise typer.Exit(2)
    statement = file.read_text(encoding="utf-8") if file else sql
    assert statement is not None
    rows = testdb.run_sql(statement)

    if rows is None:
        console.print("OK")
        return
    if not results:
        console.print(f"OK — {len(rows)} row(s)")
        return
    if not rows:
        console.print("(0 row(s))")
        return
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    for col in rows[0]:
        table.add_column(col)
    for row in rows:
        table.add_row(*(str(v) for v in row.values()))
    console.print(table)
    console.print(f"({len(rows)} row(s))")


@testdb_app.command("status")
def testdb_status() -> None:
    """Show container state, this workspace's database name, and DSN."""
    for key, value in testdb.status().items():
        console.print(f"{key}: {value}")


@testdb_app.command("shell")
def testdb_shell() -> None:
    """Drop into an interactive shell (psql, or sqlcmd for MSSQL) against
    this workspace's database."""
    binary, argv = testdb.shell_argv()
    os.execvp(binary, argv)


@testdb_app.command("clean")
def testdb_clean(
    all: bool = typer.Option(False, "--all", help="Drop every database belonging to this project"),
) -> None:
    """Drop this workspace's database (or every database of this project with --all)."""
    testdb.clean_testdb(all=all)
    console.print("[green]Cleaned.[/green]")


@migrate_app.command("check")
def migrate_check(
    migrations_dir: Path = typer.Argument(..., help="Directory of numbered .sql migration files"),
    url: str = typer.Option(..., "--url", help="PostgreSQL DSN (postgresql://user:pass@host:port/db)"),
    entra_user: str | None = typer.Option(None, "--entra-user", help="Azure Entra user (triggers token auth)"),
    tracking_table: str | None = typer.Option(
        None,
        "--tracking-table",
        help="schema.table recording applied migrations "
        "(default: tool.pgdevkit.migrations_table in pyproject.toml, else public.schema_migrations)",
    ),
) -> None:
    """List which migration files under migrations_dir are applied vs. pending."""
    if not migrations_dir.is_dir():
        err_console.print(f"[red]Error:[/red] {migrations_dir} is not a directory")
        raise typer.Exit(2)

    conninfo = build_conninfo(url, entra_user)
    tracking_table = tracking_table or migrate.default_tracking_table(migrations_dir)
    local_files = migrate.list_migration_files(migrations_dir)
    try:
        applied = migrate.applied_migrations(conninfo, tracking_table)
    except migrate.TrackingTableMissing:
        err_console.print(f"[yellow]⚠[/yellow]  {tracking_table} not found — nothing recorded as applied yet")
        applied = {}

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("File")
    table.add_column("Status")
    table.add_column("Applied at", style="dim")
    pending = []
    for f in local_files:
        if f.name in applied:
            applied_at, applied_by = applied[f.name]
            table.add_row(f.name, "[green]applied[/green]", f"{applied_at:%Y-%m-%d %H:%M} ({applied_by})")
        else:
            table.add_row(f.name, "[yellow]pending[/yellow]", "")
            pending.append(f.name)
    console.print(table)
    console.print(f"\n{len(pending)} pending, {len(applied)} applied, {len(local_files)} total")


@migrate_app.command("apply")
def migrate_apply(
    migrations_dir: Path = typer.Argument(..., help="Directory of numbered .sql migration files"),
    url: str = typer.Option(..., "--url", help="PostgreSQL DSN (postgresql://user:pass@host:port/db)"),
    entra_user: str | None = typer.Option(None, "--entra-user", help="Azure Entra user (triggers token auth)"),
    tracking_table: str | None = typer.Option(
        None,
        "--tracking-table",
        help="schema.table recording applied migrations "
        "(default: tool.pgdevkit.migrations_table in pyproject.toml, else public.schema_migrations)",
    ),
    file: str | None = typer.Option(
        None, "--file", help="Apply only this one filename (relative to migrations_dir) instead of all pending"
    ),
    ask: bool = typer.Option(False, "--ask", help="Show and confirm each migration before running it"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirm-target prompt"),
) -> None:
    """Apply pending migration files, in filename order, tracking each in tracking_table."""
    if not migrations_dir.is_dir():
        err_console.print(f"[red]Error:[/red] {migrations_dir} is not a directory")
        raise typer.Exit(2)

    conninfo = build_conninfo(url, entra_user)
    tracking_table = tracking_table or migrate.default_tracking_table(migrations_dir)
    target_desc = url.rsplit("@", 1)[-1] if "@" in url else url
    if not yes:
        typer.confirm(f"About to run migrations against {target_desc}. Continue?", abort=True)

    if file:
        targets = [migrations_dir / file]
    else:
        try:
            targets = migrate.pending_migrations(migrations_dir, conninfo, tracking_table)
        except migrate.TrackingTableMissing:
            targets = migrate.list_migration_files(migrations_dir)

    if not targets:
        console.print("No pending migrations.")
        return

    for path in targets:
        console.print(f"\n=== {path.name} ===")
        already_done = False
        if ask:
            console.print(path.read_text(encoding="utf-8"))
            answer = typer.prompt("[Y]es execute / [n]o skip / [a]lready done / [q]uit", default="y").strip().lower()
            if answer in ("q", "quit"):
                console.print("Aborted.")
                raise typer.Exit(1)
            if answer in ("n", "no"):
                console.print(f"Skipped {path.name}")
                continue
            if answer in ("a", "already", "already done"):
                already_done = True
            elif answer not in ("", "y", "yes"):
                console.print(f"Skipped {path.name}")
                continue

        try:
            result = migrate.apply_migration(conninfo, path, tracking_table, already_done=already_done)
        except migrate.MigrationVerificationError as e:
            err_console.print(f"[red]✗[/red] {e}")
            raise typer.Exit(1)

        if not result.executed:
            console.print(f"[green]✓[/green] Recorded {path.name} as already applied (not executed)")
            continue
        for tbl in result.verified_tables:
            console.print(f"  [green]✓[/green] table {tbl} exists")
        console.print(f"[green]✓[/green] Applied {path.name}")
