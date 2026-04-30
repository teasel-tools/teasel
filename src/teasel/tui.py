import asyncio
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, ScrollableContainer
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label

from .config import write_config
from .models import InstrumentDescriptor
from .registry import fetch_index, fetch_instrument, fetch_instrument_by_slug
from .wizard import (
    InvalidParamValue,
    MissingRequiredParam,
    build_resolved_config,
    resolve_values,
    validate_and_coerce,
)


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
        table = self.query_one(DataTable)
        table.add_columns("Slug", "Name", "Type", "Interfaces")
        self.run_worker(self._load_instruments, exclusive=True)

    async def _load_instruments(self) -> None:
        entries, _ = await asyncio.to_thread(fetch_index)
        self._all_entries = entries
        self._populate_table(entries)

    def _populate_table(self, entries) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for e in entries:
            table.add_row(e.slug, e.name, e.type, ", ".join(e.interfaces), key=e.slug)

    @on(Input.Changed, "#search")
    def filter_table(self, event: Input.Changed) -> None:
        query = event.value.lower()
        if not query:
            self._populate_table(self._all_entries)
            return
        filtered = [
            e for e in self._all_entries
            if query in e.slug or query in e.name.lower()
            or query in e.manufacturer.lower() or query in e.type.lower()
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
        self.descriptor: InstrumentDescriptor | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with ScrollableContainer():
            yield Label("Loading...", id="detail-content")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._load_detail, exclusive=True)

    async def _load_detail(self) -> None:
        try:
            self.descriptor, _, _warnings = await asyncio.to_thread(fetch_instrument_by_slug, self.slug)
            self._render_detail()
        except Exception as e:
            self.query_one("#detail-content", Label).update(f"[red]Error:[/] {e}")

    def _render_detail(self) -> None:
        d = self.descriptor
        lines = [
            f"[bold]{d.name}[/] — {d.manufacturer}",
            f"Type: [cyan]{d.type}[/]  |  Interfaces: {', '.join(d.interfaces)}",
        ]
        if d.year:
            lines.append(f"Year: {d.year}")
        if d.manual:
            lines.append(f"Manual: {d.manual}")
        if d.github:
            lines.append(f"GitHub: {d.github}")
        if d.setup_steps:
            lines.append("")
            lines.append("[underline]Setup steps:[/]")
            for i, step in enumerate(d.setup_steps, 1):
                lines.append(f"  {i}. {step}")
        lines.append("")
        lines.append("Press [bold]A[/] to add this instrument to your config.")
        self.query_one("#detail-content", Label).update("\n".join(lines))

    def action_add_instrument(self) -> None:
        if self.descriptor:
            self.app.push_screen(ConfigWizardScreen(self.descriptor))


class ConfigWizardScreen(Screen):
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("ctrl+s", "submit", "Save"),
    ]

    def __init__(self, descriptor: InstrumentDescriptor) -> None:
        super().__init__()
        self.descriptor = descriptor

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(f"Configure [bold]{self.descriptor.name}[/]", id="wizard-title")
        with ScrollableContainer(id="param-fields"):
            for param in self.descriptor.params:
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

    @on(Input.Changed, ".param-input")
    def validate_field(self, event: Input.Changed) -> None:
        key = event.input.id.removeprefix("param-")
        param = next((p for p in self.descriptor.params if p.key == key), None)
        if param and event.value:
            try:
                validate_and_coerce(param, event.value)
                self.query_one(f"#err-{key}", Label).update("")
            except InvalidParamValue as e:
                self.query_one(f"#err-{key}", Label).update(f"[red]Expected {e.expected_type}[/]")

    def action_submit(self) -> None:
        self._save()

    @on(Button.Pressed, "#btn-save")
    def save_pressed(self, event: Button.Pressed) -> None:
        self._save()

    def _save(self) -> None:
        provided = {}
        for param in self.descriptor.params:
            widget = self.query_one(f"#param-{param.key}", Input)
            if widget.value.strip():
                provided[param.key] = widget.value.strip()
        try:
            resolved = build_resolved_config(self.descriptor, provided)
            path = write_config(resolved)
            self.app.push_screen(SuccessScreen(path))
        except MissingRequiredParam as e:
            for key in e.keys:
                self.query_one(f"#err-{key}", Label).update("[red]Required[/]")


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
    def done_pressed(self, event: Button.Pressed) -> None:
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
