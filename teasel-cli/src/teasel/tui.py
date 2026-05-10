import asyncio
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, ScrollableContainer
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, ListItem, ListView

from . import config as cfg
from . import state as st
from .registry import DriverDescriptor, fetch_driver, fetch_index

_ADD_ROW_KEY = "__add__"


class _Input(Input):
    """Input that does not intercept escape, so screens can use it for Back."""
    BINDINGS = [b for b in Input.BINDINGS if not (isinstance(b, Binding) and b.key == "escape")]


class ConnectedScreen(Screen):
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "all_setup", "All setup"),
        Binding("c", "connection", "Connection"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="instruments-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self._focused_slug: str | None = None
        table = self.query_one(DataTable)
        table.add_columns("Slug", "Type", "Connection")
        self._refresh_table()

    def on_screen_resume(self) -> None:
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for inst in st.load():
            conn = next(iter(inst.params.values()), "") if inst.params else ""
            table.add_row(inst.slug, inst.type or "—", conn, key=inst.slug)
        table.add_row("[dim]⊕ Add device[/]", "", "", key=_ADD_ROW_KEY)

    @on(DataTable.RowHighlighted, "#instruments-table")
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._focused_slug = str(event.row_key.value) if event.row_key else None

    @on(DataTable.RowSelected, "#instruments-table")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        if key == _ADD_ROW_KEY:
            self.app.push_screen(RegistryBrowserScreen())
        else:
            instruments = st.load()
            inst = next((i for i in instruments if i.slug == key), None)
            if inst:
                self.app.push_screen(SetupScreen([inst]))

    def action_all_setup(self) -> None:
        instruments = st.load()
        if not instruments:
            self.notify("No instruments configured yet — add one first", severity="warning")
            return
        self.app.push_screen(SetupScreen(instruments))

    def action_connection(self) -> None:
        slug = self._focused_slug
        if not slug or slug == _ADD_ROW_KEY:
            return
        self.app.push_screen(InstrumentDetailScreen(slug, for_connection=True))


class RegistryBrowserScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("/", "focus_search", "Search"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield _Input(placeholder="Search instruments...", id="search")
        yield DataTable(id="instruments-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self._all_entries = []
        self._installed: set[str] = {i.driver_slug for i in st.load()}
        table = self.query_one(DataTable)
        table.add_columns("", "Slug", "Name", "Type", "Interfaces")
        self.run_worker(self._load_instruments, exclusive=True)

    def on_screen_resume(self) -> None:
        self._installed = {i.driver_slug for i in st.load()}
        self._populate_table(self._all_entries)

    async def _load_instruments(self) -> None:
        try:
            entries = await asyncio.to_thread(fetch_index)
        except Exception as e:
            self.query_one(DataTable).add_row("", "error", str(e), "", "")
            return
        self._all_entries = entries
        self._populate_table(entries)

    def _populate_table(self, entries) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for e in entries:
            mark = "[green]✓[/]" if e.slug in self._installed else ""
            table.add_row(mark, e.slug, e.name, e.type, ", ".join(e.interfaces), key=e.slug)

    @on(Input.Changed, "#search")
    def filter_table(self, event: Input.Changed) -> None:
        q = event.value.lower()
        filtered = self._all_entries if not q else [
            e for e in self._all_entries
            if q in e.slug or q in e.name.lower()
            or q in e.manufacturer.lower() or q in e.type.lower()
        ]
        self._populate_table(filtered)

    @on(DataTable.RowSelected, "#instruments-table")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        slug = str(event.row_key.value)
        self.app.push_screen(InstrumentDetailScreen(slug))

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()


class SetupScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, instruments: list[st.InstrumentConfig]) -> None:
        super().__init__()
        self.instruments = instruments

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("Experiment Setup", id="wizard-title")
        with ScrollableContainer(id="param-fields"):
            setups = {s.slug: s for s in st.load_setup()}
            for inst in self.instruments:
                current = setups.get(inst.slug, st.InstrumentSetup(slug=inst.slug))
                yield Label(f"[bold]{inst.slug}[/]  [dim]{inst.type}[/]", classes="setup-section")
                if inst.type == "function-generator":
                    yield Label("Amplitude limit (Vpp)")
                    yield _Input(
                        value=str(current.limits.get("amplitude_max", "")),
                        placeholder="no limit",
                        id=f"limit-{inst.slug}-amplitude_max",
                        classes="param-input",
                    )
                    yield Label("Frequency limit (Hz)")
                    yield _Input(
                        value=str(current.limits.get("frequency_max", "")),
                        placeholder="no limit",
                        id=f"limit-{inst.slug}-frequency_max",
                        classes="param-input",
                    )
                if inst.type in ("oscilloscope",):
                    for ch in ("C1", "C2", "C3", "C4"):
                        ch_cfg = current.channels.get(ch, {})
                        yield Label(f"{ch} probe  [dim]label[/]")
                        with Container(classes="probe-row"):
                            yield _Input(
                                value=ch_cfg.get("probe", ""),
                                placeholder="e.g. 10x",
                                id=f"ch-{inst.slug}-{ch}-probe",
                                classes="param-input probe-input",
                            )
                            yield _Input(
                                value=ch_cfg.get("label", ""),
                                placeholder="optional label",
                                id=f"ch-{inst.slug}-{ch}-label",
                                classes="param-input label-input",
                            )
                yield Label("", classes="section-spacer")
        yield Button("Save setup.toml", id="btn-save", variant="primary")
        yield Footer()

    def action_save(self) -> None:
        self._save()

    @on(Button.Pressed, "#btn-save")
    def save_pressed(self, _: Button.Pressed) -> None:
        self._save()

    def _save(self) -> None:
        setups: list[st.InstrumentSetup] = []
        for inst in self.instruments:
            limits: dict[str, float] = {}
            channels: dict[str, dict] = {}
            if inst.type == "function-generator":
                for key in ("amplitude_max", "frequency_max"):
                    widget = self.query_one(f"#limit-{inst.slug}-{key}", Input)
                    val = widget.value.strip()
                    if val:
                        try:
                            limits[key] = float(val)
                        except ValueError:
                            pass
            if inst.type == "oscilloscope":
                for ch in ("C1", "C2", "C3", "C4"):
                    probe = self.query_one(f"#ch-{inst.slug}-{ch}-probe", Input).value.strip()
                    label = self.query_one(f"#ch-{inst.slug}-{ch}-label", Input).value.strip()
                    ch_data: dict = {}
                    if probe:
                        ch_data["probe"] = probe
                    if label:
                        ch_data["label"] = label
                    if ch_data:
                        channels[ch] = ch_data
            setups.append(st.InstrumentSetup(slug=inst.slug, limits=limits, channels=channels))
        st.save_setup(setups)
        cfg.apply(self.instruments)
        self.notify("setup.toml saved")


class InstrumentDetailScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("a", "open_wizard", "Add"),
    ]

    def __init__(self, slug: str, for_connection: bool = False) -> None:
        super().__init__()
        self.slug = slug
        self.for_connection = for_connection  # True when editing an existing instrument's connection
        self.driver: DriverDescriptor | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with ScrollableContainer():
            yield Label("Loading...", id="detail-content")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._load_detail, exclusive=True)

    async def _load_detail(self) -> None:
        try:
            self.driver = await asyncio.to_thread(fetch_driver, self.slug)
            self._render_detail()
        except ValueError as e:
            lines = [
                f"[bold]{self.slug}[/]",
                "",
                f"[yellow]{e}[/]",
                "",
                "This instrument is listed in the registry but has no driver yet.",
                "Check the GitHub repo or contribute one!",
            ]
            self.query_one("#detail-content", Label).update("\n".join(lines))
        except Exception as e:
            self.query_one("#detail-content", Label).update(f"[red]Error:[/] {e}")

    def _render_detail(self) -> None:
        d = self.driver
        if d.package == "teasel-server":
            pkg_line = "Driver: [green]bundled in teasel-server[/]"
        else:
            pkg_line = f"Driver: [dim]uvx --with {d.package} teasel-server[/]"
        lines = [
            f"[bold]{d.name}[/] — {d.manufacturer}",
            f"Type: [cyan]{d.type}[/]  |  Interfaces: {', '.join(d.interfaces)}",
            pkg_line,
        ]
        if d.manual:
            lines.append(f"Manual: {d.manual}")
        if d.github:
            lines.append(f"GitHub: {d.github}")
        if d.setup_steps:
            lines += ["", "[underline]Setup steps:[/]"]
            for i, step in enumerate(d.setup_steps, 1):
                lines.append(f"  {i}. {step}")
        if self.for_connection:
            action_hint = "Press [bold]A[/] to edit connection settings."
        else:
            action_hint = "Press [bold]A[/] to add this instrument."
        lines += ["", action_hint]
        self.query_one("#detail-content", Label).update("\n".join(lines))

    def action_open_wizard(self) -> None:
        if self.driver:
            self.app.push_screen(ConnectionWizardScreen(
                self.driver,
                add_setup_step=not self.for_connection,
                instance_name=self.slug if self.for_connection else None,
            ))



def _list_serial_ports() -> list[str]:
    from serial.tools.list_ports import comports
    return sorted(p.device for p in comports())


class DiscoveryPickerScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    def __init__(self, ports: list[str]) -> None:
        super().__init__()
        self.ports = ports

    def compose(self) -> ComposeResult:
        with Container(id="picker-dialog"):
            yield Label("Select a serial port:")
            yield ListView(
                *[ListItem(Label(p), id=f"port-{i}") for i, p in enumerate(self.ports)],
                id="port-list",
            )

    @on(ListView.Selected, "#port-list")
    def port_selected(self, event: ListView.Selected) -> None:
        idx = int(event.item.id.split("-")[1])
        self.dismiss(self.ports[idx])


class ConnectionWizardScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+s", "submit", "Save"),
        Binding("f1", "discover", "Discover"),
        Binding("f2", "fill_example", "Fill example"),
    ]

    def __init__(
        self,
        driver: DriverDescriptor,
        add_setup_step: bool = True,
        instance_name: str | None = None,
    ) -> None:
        super().__init__()
        self.driver = driver
        self.add_setup_step = add_setup_step  # False when editing an existing connection
        self.instance_name = instance_name    # set when editing; None means new instance

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(f"Configure [bold]{self.driver.name}[/]", id="wizard-title")
        existing = next(
            (i.params for i in st.load() if i.slug == self.instance_name), {}
        ) if self.instance_name else {}
        with ScrollableContainer(id="param-fields"):
            if self.add_setup_step:
                default_name = st.next_instance_name(self.driver.slug)
                yield Label("[bold]Instance name[/]  [dim]unique name for this device[/]")
                yield _Input(
                    value=default_name,
                    placeholder=default_name,
                    id="instance-name",
                    classes="param-input",
                )
                yield Label("", id="err-instance-name", classes="error-label")
            for param in self.driver.params:
                required_mark = " [red]*[/]" if param.required else ""
                yield Label(f"[bold]{param.key}[/]{required_mark}  {param.description}")
                param_name = param.param or param.key.lower()
                if existing.get(param_name):
                    prefill = existing[param_name]
                elif param.default is not None:
                    prefill = param.default
                elif param.required:
                    prefill = param.example or ""
                else:
                    prefill = ""
                yield _Input(
                    value=prefill,
                    placeholder=param.example or "",
                    id=f"param-{param.key}",
                    classes="param-input",
                )
                yield Label("", id=f"err-{param.key}", classes="error-label")
        yield Button("Save to teasel.toml", id="btn-save", variant="primary")
        yield Footer()

    def action_discover(self) -> None:
        focused = self.focused
        if not isinstance(focused, Input) or not focused.id or not focused.id.startswith("param-"):
            return
        key = focused.id[len("param-"):]
        param = next((p for p in self.driver.params if p.key == key), None)
        if not param or param.discovery != "serial-ports":
            self.notify("No discovery available for this field")
            return
        ports = _list_serial_ports()
        if not ports:
            self.notify("No serial ports found", severity="warning")
        elif len(ports) == 1:
            focused.value = ports[0]
        else:
            def _pick(port: str | None) -> None:
                if port:
                    focused.value = port
            self.app.push_screen(DiscoveryPickerScreen(ports), _pick)

    def action_fill_example(self) -> None:
        focused = self.focused
        if not isinstance(focused, Input) or not focused.id or not focused.id.startswith("param-"):
            return
        key = focused.id[len("param-"):]
        param = next((p for p in self.driver.params if p.key == key), None)
        if param and param.example:
            focused.value = param.example

    def action_submit(self) -> None:
        self._save()

    @on(Button.Pressed, "#btn-save")
    def save_pressed(self, _: Button.Pressed) -> None:
        self._save()

    def _save(self) -> None:
        params: dict[str, str] = {}
        missing: list[str] = []

        for param in self.driver.params:
            widget = self.query_one(f"#param-{param.key}", Input)
            value = widget.value.strip()
            param_name = param.param or param.key.lower()
            if value:
                params[param_name] = value
            elif param.default is not None:
                params[param_name] = param.default
            elif param.required:
                missing.append(param.key)

        if missing:
            for key in missing:
                self.query_one(f"#err-{key}", Label).update("[red]Required[/]")
            return

        if self.add_setup_step:
            raw = self.query_one("#instance-name", Input).value.strip()
            inst_slug = raw or self.driver.slug
        else:
            inst_slug = self.instance_name

        instruments = [i for i in st.load() if i.slug != inst_slug]
        new_inst = st.InstrumentConfig(
            slug=inst_slug,
            driver=self.driver.slug if self.driver.slug != inst_slug else "",
            package=self.driver.package,
            type=self.driver.type,
            params=params,
        )
        instruments.append(new_inst)
        st.save(instruments)
        path = cfg.apply(instruments)
        if self.add_setup_step:
            self.app.push_screen(InstrumentSetupScreen(new_inst, path))
        else:
            self.notify("teasel.toml updated")
            self.app.pop_screen()


class InstrumentSetupScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+s", "save_setup", "Save"),
    ]

    def __init__(self, instrument: st.InstrumentConfig, config_path: Path) -> None:
        super().__init__()
        self.instrument = instrument
        self.config_path = config_path

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(
            f"Setup [bold]{self.instrument.slug}[/]  [dim](optional)[/]",
            id="wizard-title",
        )
        inst = self.instrument
        current = next(
            (s for s in st.load_setup() if s.slug == inst.slug),
            st.InstrumentSetup(slug=inst.slug),
        )
        with ScrollableContainer(id="param-fields"):
            if inst.type == "function-generator":
                yield Label("Amplitude limit (Vpp)")
                yield _Input(
                    value=str(current.limits.get("amplitude_max", "")),
                    placeholder="no limit",
                    id=f"limit-{inst.slug}-amplitude_max",
                    classes="param-input",
                )
                yield Label("Frequency limit (Hz)")
                yield _Input(
                    value=str(current.limits.get("frequency_max", "")),
                    placeholder="no limit",
                    id=f"limit-{inst.slug}-frequency_max",
                    classes="param-input",
                )
            if inst.type in ("oscilloscope",):
                for ch in ("C1", "C2", "C3", "C4"):
                    ch_cfg = current.channels.get(ch, {})
                    yield Label(f"{ch} probe  [dim]label[/]")
                    with Container(classes="probe-row"):
                        yield _Input(
                            value=ch_cfg.get("probe", ""),
                            placeholder="e.g. 10x",
                            id=f"ch-{inst.slug}-{ch}-probe",
                            classes="param-input probe-input",
                        )
                        yield _Input(
                            value=ch_cfg.get("label", ""),
                            placeholder="optional label",
                            id=f"ch-{inst.slug}-{ch}-label",
                            classes="param-input label-input",
                        )
        with Container(id="btn-row"):
            yield Button("Save setup.toml", id="btn-save", variant="primary")
            yield Button("Skip", id="btn-skip", variant="default")
        yield Footer()

    def action_save_setup(self) -> None:
        self._save()

    @on(Button.Pressed, "#btn-save")
    def save_pressed(self, _: Button.Pressed) -> None:
        self._save()

    @on(Button.Pressed, "#btn-skip")
    def skip_pressed(self, _: Button.Pressed) -> None:
        self.app.push_screen(SuccessScreen(self.config_path, pops=4))

    def _save(self) -> None:
        inst = self.instrument
        limits: dict[str, float] = {}
        channels: dict[str, dict] = {}

        if inst.type == "function-generator":
            for key in ("amplitude_max", "frequency_max"):
                widget = self.query_one(f"#limit-{inst.slug}-{key}", Input)
                val = widget.value.strip()
                if val:
                    try:
                        limits[key] = float(val)
                    except ValueError:
                        pass

        if inst.type == "oscilloscope":
            for ch in ("C1", "C2", "C3", "C4"):
                probe = self.query_one(f"#ch-{inst.slug}-{ch}-probe", Input).value.strip()
                label = self.query_one(f"#ch-{inst.slug}-{ch}-label", Input).value.strip()
                ch_data: dict = {}
                if probe:
                    ch_data["probe"] = probe
                if label:
                    ch_data["label"] = label
                if ch_data:
                    channels[ch] = ch_data

        existing_setups = [s for s in st.load_setup() if s.slug != inst.slug]
        existing_setups.append(st.InstrumentSetup(slug=inst.slug, limits=limits, channels=channels))
        st.save_setup(existing_setups)
        self.app.push_screen(SuccessScreen(self.config_path, pops=4))


class SuccessScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss_success", "Close")]

    def __init__(self, path: Path, pops: int = 2) -> None:
        super().__init__()
        self.path = path
        self.pops = pops

    def compose(self) -> ComposeResult:
        with Container(id="success-dialog"):
            yield Label("[green]Config written![/]", id="success-title")
            yield Label(str(self.path), id="success-path")
            yield Label("Claude Code will pick this up automatically.")
            yield Button("Done", id="btn-done", variant="success")

    @on(Button.Pressed, "#btn-done")
    def done_pressed(self, _: Button.Pressed) -> None:
        self.action_dismiss_success()

    def action_dismiss_success(self) -> None:
        self.dismiss()
        for _ in range(self.pops):
            self.app.pop_screen()


class TeaselApp(App):
    CSS_PATH = "teasel.tcss"
    TITLE = "teasel"

    def on_mount(self) -> None:
        from . import get_version
        self.sub_title = get_version()
        self.push_screen(ConnectedScreen())


def run_tui() -> None:
    TeaselApp().run()
