from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

from .config import write_config
from .registry import fetch_index, fetch_instrument_by_slug
from .wizard import (
    InvalidParamValue,
    MissingRequiredParam,
    build_resolved_config,
    check_required,
    parse_set_args,
    resolve_values,
    validate_and_coerce,
)

app = typer.Typer(
    name="teasel",
    help="Connect lab instruments to AI assistants.",
    no_args_is_help=False,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        from .tui import run_tui
        run_tui()


@app.command()
def add(
    slug: str = typer.Argument(..., help="Instrument slug, e.g. 'pm5190'"),
    set: list[str] = typer.Option(
        [], "--set", "-s", metavar="KEY=VALUE", help="Set a config param. Repeatable."
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Directory to write config into. Defaults to cwd."
    ),
    target: str = typer.Option(
        "claude-code", "--target", "-t", help="Config format (claude-code)."
    ),
) -> None:
    """Add an instrument to your AI assistant config."""
    with console.status(f"Fetching instrument '{slug}'..."):
        try:
            instrument, registry_name, warnings = fetch_instrument_by_slug(slug)
        except ValueError as e:
            err_console.print(f"[red]Error:[/] {e}")
            raise typer.Exit(1)
    for w in warnings:
        err_console.print(f"[yellow]Warning:[/] {w}")

    try:
        provided = parse_set_args(set)
    except ValueError as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1)

    known_keys = {p.key for p in instrument.params}
    for key in provided:
        if key not in known_keys:
            err_console.print(f"[yellow]Warning:[/] '{key}' is not a documented param for '{slug}'")

    if len(instrument.packages) > 1:
        console.print("\nMultiple implementations available:")
        for i, pkg in enumerate(instrument.packages):
            console.print(f"  [{i}] [cyan]{pkg.package}[/] by {pkg.author} — {pkg.description}")
        choice = typer.prompt("Select implementation", default="0")
        try:
            package_index = int(choice)
        except ValueError:
            err_console.print("[red]Error:[/] Please enter a number.")
            raise typer.Exit(1)
    else:
        package_index = 0

    resolved = resolve_values(instrument.params, provided)
    missing = check_required(instrument.params, resolved)
    for key in missing:
        param = next(p for p in instrument.params if p.key == key)
        hint = f" (e.g. {param.example})" if param.example else ""
        while True:
            value = typer.prompt(f"{param.description}{hint}")
            try:
                validate_and_coerce(param, value)
                provided[key] = value
                break
            except InvalidParamValue as e:
                err_console.print(f"[red]Invalid value:[/] {e}")

    try:
        resolved_config = build_resolved_config(instrument, provided, package_index)
    except MissingRequiredParam as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1)

    path = write_config(resolved_config, target=target, output_dir=output)
    console.print(f"[green]✓[/] Written to [bold]{path}[/]  [dim](from {registry_name})[/]")


@app.command(name="list")
def list_instruments(
    type: Optional[str] = typer.Option(None, "--type", help="Filter by instrument type."),
    interface: Optional[str] = typer.Option(None, "--interface", help="Filter by interface."),
) -> None:
    """List instruments available in the registry."""
    with console.status("Fetching registry..."):
        entries, warnings = fetch_index()
    for w in warnings:
        err_console.print(f"[yellow]Warning:[/] {w}")

    if type:
        entries = [e for e in entries if e.type == type]
    if interface:
        entries = [e for e in entries if interface in e.interfaces]

    if not entries:
        console.print("No instruments found.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Slug", style="cyan")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Interfaces")
    table.add_column("Registry", style="dim")
    for e in entries:
        table.add_row(e.slug, e.name, e.type, ", ".join(e.interfaces), e.registry)
    console.print(table)


def main() -> None:
    app()
