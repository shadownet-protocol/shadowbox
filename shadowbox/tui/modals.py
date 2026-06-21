from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from shadowbox.config import PersonaTemplate, ProviderCred, TelegramCred

CSS = """
ConfirmScreen, AddContactScreen, NotesScreen, AddShadowScreen {
    align: center middle;
}
#dialog {
    width: 90; height: auto; max-height: 90%; overflow-y: auto;
    border: round $primary; padding: 1 2; background: $surface;
}
#dialog-title { text-style: bold; margin-bottom: 1; }
#dialog Input, #dialog Select { margin-bottom: 1; }
#dialog-buttons { height: auto; align-horizontal: center; margin-top: 1; }
#dialog-buttons Button { margin: 0 2; }
"""


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
        next_name: str,
        personas: list[PersonaTemplate],
        providers: list[ProviderCred],
        telegrams: list[TelegramCred],
        current: dict | None = None,
    ):
        super().__init__()
        self._next_name = next_name
        self._personas = personas
        self._providers = providers
        self._telegrams = telegrams
        self._current = current or {}
        self._editing = current is not None

    def _preset(self, key: str) -> dict:
        value = self._current.get(key)
        return {"value": value} if value else {}

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(
                f"edit shadow: {self._next_name}" if self._editing else "new shadow",
                id="dialog-title",
            )
            yield Input(
                value=self._next_name,
                placeholder="shadow id",
                id="name",
                disabled=self._editing,
            )
            yield Select(
                [(p.display, p.id) for p in self._personas],
                prompt="persona (optional)",
                id="persona",
                **self._preset("persona"),
            )
            yield Select(
                [(f"{p.name}  [{p.kind}: {p.model}]", p.name) for p in self._providers],
                prompt="provider (optional)",
                id="provider",
                **self._preset("provider"),
            )
            yield Select(
                [(t.name, t.name) for t in self._telegrams],
                prompt="telegram (optional)",
                id="telegram",
                **self._preset("telegram"),
            )
            with Horizontal(id="dialog-buttons"):
                yield Button("OK", variant="success", id="confirm")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "confirm":
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _select(self, select_id: str) -> str | None:
        value = self.query_one(f"#{select_id}", Select).value
        return value if isinstance(value, str) else None

    def _submit(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            return
        self.dismiss(
            {
                "name": name,
                "persona": self._select("persona"),
                "provider": self._select("provider"),
                "telegram": self._select("telegram"),
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class AddProviderScreen(ModalScreen[dict | None]):
    BINDINGS = [("escape", "cancel", "cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("add provider", id="dialog-title")
            yield Input(placeholder="name (e.g. or-main)", id="name")
            yield Select(
                [(k, k) for k in ("anthropic", "openai-api", "openrouter")],
                prompt="kind",
                id="kind",
            )
            yield Input(placeholder="model id", id="model")
            yield Input(placeholder="API key", password=True, id="api_key")
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
        name = self.query_one("#name", Input).value.strip()
        kind = self.query_one("#kind", Select).value
        model = self.query_one("#model", Input).value.strip()
        api_key = self.query_one("#api_key", Input).value.strip()
        if name and isinstance(kind, str) and model and api_key:
            self.dismiss(
                {"name": name, "kind": kind, "model": model, "api_key": api_key}
            )

    def action_cancel(self) -> None:
        self.dismiss(None)


class AddTelegramScreen(ModalScreen[dict | None]):
    BINDINGS = [("escape", "cancel", "cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("add telegram bot", id="dialog-title")
            yield Input(placeholder="name (e.g. my-bot)", id="name")
            yield Input(placeholder="bot token", password=True, id="token")
            yield Input(placeholder="allowed chat IDs, comma-separated", id="users")
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
        name = self.query_one("#name", Input).value.strip()
        token = self.query_one("#token", Input).value.strip()
        users = [
            u.strip()
            for u in self.query_one("#users", Input).value.split(",")
            if u.strip()
        ]
        if name and token:
            self.dismiss({"name": name, "token": token, "allowed_users": users})

    def action_cancel(self) -> None:
        self.dismiss(None)
