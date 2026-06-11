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
    Static,
)

from shadowbox import crypto
from shadowbox.config import Settings, load_config
from shadowbox.contacts import ContactStore, ToolError
from shadowbox.init import initialize, plan, state, wipe
from shadowbox.models import AddContactInput, ContactProfile


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


class ContactsScreen(Screen):
    BINDINGS = [
        ("a", "add", "add contact"),
        ("g", "toggle_grant", "toggle messaging"),
        ("e", "edit_notes", "edit notes"),
        ("escape", "back", "back"),
    ]

    def __init__(self, persona: str):
        super().__init__()
        self.persona = persona

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = f"{self.persona} — contacts"
        self.store = ContactStore(Settings(), self.persona)
        table = self.query_one(DataTable)
        table.add_columns("contact", "display name", "grants")
        self._reload()
        table.focus()

    def on_unmount(self) -> None:
        self.store.close()

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
    ConfirmScreen, AddContactScreen, NotesScreen { align: center middle; }
    #dialog {
        width: 90; height: auto; max-height: 80%;
        border: round $primary; padding: 1 2; background: $surface;
    }
    #dialog-title { text-style: bold; margin-bottom: 1; }
    #dialog Input { margin: 1 0; }
    #dialog-buttons { height: auto; align-horizontal: center; margin-top: 1; }
    #dialog-buttons Button { margin: 0 2; }
    """
    BINDINGS = [("q", "quit", "quit"), ("r", "reinit", "reinitialize")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield ListView()
        yield Footer()

    def on_mount(self) -> None:
        self._start()

    @work
    async def _start(self) -> None:
        settings = Settings()
        st, reason = state(settings)
        if st == "fresh":
            ok = await self.push_screen_wait(
                ConfirmScreen(
                    "shadowbox is not initialized.",
                    plan(settings),
                    "Initialize",
                    variant="success",
                    cancel_label="Quit",
                )
            )
            if not ok:
                self.exit()
                return
            initialize(settings)
        elif st == "broken":
            ok = await self.push_screen_wait(
                ConfirmScreen(
                    f"state at {settings.home_dir} is broken: {reason}",
                    [
                        "reinitializing permanently deletes:",
                        "  keys/  (identities are unrecoverable)",
                        "  config.yaml",
                        "  shadowbox.db",
                    ],
                    "Delete and reinitialize",
                    cancel_label="Quit",
                )
            )
            if not ok:
                self.exit()
                return
            wipe(settings)
            initialize(settings)
        self._load_personas(settings)

    def _load_personas(self, settings: Settings) -> None:
        lv = self.query_one(ListView)
        lv.clear()
        for p in load_config(settings).personas:
            key = crypto.load_key(settings.keys_dir / f"{p.name}.pem")
            uri = f"shadow://key:{crypto.public_multibase(key)}@localhost:{p.port}"
            item = ListItem(Label(f"[b]{p.name}[/b]  {uri}"))
            item.persona_name = p.name
            lv.append(item)
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.push_screen(ContactsScreen(event.item.persona_name))

    @work
    async def action_reinit(self) -> None:
        settings = Settings()
        ok = await self.push_screen_wait(
            ConfirmScreen(
                "delete and reinitialize?",
                [
                    f"permanently deletes from {settings.home_dir}:",
                    "  keys/  (identities are unrecoverable)",
                    "  config.yaml",
                    "  shadowbox.db",
                ],
                "Delete and reinitialize",
            )
        )
        if not ok:
            return
        wipe(settings)
        initialize(settings)
        self._load_personas(settings)
        self.notify("reinitialized")


def main() -> None:
    ShadowboxApp().run()


if __name__ == "__main__":
    main()