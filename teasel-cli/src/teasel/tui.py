import asyncio
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, ScrollableContainer
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label

from . import config as cfg
from . import state as st
from .registry import DriverDescriptor, fetch_driver, fetch_index
from .state import InstalledInstrument


class InstrumentListScreen(Screen):
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("/", "focus_search", "Search"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Search instruments...", id="search")
        yield DataTable(id="instruments-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self._all_entries = []
        self._installed: set[str] = {i.slug for i in st.load()}
        table = self.query_one(DataTable)
        table.add_columns("", "Slug", "Name", "Type", "Interfaces")
        self.run_worker(self._load_instruments, exclusive=True)

    def on_screen_resume(self) -> None:
        self._installed = {i.slug for i in st.load()}
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


class InstrumentDetailScreen(Screen):
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("a", "add_instrument", "Add"),
    ]

    def __init__(self, slug: str) -> None:
        super().__init__()
        self.slug = slug
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
        lines = [
            f"[bold]{d.name}[/] — {d.manufacturer}",
            f"Type: [cyan]{d.type}[/]  |  Interfaces: {', '.join(d.interfaces)}",
            f"Package: [dim]{d.package}[/]",
        ]
        if d.manual:
            lines.append(f"Manual: {d.manual}")
        if d.github:
            lines.append(f"GitHub: {d.github}")
        if d.setup_steps:
            lines += ["", "[underline]Setup steps:[/]"]
            for i, step in enumerate(d.setup_steps, 1):
                lines.append(f"  {i}. {step}")
        lines += ["", "Press [bold]A[/] to add this instrument to your config."]
        self.query_one("#detail-content", Label).update("\n".join(lines))

    def action_add_instrument(self) -> None:
        if self.driver:
            self.app.push_screen(ConfigWizardScreen(self.driver))


class ConfigWizardScreen(Screen):
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("ctrl+s", "submit", "Save"),
    ]

    def __init__(self, driver: DriverDescriptor) -> None:
        super().__init__()
        self.driver = driver

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(f"Configure [bold]{self.driver.name}[/]", id="wizard-title")
        with ScrollableContainer(id="param-fields"):
            for param in self.driver.params:
                required_mark = " [red]*[/]" if param.required else ""
                yield Label(f"[bold]{param.key}[/]{required_mark}  {param.description}")
                yield Input(
                    value=param.default or "",
                    placeholder=param.example or "",
                    id=f"param-{param.key}",
                    classes="param-input",
                )
                yield Label("", id=f"err-{param.key}", classes="error-label")
        yield Button("Save to .mcp.json", id="btn-save", variant="primary")
        yield Footer()

    def action_submit(self) -> None:
        self._save()

    @on(Button.Pressed, "#btn-save")
    def save_pressed(self, _: Button.Pressed) -> None:
        self._save()

    def _save(self) -> None:
        env: dict[str, str] = {}
        missing: list[str] = []

        for param in self.driver.params:
            widget = self.query_one(f"#param-{param.key}", Input)
            value = widget.value.strip()
            if value:
                env[param.key] = value
            elif param.default is not None:
                env[param.key] = param.default
            elif param.required:
                missing.append(param.key)

        if missing:
            for key in missing:
                self.query_one(f"#err-{key}", Label).update("[red]Required[/]")
            return

        instruments = [i for i in st.load() if i.slug != self.driver.slug]
        instruments.append(InstalledInstrument(slug=self.driver.slug, package=self.driver.package, env=env))
        st.save(instruments)
        path = cfg.apply(instruments)
        self.app.push_screen(SuccessScreen(path))


class SuccessScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss_success", "Close")]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

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
        self.app.pop_screen()
        self.app.pop_screen()


class TeaselApp(App):
    CSS_PATH = "teasel.tcss"
    TITLE = "teasel"
    SUB_TITLE = "Connect lab instruments to AI assistants"

    def on_mount(self) -> None:
        self.push_screen(InstrumentListScreen())


def run_tui() -> None:
    TeaselApp().run()
