import json
import time

from textual import work
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from shadowbox.data.agentcard import AgentCard
from shadowbox.data.contacts import ToolError
from shadowbox.data.credential import Credential
from shadowbox.models import AddContactInput, ContactProfile
from shadowbox.orchestrator import Orchestrator
from shadowbox.shadow import Shadow
from shadowbox.tui.modals import (
    AddContactScreen,
    AddProviderScreen,
    AddTelegramScreen,
    ConfirmScreen,
    NotesScreen,
)


class ShadowScreen(Screen):
    BINDINGS = [
        ("a", "add", "add contact"),
        ("g", "toggle_grant", "toggle messaging"),
        ("e", "edit_notes", "edit notes"),
        ("w", "wipe", "wipe shadow"),
        ("escape", "back", "back"),
    ]

    def __init__(self, orchestrator: Orchestrator, shadow: Shadow):
        super().__init__()
        self.orchestrator = orchestrator
        self.shadow = shadow

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="stats")
        with TabbedContent():
            with TabPane("contacts", id="tab-contacts"):
                yield DataTable(id="contacts", cursor_type="row")
            with TabPane("reviews", id="tab-reviews"):
                yield DataTable(id="reviews", cursor_type="row")
            with TabPane("threads", id="tab-threads"):
                yield DataTable(id="threads", cursor_type="row")
            with TabPane("directives", id="tab-directives"):
                yield RichLog(id="directives", markup=True, wrap=True)
            with TabPane("creds", id="tab-creds"):
                yield RichLog(id="creds", markup=True, wrap=True)
            with TabPane("card", id="tab-card"):
                yield RichLog(id="card", wrap=True)
            with TabPane("log", id="tab-log"):
                yield RichLog(id="log", wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = self.shadow.name
        self.query_one("#contacts", DataTable).add_columns("contact", "name", "grants")
        self.query_one("#reviews", DataTable).add_columns("from", "intent", "received")
        self.query_one("#threads", DataTable).add_columns("context", "peers", "last")
        self._reload()
        self.query_one("#contacts", DataTable).focus()

    def _reload(self) -> None:
        s = self.shadow
        contacts = s.contacts.contacts().contacts
        review = [
            i
            for i in s.messages.inbox(include_review=True).items
            if i.status == "stranger_review"
        ]
        threads = s.messages.contexts(include_review=True).contexts
        creds = s.credentials.valid_tokens(int(time.time()))
        self.query_one("#stats", Static).update(
            f"[b]{s.name}[/b]  {s.uri}\n"
            f"{len(contacts)} contacts · {len(review)} reviews ·"
            f" {len(threads)} threads · {len(creds)} creds"
        )

        ct = self.query_one("#contacts", DataTable)
        ct.clear()
        self.contact_names = []
        for c in contacts:
            self.contact_names.append(c.shadowname)
            ct.add_row(c.shadowname, c.display_name or "", ", ".join(c.grants))

        rt = self.query_one("#reviews", DataTable)
        rt.clear()
        for i in review:
            rt.add_row(i.from_, i.body.intent or "", i.received_at)

        tt = self.query_one("#threads", DataTable)
        tt.clear()
        for c in threads:
            tt.add_row(
                c.context_id, ", ".join(p[:10] for p in c.peers), c.last_message_at
            )

        dlog = self.query_one("#directives", RichLog)
        dlog.clear()
        for layer in s.directives.layers().directives:
            dlog.write(f"[b]{layer.scope}[/b]")
            for item in layer.items:
                dlog.write(f"  • {item.text}")

        clog = self.query_one("#creds", RichLog)
        clog.clear()
        for token in creds:
            c = Credential.validate(token, int(time.time()))
            clog.write(f"{c.kind}  iss {c.iss[:14]}…  exp {c.exp}")

        card = AgentCard.build(s.public_key.multibase, s.wire.url, s.name).sign(
            s.signing_key
        )
        cardlog = self.query_one("#card", RichLog)
        cardlog.clear()
        cardlog.write(json.dumps(card.to_dict(), indent=2))

        loglog = self.query_one("#log", RichLog)
        loglog.clear()
        loglog.write(s.agent.log_tail() or "(no host LLM log)")

    def _selected_contact(self) -> str | None:
        table = self.query_one("#contacts", DataTable)
        if not self.contact_names or table.cursor_row >= len(self.contact_names):
            return None
        return self.contact_names[table.cursor_row]

    def action_back(self) -> None:
        self.app.pop_screen()

    @work
    async def action_add(self) -> None:
        result = await self.app.push_screen_wait(AddContactScreen())
        if result is None:
            return
        uri, display = result
        try:
            self.shadow.contacts.add_contact(
                AddContactInput(name=uri, display_name=display or None)
            )
            self.shadow.messages.graduate(uri)
        except ToolError as exc:
            self.notify(exc.code, severity="error")
            return
        self._reload()

    @work
    async def action_toggle_grant(self) -> None:
        name = self._selected_contact()
        if name is None:
            return
        detail = self.shadow.contacts.contact_detail(name)
        self.shadow.contacts.grant(name, "messaging", "messaging" not in detail.grants)
        self._reload()

    @work
    async def action_edit_notes(self) -> None:
        name = self._selected_contact()
        if name is None:
            return
        detail = self.shadow.contacts.contact_detail(name)
        profile = detail.profile or ContactProfile()
        notes = await self.app.push_screen_wait(NotesScreen(profile.notes or ""))
        if notes is None:
            return
        self.shadow.contacts.set_contact_profile(
            name, profile.model_copy(update={"notes": notes or None})
        )
        self.notify("profile updated")

    @work
    async def action_wipe(self) -> None:
        ok = await self.app.push_screen_wait(
            ConfirmScreen(
                f"wipe {self.shadow.name}?",
                ["permanently deletes its key, contacts, messages, creds, hermes home"],
                "Wipe shadow",
            )
        )
        if not ok:
            return
        await self.orchestrator.remove_shadow(self.shadow.name)
        self.app.pop_screen()


class SettingsScreen(Screen):
    BINDINGS = [
        ("p", "add_provider", "add provider"),
        ("t", "add_telegram", "add telegram"),
        ("x", "remove", "remove selected"),
        ("escape", "back", "back"),
    ]

    def __init__(self, orchestrator: Orchestrator):
        super().__init__()
        self.orchestrator = orchestrator

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "providers & telegram bots — picked when you create or edit a shadow",
            id="stats",
        )
        with TabbedContent():
            with TabPane("providers", id="tab-providers"):
                yield DataTable(id="providers", cursor_type="row")
            with TabPane("telegram", id="tab-telegram"):
                yield DataTable(id="telegram", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = "settings"
        self.query_one("#providers", DataTable).add_columns("name", "kind", "model")
        self.query_one("#telegram", DataTable).add_columns("name", "chat IDs")
        self._reload()
        self.query_one("#providers", DataTable).focus()

    def _reload(self) -> None:
        pt = self.query_one("#providers", DataTable)
        pt.clear()
        self.provider_names = []
        for p in self.orchestrator.providers():
            self.provider_names.append(p.name)
            pt.add_row(p.name, p.kind, p.model)
        tt = self.query_one("#telegram", DataTable)
        tt.clear()
        self.telegram_names = []
        for t in self.orchestrator.telegrams():
            self.telegram_names.append(t.name)
            tt.add_row(t.name, ", ".join(t.allowed_users) or "(any)")

    def action_back(self) -> None:
        self.app.pop_screen()

    @work
    async def action_add_provider(self) -> None:
        result = await self.app.push_screen_wait(AddProviderScreen())
        if result is None:
            return
        self.orchestrator.add_provider(**result)
        self._reload()

    @work
    async def action_add_telegram(self) -> None:
        result = await self.app.push_screen_wait(AddTelegramScreen())
        if result is None:
            return
        self.orchestrator.add_telegram(**result)
        self._reload()

    @work
    async def action_remove(self) -> None:
        active = self.query_one(TabbedContent).active
        if active == "tab-providers":
            table = self.query_one("#providers", DataTable)
            names, kind, remove = (
                self.provider_names,
                "provider",
                (self.orchestrator.remove_provider),
            )
        else:
            table = self.query_one("#telegram", DataTable)
            names, kind, remove = (
                self.telegram_names,
                "telegram bot",
                (self.orchestrator.remove_telegram),
            )
        if not names or table.cursor_row >= len(names):
            return
        name = names[table.cursor_row]
        ok = await self.app.push_screen_wait(
            ConfirmScreen(
                f"remove {kind} {name}?",
                ["shadows already using it keep their copy until reconfigured"],
                "Remove",
            )
        )
        if not ok:
            return
        remove(name)
        self._reload()
