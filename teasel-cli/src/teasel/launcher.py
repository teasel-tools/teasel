"""Process orchestration for the launcher menu.

The TUI launcher screen only *picks* a mode (it exits Textual returning a
``LaunchChoice``); this module runs in the plain terminal afterwards. It prints
the equivalent commands (so they stay learnable), starts the right processes
with flags derived from the project, and tears everything down on Ctrl-C.
"""

import importlib.util
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from . import config as cfg
from . import state as st

console = Console()
err = Console(stderr=True)


@dataclass
class LaunchChoice:
    mode: str  # "standalone" | "companion" | "mcp" | "local"
    web_port: int = 7890
    server_port: int = 7891


# ── command construction ──────────────────────────────────────────────────────

def _server_base(project_dir: Path) -> list[str]:
    """How to invoke teasel-server.

    Order matters:
    1. If teasel_server is importable in *this* interpreter, run it via the same
       Python (``-m``). This is the dev/workspace case (editable install) and any
       env where both packages are installed together — it ties the server to our
       exact venv and live source, instead of whatever a bare ``teasel-server`` on
       PATH happens to resolve to (e.g. a global ~/.local/bin shadow).
    2. Else a ``teasel-server`` on PATH.
    3. Else ``uvx teasel-server`` with the project's extra driver packages — the
       same shape .mcp.json uses, so a customer who only ran ``uvx teasel`` works.
    """
    if importlib.util.find_spec("teasel_server") is not None:
        return [sys.executable, "-m", "teasel_server.server"]
    if shutil.which("teasel-server"):
        return ["teasel-server"]
    if shutil.which("uvx"):
        packages = cfg.extra_packages(st.load(project_dir))
        with_args = [item for pkg in packages for item in ("--with", pkg)]
        return ["uvx", *with_args, "teasel-server"]
    raise RuntimeError(
        "Can't find teasel-server or uvx. Install uv (https://docs.astral.sh/uv/) "
        "or `pip install teasel-server`."
    )


def _server_command(project_dir: Path, transport: str, web_port: int, server_port: int) -> list[str]:
    cmd = _server_base(project_dir)
    cmd += ["--transport", transport, "--port", str(server_port)]
    config_path = project_dir / st.TEASEL_TOML
    setup_path = project_dir / st.PROJECT_TOML
    if config_path.exists():
        cmd += ["--config", str(config_path.resolve())]
    if setup_path.exists():
        cmd += ["--setup", str(setup_path.resolve())]
    cmd += ["--ui-url", f"http://localhost:{web_port}"]
    return cmd


# ── process helpers ───────────────────────────────────────────────────────────

def _port_open(port: int, host: str = "localhost", timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _print_command(label: str, cmd: list[str]) -> None:
    console.print(f"[dim]{label}:[/dim]")
    console.print(f"  [cyan]{' '.join(cmd)}[/cyan]")


def _start_server(cmd: list[str], server_port: int) -> subprocess.Popen | None:
    """Start the MCP server in the background and wait until its port answers.
    Returns None if a server is already running on the port (we reuse it)."""
    if _port_open(server_port):
        console.print(f"[green]✓[/] Reusing MCP server already on :{server_port}")
        return None
    proc = subprocess.Popen(cmd)  # inherits stdio so driver-load lines are visible
    for _ in range(30):  # ~15s
        if proc.poll() is not None:
            raise RuntimeError(f"teasel-server exited early (code {proc.returncode}).")
        if _port_open(server_port):
            console.print(f"[green]✓[/] MCP server  :{server_port}")
            return proc
        time.sleep(0.5)
    _stop_server(proc)
    raise RuntimeError(f"teasel-server didn't come up on :{server_port} in time.")


def _stop_server(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _open_browser_when_ready(web_port: int) -> None:
    def _wait_open() -> None:
        for _ in range(30):
            if _port_open(web_port):
                webbrowser.open(f"http://localhost:{web_port}")
                return
            time.sleep(0.5)
    threading.Thread(target=_wait_open, daemon=True).start()


def _run_web(web_port: int) -> None:
    import uvicorn
    uvicorn.run("teasel.web:app", host="127.0.0.1", port=web_port, reload=False)


# ── modes ─────────────────────────────────────────────────────────────────────

def launch_standalone(choice: LaunchChoice) -> None:
    project_dir = Path.cwd()
    if _port_open(choice.web_port):
        err.print(f"[red]Something is already listening on :{choice.web_port}.[/] "
                  "Is a teasel web UI already running? Close it or pick another port.")
        return
    server_cmd = _server_command(project_dir, "streamable-http", choice.web_port, choice.server_port)
    # A previous Companion launch persisted [ui].mode = "companion", which makes the
    # web chat show "paused" — standalone means the web chat IS the brain.
    st.save_ui_mode("standalone", project_dir)
    console.print("[bold]Standalone[/] — MCP server + web UI + chat brain\n")
    _print_command("MCP server", server_cmd)
    _print_command("Web UI", ["teasel", "web", "--port", str(choice.web_port)])
    console.print()

    proc = _start_server(server_cmd, choice.server_port)
    try:
        console.print(f"Opening [link]http://localhost:{choice.web_port}[/link]  "
                      "[dim](Ctrl-C to stop)[/dim]\n")
        _open_browser_when_ready(choice.web_port)
        _run_web(choice.web_port)
    except KeyboardInterrupt:
        pass
    finally:
        _stop_server(proc)
        console.print("\n[dim]Stopped.[/dim]")


def launch_mcp_only(choice: LaunchChoice) -> None:
    project_dir = Path.cwd()
    if _port_open(choice.server_port):
        err.print(f"[red]Port :{choice.server_port} is already in use[/] — "
                  "a teasel-server may already be running.")
        return
    server_cmd = _server_command(project_dir, "streamable-http", choice.web_port, choice.server_port)
    console.print("[bold]MCP server only[/] — connect your own client\n")
    _print_command("Command", server_cmd)
    console.print(f"\nListening on :{choice.server_port}  [dim](Ctrl-C to stop)[/dim]\n")
    try:
        subprocess.run(server_cmd)
    except KeyboardInterrupt:
        pass
    console.print("\n[dim]Stopped.[/dim]")


def launch_companion(choice: LaunchChoice) -> None:
    project_dir = Path.cwd()
    instruments = st.load(project_dir)
    mcp_path = cfg.apply(instruments, project_dir)
    st.save_ui_mode("companion", project_dir)
    console.print("[bold]Companion[/] — Claude Code is the brain\n")
    console.print(f"[green]✓[/] Wrote [bold]{mcp_path}[/] — Claude Code starts teasel-server "
                  "automatically over stdio.")
    console.print("  Run [cyan]claude[/] in this directory to connect.\n")
    if _port_open(choice.web_port):
        err.print(f"[yellow]A web UI is already on :{choice.web_port}; not starting another.[/]")
        return
    console.print(f"Starting the web UI for the board / journal / probe tabs at "
                  f"[link]http://localhost:{choice.web_port}[/link]  [dim](Ctrl-C to stop)[/dim]\n")
    try:
        _open_browser_when_ready(choice.web_port)
        _run_web(choice.web_port)
    except KeyboardInterrupt:
        pass
    console.print("\n[dim]Stopped.[/dim]")


def launch_local(choice: LaunchChoice) -> None:
    import asyncio
    from .local_llm import run, run_first_run_wizard

    project_dir = Path.cwd()
    teasel_url = f"http://localhost:{choice.server_port}/mcp"
    server_cmd = _server_command(project_dir, "streamable-http", choice.web_port, choice.server_port)
    console.print("[bold]Local REPL[/] — terminal chat against teasel-server\n")
    _print_command("MCP server", server_cmd)
    console.print()

    proc = _start_server(server_cmd, choice.server_port)
    try:
        pc = st.resolve_provider(None, project_dir)
        if pc is None:
            pc = run_first_run_wizard()
        asyncio.run(run(pc, teasel_url, None))
    except KeyboardInterrupt:
        console.print()
    finally:
        _stop_server(proc)
        console.print("[dim]Stopped.[/dim]")


_DISPATCH = {
    "standalone": launch_standalone,
    "companion": launch_companion,
    "mcp": launch_mcp_only,
    "local": launch_local,
}


def run_launch(choice: LaunchChoice) -> None:
    fn = _DISPATCH.get(choice.mode)
    if fn is None:
        err.print(f"[red]Unknown launch mode: {choice.mode}[/]")
        return
    try:
        fn(choice)
    except RuntimeError as e:
        err.print(f"[red]{e}[/]")
