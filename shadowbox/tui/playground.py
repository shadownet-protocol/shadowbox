import asyncio

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label

from shadowbox.orchestrator import Orchestrator
from shadowbox.shadow.channel.tui import TuiChannel
from shadowbox.shadow.runtime import EmbeddedAgent
from shadowbox.tui.widgets import ChatLog


class PlaygroundScreen(Screen):
    """Four-column realtime view of two shadows' full pipeline.

    cols: A's user chat | A's office wire | B's office wire | B's user chat.
    The middle two are the same wire conversation from each office's side.
    """

    CSS = """
    #cols { height: 1fr; }
    .col { width: 1fr; border-right: solid $panel; }
    .col Label { background: $panel; text-style: bold; padding: 0 1; width: 1fr; }
    .col ChatLog { height: 1fr; padding: 0 1; }
    .col Input { dock: bottom; }
    """
    BINDINGS = [("escape", "back", "back")]

    def __init__(self, orchestrator: Orchestrator, name_a: str, name_b: str):
        super().__init__()
        self.orchestrator = orchestrator
        self.a = orchestrator.get(name_a)
        self.b = orchestrator.get(name_b)
        self.a_ch = TuiChannel()
        self.b_ch = TuiChannel()
        self.a_agent = EmbeddedAgent(self.a, self.a_ch)
        self.b_agent = EmbeddedAgent(self.b, self.b_ch)
        self._workers: list[asyncio.Task] = []
        self._seen: dict[str, set[str]] = {"a-wire": set(), "b-wire": set()}

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="cols"):
            with Vertical(classes="col"):
                yield Label(f"{self.a.name} ⇄ user")
                yield ChatLog(id="a-user")
                yield Input(placeholder=f"message {self.a.name}…", id="a-input")
            with Vertical(classes="col"):
                yield Label(f"{self.a.name} office")
                yield ChatLog(id="a-wire")
            with Vertical(classes="col"):
                yield Label(f"{self.b.name} office")
                yield ChatLog(id="b-wire")
            with Vertical(classes="col"):
                yield Label(f"{self.b.name} ⇄ user")
                yield ChatLog(id="b-user")
                yield Input(placeholder=f"message {self.b.name}…", id="b-input")
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = f"playground: {self.a.name} ⇄ {self.b.name}"
        self._spawn(self._boot())

    def _spawn(self, coro) -> None:
        self._workers.append(asyncio.get_running_loop().create_task(coro))

    def on_unmount(self) -> None:
        self.a_agent.stop()
        self.b_agent.stop()
        for worker in self._workers:
            worker.cancel()

    def action_back(self) -> None:
        self.app.pop_screen()

    async def _boot(self) -> None:
        await self.orchestrator.up(self.a.name)
        await self.orchestrator.up(self.b.name)
        self._spawn(self.a_agent.run())
        self._spawn(self.b_agent.run())
        self._spawn(self._drain(self.a_ch, "a-user", self.a.name))
        self._spawn(self._drain(self.b_ch, "b-user", self.b.name))
        self._spawn(self._wire_loop())

    async def _drain(self, channel: TuiChannel, log_id: str, name: str) -> None:
        log = self.query_one(f"#{log_id}", ChatLog)
        while True:
            log.say(name, await channel.outbound.get(), "magenta")

    async def _wire_loop(self) -> None:
        while True:
            self._render_wire(self.a, self.b, "a-wire")
            self._render_wire(self.b, self.a, "b-wire")
            await asyncio.sleep(0.4)

    def _render_wire(self, shadow, peer, log_id: str) -> None:
        log = self.query_one(f"#{log_id}", ChatLog)
        seen = self._seen[log_id]
        items = shadow.messages.history(
            contact=peer.public_key.multibase, limit=100
        ).items
        for item in reversed(items):
            if item.message_id in seen:
                continue
            seen.add(item.message_id)
            note = item.status if item.status != "inbox" else None
            log.wire(item.direction == "outbound", item.body.text or "", note)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if event.input.id == "a-input":
            self.query_one("#a-user", ChatLog).say("you", text)
            self._spawn(self.a_ch.submit(text))
        elif event.input.id == "b-input":
            self.query_one("#b-user", ChatLog).say("you", text)
            self._spawn(self.b_ch.submit(text))
