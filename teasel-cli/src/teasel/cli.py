from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import config as cfg
from . import state as st
from .registry import fetch_driver

app = typer.Typer(
    name="teasel",
    help="Connect lab instruments to AI assistants.",
    no_args_is_help=False,
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    ver: bool = typer.Option(False, "--version", "-V", is_eager=True, help="Show version and exit."),
) -> None:
    if ver:
        typer.echo(version("teasel"))
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        from .tui import run_tui
        run_tui()
console = Console()
err = Console(stderr=True)


@app.command()
def add(
    slug: str = typer.Argument(..., help="Instrument slug, e.g. 'pm5190'"),
    set_args: Annotated[
        list[str], typer.Option("--set", "-s", metavar="KEY=VALUE", help="Set a config param.")
    ] = [],
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Directory for .mcp.json"),
) -> None:
    """Add an instrument and regenerate .mcp.json."""
    with console.status(f"Fetching driver for '{slug}'..."):
        try:
            driver = fetch_driver(slug)
        except Exception as e:
            err.print(f"[red]Error:[/] {e}")
            raise typer.Exit(1)

    provided: dict[str, str] = {}
    for s in set_args:
        if "=" not in s:
            err.print(f"[red]Error:[/] --set value must be KEY=VALUE, got: {s!r}")
            raise typer.Exit(1)
        k, v = s.split("=", 1)
        provided[k] = v

    env: dict[str, str] = {}
    for param in driver.params:
        if param.key in provided:
            env[param.key] = provided[param.key]
        elif param.default is not None:
            env[param.key] = provided.get(param.key, param.default)
        elif param.required:
            hint = f" (e.g. {param.example})" if param.example else ""
            env[param.key] = typer.prompt(f"{param.description}{hint}")

    instruments = [i for i in st.load() if i.slug != slug]
    instruments.append(st.InstalledInstrument(slug=slug, package=driver.package, env=env))
    st.save(instruments)

    path = cfg.apply(instruments, output)
    console.print(f"[green]✓[/] Added [cyan]{driver.name}[/] → [bold]{path}[/]")


@app.command()
def remove(
    slug: str = typer.Argument(..., help="Instrument slug to remove"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Directory for .mcp.json"),
) -> None:
    """Remove an instrument from the config."""
    instruments = st.load()
    filtered = [i for i in instruments if i.slug != slug]
    if len(filtered) == len(instruments):
        err.print(f"[yellow]'{slug}' is not installed.[/]")
        raise typer.Exit(1)
    st.save(filtered)
    path = cfg.apply(filtered, output)
    console.print(f"[green]✓[/] Removed [cyan]{slug}[/] → [bold]{path}[/]")


@app.command(name="list")
def list_instruments() -> None:
    """List installed instruments."""
    instruments = st.load()
    if not instruments:
        console.print("No instruments installed. Run [cyan]teasel add <slug>[/] to get started.")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Slug", style="cyan")
    table.add_column("Package")
    table.add_column("Config")
    for inst in instruments:
        config_str = ", ".join(f"{k}={v}" for k, v in inst.env.items()) or "—"
        table.add_row(inst.slug, inst.package, config_str)
    console.print(table)


@app.command()
def apply(
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Directory for .mcp.json"),
) -> None:
    """Regenerate .mcp.json from saved state."""
    instruments = st.load()
    path = cfg.apply(instruments, output)
    n = len(instruments)
    console.print(f"[green]✓[/] Written to [bold]{path}[/] ({n} instrument{'s' if n != 1 else ''})")


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind to"),
    port: int = typer.Option(7890, "--port", "-p", help="Port to listen on"),
) -> None:
    """Start the teasel web UI."""
    import uvicorn
    console.print(f"Starting teasel web UI at [link]http://{host}:{port}[/link]")
    uvicorn.run("teasel.web:app", host=host, port=port, reload=False)


def main() -> None:
    app()
