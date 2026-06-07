"""SPICE simulator as a virtual instrument (spicelib + ngspice).

Returns the same WaveformData shape the scope uses, so a simulated node trace and a
bench capture are directly comparable. The netlist is read live from the project
(project.toml [netlist].path) at call time, so setting a netlist in Settings →
Simulation takes effect without restarting the server.
"""

import shutil
import tempfile
import tomllib
from pathlib import Path

from teasel_server.base import SimulatorBase, WaveformData


class SpiceSimulator(SimulatorBase):
    @property
    def slug(self) -> str:
        return "spice"

    @property
    def name(self) -> str:
        return "SPICE Simulator (ngspice)"

    def __init__(self, config: dict):
        self._project_dir = Path(config.get("PROJECT_DIR") or ".")

    # ── netlist resolution (live from project.toml) ──────────────────────────
    def _netlist_path(self) -> Path | None:
        proj = self._project_dir / "project.toml"
        if not proj.exists():
            return None
        try:
            rel = tomllib.loads(proj.read_text()).get("netlist", {}).get("path")
        except Exception:
            return None
        if not rel:
            return None
        p = Path(rel)
        return p if p.is_absolute() else (self._project_dir / p)

    def _run(self):
        """Run the configured netlist; return (RawRead, netlist_path). Raises with a
        clear message on misconfiguration so the tool surfaces it instead of crashing."""
        netlist = self._netlist_path()
        if netlist is None:
            raise RuntimeError(
                "No SPICE netlist set — configure one under Settings → Simulation."
            )
        if not netlist.exists():
            raise RuntimeError(f"Netlist not found: {netlist}")
        if shutil.which("ngspice") is None:
            raise RuntimeError("ngspice not found on PATH — install ngspice to run simulations.")

        from spicelib import RawRead, SimRunner, SpiceEditor
        from spicelib.simulators.ngspice_simulator import NGspiceSimulator

        editor = SpiceEditor(str(netlist))
        outdir = tempfile.mkdtemp(prefix="teasel-sim-")
        runner = SimRunner(simulator=NGspiceSimulator, output_folder=outdir)
        raw, log = runner.run_now(editor)
        if raw is None:
            raise RuntimeError(f"ngspice run failed (see log: {log}).")
        return RawRead(str(raw)), netlist

    @staticmethod
    def _find_trace(rr, node: str) -> str | None:
        wanted = {node.lower(), f"v({node.lower()})"}
        for n in rr.get_trace_names():
            if n.lower() in wanted:
                return n
        return None

    # ── tools ────────────────────────────────────────────────────────────────
    def simulate(self, node: str, analysis: str = "tran") -> WaveformData:
        rr, netlist = self._run()
        tname = self._find_trace(rr, node)
        if tname is None:
            avail = ", ".join(rr.get_trace_names()[:20])
            raise RuntimeError(f"Node '{node}' not in results. Available: {avail}")

        import numpy as np

        # The x-axis is the 'time' (tran) or 'frequency' (ac) trace; get_axis() is
        # unreliable across ngspice raw dialects, so read it as a named trace.
        axis_name = next(
            (n for n in rr.get_trace_names() if n.lower() in ("time", "frequency")), None
        )
        x = np.asarray(rr.get_wave(axis_name)) if axis_name else np.arange(len(rr.get_wave(tname)))
        y = np.asarray(rr.get_wave(tname))
        if np.iscomplexobj(y):  # AC analysis → magnitude
            y = np.abs(y)
        x = np.real(x)
        time_data = [float(v) for v in x]
        voltage_data = [float(v) for v in y]
        dt = (time_data[1] - time_data[0]) if len(time_data) > 1 else 0.0
        sample_rate = (1.0 / dt) if dt else 0.0
        return WaveformData(
            channel=0,
            time_data=time_data,
            voltage_data=voltage_data,
            sample_rate=sample_rate,
            metadata={"node": node, "source": "ngspice", "netlist": netlist.name},
        )

    def list_nodes(self) -> list[str]:
        rr, _ = self._run()
        nodes: list[str] = []
        for n in rr.get_trace_names():
            low = n.lower()
            if low in ("time", "frequency"):
                continue
            if low.startswith("v(") and low.endswith(")"):
                nodes.append(n[2:-1])
            else:
                nodes.append(n)
        return sorted(set(nodes), key=str.casefold)

    def status(self) -> str:
        netlist = self._netlist_path()
        ng = "ngspice ✓" if shutil.which("ngspice") else "ngspice MISSING"
        if netlist is None:
            return f"{self.name} — no netlist set (Settings → Simulation). {ng}"
        state = "exists" if netlist.exists() else "MISSING"
        return f"{self.name} — netlist: {netlist.name} ({state}). {ng}"
