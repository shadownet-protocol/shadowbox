import asyncio

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Label, ListItem, ListView, RichLog, Static

from shadowbox.config import load_personas, load_secrets
from shadowbox.orchestrator import Orchestrator, OrchestratorError
from shadowbox.tui.modals import CSS as MODAL_CSS
from shadowbox.tui.modals import AddShadowScreen, ConfirmScreen
from shadowbox.tui.screens import ShadowScreen


def lamp(up: bool) -> str:
    return "[green]●[/]" if up else "[red]○[/]"


def agent_lamp(status: str | None) -> str:
    if status is None:
        return "[dim]·[/]"
    return "[green]●[/]" if status == "running" else "[red]○[/]"


def row_markup(name: str, sub: dict, persona: str | None) -> str:
    lamps = f"G{lamp(sub['gateway'])} A{lamp(sub['a2a'])} L{agent_lamp(sub['agent'])}"
    tail = f"  [dim]{persona}[/dim]" if persona else ""
    return f"[b]{name:<10}[/b] {lamps}{tail}"


class ShadowboxApp(App):
    TITLE = "shadowbox"
    CSS = (
        MODAL_CSS
        + """
    #top { height: 1fr; }
    #shadows { width: 45%; border-right: solid $panel; }
    #details { width: 1fr; padding: 1 2; }
    #events { height: 9; border-top: solid $panel; }
    #stats { padding: 1 2; height: auto; }
    """
    )
    BINDINGS = [
        ("u", "up", "up"),
        ("d", "down", "down"),
        ("n", "new_shadow", "new"),
        ("w", "wipe", "wipe"),
        ("r", "reinit", "reinit"),
        ("q", "quit", "quit"),
    ]

    def __init__(self):
        super().__init__()
        self.orchestrator = Orchestrator()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="top"):
            yield ListView(id="shadows")
            yield Static(id="details")
        yield RichLog(id="events", markup=True)
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
                        "reinitializing permanently deletes keys/, config.yaml,",
                        "shadowbox.db, trust.yaml, hermes/  (personas/secrets kept)",
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
        self._load()
        self._feed()

    def on_unmount(self) -> None:
        self.orchestrator.stop_all()

    def _highlighted(self) -> str | None:
        item = self.query_one("#shadows", ListView).highlighted_child
        return item.shadow_name if item is not None else None

    def _load(self) -> None:
        lv = self.query_one("#shadows", ListView)
        index = lv.index
        lv.clear()
        for shadow in self.orchestrator.shadows:
            sub = self.orchestrator.subsystems(shadow.name)
            item = ListItem(Label(row_markup(shadow.name, sub, shadow.persona)))
            item.shadow_name = shadow.name
            lv.append(item)
        if lv.children:
            lv.index = min(index, len(lv.children) - 1) if index is not None else 0
        lv.focus()
        self._show_details(self._highlighted())

    def _refresh_rows(self) -> None:
        for item in self.query_one("#shadows", ListView).children:
            name = getattr(item, "shadow_name", None)
            if name is None:
                continue
            sub = self.orchestrator.subsystems(name)
            persona = self.orchestrator.get(name).persona
            item.query_one(Label).update(row_markup(name, sub, persona))

    def _show_details(self, name: str | None) -> None:
        details = self.query_one("#details", Static)
        if name is None:
            details.update("no shadows")
            return
        shadow = self.orchestrator.get(name)
        sub = self.orchestrator.subsystems(name)
        contacts = len(shadow.contacts.contacts().contacts)
        reviews = len(
            [
                i
                for i in shadow.messages.inbox(include_review=True).items
                if i.status == "stranger_review"
            ]
        )
        gw = "mcp✓ sse✓" if sub["gateway"] else "down"
        details.update(
            f"[b]{name}[/b]\n"
            f"gateway :{shadow.mcp_port}  {gw}\n"
            f"a2a     :{shadow.port}  {'up' if sub['a2a'] else 'down'}\n"
            f"agent   {sub['agent'] or 'n/a'}\n"
            f"key     {shadow.public_key.multibase[:16]}…\n"
            f"{contacts} contacts · {reviews} review"
        )

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        name = getattr(event.item, "shadow_name", None) if event.item else None
        self._show_details(name)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        name = getattr(event.item, "shadow_name", None)
        if name is not None:
            self.push_screen(
                ShadowScreen(self.orchestrator, self.orchestrator.get(name))
            )

    @work(exclusive=True)
    async def _feed(self) -> None:
        log = self.query_one("#events", RichLog)
        cursors: dict[str, str | None] = {}
        while True:
            for line in self.orchestrator.drain_log():
                log.write(f"[dim]·[/] {line}")
            for shadow in self.orchestrator.shadows:
                events, cursors[shadow.name] = shadow.events.since(
                    cursors.get(shadow.name)
                )
                for e in events:
                    summary = e.data.get("from", e.data.get("scope", ""))
                    log.write(
                        f"{e.occurred_at[11:]} [b]{shadow.name}[/b]"
                        f"  {e.event}  [dim]{summary}[/dim]"
                    )
            self._refresh_rows()
            if self.screen is self:
                self._show_details(self._highlighted())
            await asyncio.sleep(0.6)

    @work
    async def action_up(self) -> None:
        name = self._highlighted()
        if name is not None:
            await self.orchestrator.up(name)
            self._refresh_rows()

    @work
    async def action_down(self) -> None:
        name = self._highlighted()
        if name is not None:
            await self.orchestrator.down(name)
            self._refresh_rows()

    @work
    async def action_new_shadow(self) -> None:
        try:
            personas = load_personas(self.orchestrator.settings).personas
            secrets = load_secrets(self.orchestrator.settings)
        except FileNotFoundError:
            self.notify("lab not initialized", severity="error")
            return
        existing = {s.name for s in self.orchestrator.shadows}
        n = len(existing) + 1
        while f"shadow{n}" in existing:
            n += 1
        result = await self.push_screen_wait(
            AddShadowScreen(f"shadow{n}", personas, secrets.providers, secrets.telegram)
        )
        if result is None:
            return
        try:
            shadow, lines = self.orchestrator.add_shadow(**result)
        except OrchestratorError as exc:
            self.notify(str(exc), severity="error")
            return
        await self.orchestrator.up(shadow.name)
        self._load()
        self.notify("\n".join(lines), markup=False)

    @work
    async def action_wipe(self) -> None:
        name = self._highlighted()
        if name is None:
            return
        ok = await self.push_screen_wait(
            ConfirmScreen(
                f"wipe {name}?",
                ["permanently deletes its key, contacts, messages, creds, hermes home"],
                "Wipe shadow",
            )
        )
        if not ok:
            return
        await self.orchestrator.remove_shadow(name)
        self._load()

    @work
    async def action_reinit(self) -> None:
        ok = await self.push_screen_wait(
            ConfirmScreen(
                "delete and reinitialize the whole lab?",
                [
                    "permanently deletes keys/, config.yaml, shadowbox.db, trust.yaml,"
                    " hermes/  (personas/secrets kept)"
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
        self._load()
        self.notify("reinitialized")


def main() -> None:
    ShadowboxApp().run()


if __name__ == "__main__":
    main()
