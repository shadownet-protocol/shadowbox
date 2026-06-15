from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
)

from shadowbox.config import (
    PersonaTemplate,
    ProviderCred,
    TelegramCred,
    load_personas,
    load_secrets,
)
from shadowbox.data.contacts import ToolError
from shadowbox.models import AddContactInput, ContactProfile
from shadowbox.orchestrator import Orchestrator, OrchestratorError
from shadowbox.shadow import Shadow


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "cancel")]

    def __init__(
        self,
        title: str,
        lines: list[str],
        action: str,
        variant: str = "error",
        cancel_label: str = "Cancel",
    ):
        super().__init__()
        self._title = title
        self._lines = lines
        self._action = action
        self._variant = variant
        self._cancel_label = cancel_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dialog-title")
            for line in self._lines:
                yield Static(line)
            with Horizontal(id="dialog-buttons"):
                yield Button(self._action, variant=self._variant, id="confirm")
                yield Button(self._cancel_label, id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class AddContactScreen(ModalScreen[tuple[str, str] | None]):
    BINDINGS = [("escape", "cancel", "cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("add contact", id="dialog-title")
            yield Input(placeholder="shadow://key:z6Mk...@host:port", id="uri")
            yield Input(placeholder="display name (optional)", id="display")
            with Horizontal(id="dialog-buttons"):
                yield Button("Add", variant="success", id="confirm")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "confirm":
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        uri = self.query_one("#uri", Input).value.strip()
        display = self.query_one("#display", Input).value.strip()
        if uri:
            self.dismiss((uri, display))

    def action_cancel(self) -> None:
        self.dismiss(None)


class NotesScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "cancel")]

    def __init__(self, notes: str):
        super().__init__()
        self._notes = notes

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("notes", id="dialog-title")
            yield Input(value=self._notes, id="notes")
            with Horizontal(id="dialog-buttons"):
                yield Button("Save", variant="success", id="confirm")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.dismiss(self.query_one("#notes", Input).value)
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AddShadowScreen(ModalScreen[dict | None]):
    BINDINGS = [("escape", "cancel", "cancel")]

    def __init__(
        self,
        personas: list[PersonaTemplate],
        providers: list[ProviderCred],
        telegrams: list[TelegramCred],
    ):
        super().__init__()
        self._personas = personas
        self._providers = providers
        self._telegrams = telegrams

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("new shadow", id="dialog-title")
            yield Input(placeholder="name", id="name")
            yield Select(
                [(p.display, p.id) for p in self._personas],
                prompt="persona (optional)",
                id="persona",
            )
            yield Select(
                [(f"{p.name}  [{p.kind}: {p.model}]", p.name) for p in self._providers],
                prompt="provider (optional)",
                id="provider",
            )
            yield Select(
                [(t.name, t.name) for t in self._telegrams],
                prompt="telegram (optional)",
                id="telegram",
            )
            with Horizontal(id="dialog-buttons"):
                yield Button("Create", variant="success", id="confirm")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "confirm":
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _select_value(self, select_id: str) -> str | None:
        value = self.query_one(f"#{select_id}", Select).value
        return None if value is Select.BLANK else value

    def _submit(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            return
        self.dismiss(
            {
                "name": name,
                "persona": self._select_value("persona"),
                "provider": self._select_value("provider"),
                "telegram": self._select_value("telegram"),
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class ContactsScreen(Screen):
    BINDINGS = [
        ("a", "add", "add contact"),
        ("g", "toggle_grant", "toggle messaging"),
        ("e", "edit_notes", "edit notes"),
        ("escape", "back", "back"),
    ]

    def __init__(self, shadow: Shadow):
        super().__init__()
        self.shadow = shadow

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = f"{self.shadow.name} — contacts"
        self.store = self.shadow.contacts
        table = self.query_one(DataTable)
        table.add_columns("contact", "display name", "grants")
        self._reload()
        table.focus()

    def _reload(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self.names: list[str] = []
        for c in self.store.contacts().contacts:
            self.names.append(c.shadowname)
            table.add_row(c.shadowname, c.display_name or "", ", ".join(c.grants))

    def _selected(self) -> str | None:
        table = self.query_one(DataTable)
        if not self.names or table.cursor_row >= len(self.names):
            return None
        return self.names[table.cursor_row]

    def action_back(self) -> None:
        self.app.pop_screen()

    @work
    async def action_add(self) -> None:
        result = await self.app.push_screen_wait(AddContactScreen())
        if result is None:
            return
        uri, display = result
        try:
            added = self.store.add_contact(
                AddContactInput(name=uri, display_name=display or None)
            )
        except ToolError as exc:
            self.notify(exc.code, severity="error")
            return
        self._reload()
        self.notify(f"added {added.shadowname}")

    @work
    async def action_toggle_grant(self) -> None:
        name = self._selected()
        if name is None:
            return
        detail = self.store.contact_detail(name)
        if "messaging" in detail.grants:
            ok = await self.app.push_screen_wait(
                ConfirmScreen(
                    "revoke messaging?",
                    [name, "future inbound routes to stranger review"],
                    "Revoke",
                )
            )
            if not ok:
                return
            self.store.grant(name, "messaging", False)
        else:
            self.store.grant(name, "messaging", True)
        self._reload()

    @work
    async def action_edit_notes(self) -> None:
        name = self._selected()
        if name is None:
            return
        detail = self.store.contact_detail(name)
        profile = detail.profile or ContactProfile()
        notes = await self.app.push_screen_wait(NotesScreen(profile.notes or ""))
        if notes is None:
            return
        self.store.set_contact_profile(
            name, profile.model_copy(update={"notes": notes or None})
        )
        self.notify("profile updated")


class ShadowboxApp(App):
    TITLE = "shadowbox"
    CSS = """
    ConfirmScreen, AddContactScreen, NotesScreen, AddShadowScreen {
        align: center middle;
    }
    #dialog {
        width: 90; height: auto; max-height: 80%;
        border: round $primary; padding: 1 2; background: $surface;
    }
    #dialog-title { text-style: bold; margin-bottom: 1; }
    #dialog Input { margin: 1 0; }
    #dialog Select { margin: 1 0; }
    #dialog-buttons { height: auto; align-horizontal: center; margin-top: 1; }
    #dialog-buttons Button { margin: 0 2; }
    """
    BINDINGS = [
        ("q", "quit", "quit"),
        ("n", "new_shadow", "new shadow"),
        ("s", "toggle_agent", "host LLM"),
        ("r", "reinit", "reinitialize"),
    ]

    def __init__(self):
        super().__init__()
        self.orchestrator = Orchestrator()

    def compose(self) -> ComposeResult:
        yield Header()
        yield ListView()
        yield Footer()

    def on_mount(self) -> None:
        self._start()

    @work
    async def _start(self) -> None:
        st, reason = self.orchestrator.state()
        if st == "fresh":
            ok = await self.push_screen_wait(
                ConfirmScreen(
                    "shadowbox is not initialized.",
                    self.orchestrator.plan(),
                    "Initialize",
                    variant="success",
                    cancel_label="Quit",
                )
            )
            if not ok:
                self.exit()
                return
            self.orchestrator.initialize()
        elif st == "broken":
            ok = await self.push_screen_wait(
                ConfirmScreen(
                    f"state at {self.orchestrator.settings.home_dir} broken: {reason}",
                    [
                        "reinitializing permanently deletes:",
                        "  keys/  (identities are unrecoverable)",
                        "  config.yaml, shadowbox.db, hermes/",
                        "personas.yaml and secrets.yaml are kept",
                    ],
                    "Delete and reinitialize",
                    cancel_label="Quit",
                )
            )
            if not ok:
                self.exit()
                return
            self.orchestrator.wipe()
            self.orchestrator.initialize()
        self.orchestrator.start_all()
        self._load_shadows()

    def on_unmount(self) -> None:
        self.orchestrator.stop_all()

    def _load_shadows(self) -> None:
        lv = self.query_one(ListView)
        lv.clear()
        for shadow in self.orchestrator.shadows:
            extras = " / ".join(
                v
                for v in (shadow.config.persona, shadow.config.provider)
                if v is not None
            )
            mark = "●" if self.orchestrator.running(shadow.name) else "○"
            detail = f"mcp :{shadow.config.mcp_port} {mark}"
            if shadow.config.provider is not None:
                detail += f"  llm:{self.orchestrator.agent_status(shadow.name)}"
            if extras:
                detail += f"  {extras}"
            item = ListItem(
                Label(f"[b]{shadow.name}[/b]  {shadow.uri}  [dim]{detail}[/dim]")
            )
            item.shadow_name = shadow.name
            lv.append(item)
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.push_screen(ContactsScreen(self.orchestrator.get(event.item.shadow_name)))

    @work
    async def action_toggle_agent(self) -> None:
        item = self.query_one(ListView).highlighted_child
        if item is None:
            return
        name = item.shadow_name
        if self.orchestrator.get(name).config.provider is None:
            self.notify("no host LLM configured for this shadow", severity="warning")
            return
        try:
            if self.orchestrator.agent_status(name) == "running":
                await self.orchestrator.stop_agent(name)
                self.notify(f"{name} host LLM stopped")
            else:
                await self.orchestrator.start_agent(name)
                self.notify(f"{name} host LLM started")
        except RuntimeError as exc:
            self.notify(str(exc), severity="error")
        self._load_shadows()

    @work
    async def action_new_shadow(self) -> None:
        try:
            personas = load_personas(self.orchestrator.settings).personas
            secrets = load_secrets(self.orchestrator.settings)
        except FileNotFoundError:
            self.notify("lab not initialized", severity="error")
            return
        result = await self.push_screen_wait(
            AddShadowScreen(personas, secrets.providers, secrets.telegram)
        )
        if result is None:
            return
        try:
            shadow, lines = self.orchestrator.add_shadow(**result)
        except OrchestratorError as exc:
            self.notify(str(exc), severity="error")
            return
        self.orchestrator.start(shadow.name)
        self._load_shadows()
        self.notify("\n".join(lines), markup=False)

    @work
    async def action_reinit(self) -> None:
        ok = await self.push_screen_wait(
            ConfirmScreen(
                "delete and reinitialize?",
                [
                    f"permanently deletes from {self.orchestrator.settings.home_dir}:",
                    "  keys/  (identities are unrecoverable)",
                    "  config.yaml, shadowbox.db, hermes/",
                    "personas.yaml and secrets.yaml are kept",
                ],
                "Delete and reinitialize",
            )
        )
        if not ok:
            return
        self.orchestrator.stop_all()
        self.orchestrator.wipe()
        self.orchestrator.initialize()
        self.orchestrator.start_all()
        self._load_shadows()
        self.notify("reinitialized")


def main() -> None:
    ShadowboxApp().run()


if __name__ == "__main__":
    main()
