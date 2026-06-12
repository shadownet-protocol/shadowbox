import asyncio
import hmac
from contextlib import asynccontextmanager

import mcp.types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount

from shadowbox import crypto
from shadowbox.config import Settings, ShadowConfig
from shadowbox.contacts import ContactStore, ToolError, resolve
from shadowbox.directives import DirectiveStore
from shadowbox.lab import Lab
from shadowbox.models import (
    TOOLS,
    ContextsResult,
    HistoryResult,
    IdentityResult,
    InboxResult,
    InboxWaitResult,
)

DESCRIPTIONS = {
    "identity": "This Shadow's own addressing forms, key, and credentials.",
    "resolve": "Resolve an identifier without adding it to the contact graph.",
    "contacts": "List known contacts.",
    "contact_detail": "Full record for one contact.",
    "add_contact": "Add an identifier to the contact graph.",
    "grant": "Set or clear a per-contact permission.",
    "set_contact_profile": "Replace the local-only profile on a contact.",
    "directives": "Read applicable directive layers.",
    "set_directives": "Full-replace one directive layer.",
    "send": "Send an envelope to a contact.",
    "respond": "Reply within an existing context.",
    "inbox": "Read inbound envelopes.",
    "inbox_wait": "Long-poll for new events.",
    "contexts": "List conversation contexts.",
    "history": "Read stored messages.",
}

WIRE_PENDING = {"send", "respond"}
MAX_WAIT_SECONDS = 60


class BearerAuth:
    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(auth, self.expected):
                response = PlainTextResponse("unauthorized", status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class Sidecar:
    def __init__(self, settings: Settings, config: ShadowConfig):
        self.settings = settings
        self.config = config
        self.contacts = ContactStore(settings, config.name)
        self.directives = DirectiveStore(settings, config.name)
        self._uvicorn: uvicorn.Server | None = None
        self._handlers = {
            "identity": self._identity,
            "resolve": lambda inp: resolve(inp.name),
            "contacts": lambda inp: self.contacts.contacts(inp.query),
            "contact_detail": lambda inp: self.contacts.contact_detail(inp.name),
            "add_contact": self.contacts.add_contact,
            "grant": lambda inp: self.contacts.grant(inp.name, inp.grant, inp.allowed),
            "set_contact_profile": lambda inp: self.contacts.set_contact_profile(
                inp.name, inp.profile
            ),
            "directives": lambda inp: self.directives.layers(
                inp.contact, inp.context_id
            ),
            "set_directives": self.directives.set_layer,
            "inbox": lambda inp: InboxResult(items=[], next_since=None),
            "contexts": lambda inp: ContextsResult(contexts=[], next_since=None),
            "history": lambda inp: HistoryResult(items=[], next_before=None),
        }

    def _identity(self, _inp) -> IdentityResult:
        key = crypto.load_key(self.settings.keys_dir / f"{self.config.name}.pem")
        pk = crypto.public_multibase(key)
        return IdentityResult(
            direct_uri=f"shadow://key:{pk}@localhost:{self.config.port}", pk=pk
        )

    def tool_list(self) -> list[types.Tool]:
        return [
            types.Tool(
                name=name,
                description=DESCRIPTIONS[name],
                inputSchema=inp.model_json_schema(by_alias=True),
                outputSchema=out.model_json_schema(by_alias=True),
            )
            for name, (inp, out) in TOOLS.items()
        ]

    async def call(self, name: str, arguments: dict | None) -> dict:
        if name not in TOOLS:
            raise ToolError("unknown_tool")
        if name in WIRE_PENDING:
            raise ToolError("wire_not_implemented")
        inp = TOOLS[name][0].model_validate(arguments or {})
        if name == "inbox_wait":
            await asyncio.sleep(max(0, min(inp.timeout_seconds, MAX_WAIT_SECONDS)))
            result = InboxWaitResult(events=[], next_event_id=inp.last_event_id)
        else:
            result = self._handlers[name](inp)
        return result.model_dump(mode="json", by_alias=True)

    def build_app(self):
        server = Server(f"shadownet-{self.config.name}")
        server.list_tools()(self._mcp_list_tools)
        server.call_tool()(self.call)
        manager = StreamableHTTPSessionManager(
            app=server, json_response=True, stateless=True
        )

        async def endpoint(scope, receive, send):
            await manager.handle_request(scope, receive, send)

        @asynccontextmanager
        async def lifespan(app):
            async with manager.run():
                yield

        app = Starlette(routes=[Mount("/mcp", app=endpoint)], lifespan=lifespan)
        return BearerAuth(app, self.config.token)

    async def _mcp_list_tools(self) -> list[types.Tool]:
        return self.tool_list()

    async def serve(self) -> None:
        self._uvicorn = uvicorn.Server(
            uvicorn.Config(
                self.build_app(),
                host="127.0.0.1",
                port=self.config.mcp_port,
                log_level="warning",
            )
        )
        await self._uvicorn.serve()

    def stop(self) -> None:
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        self.contacts.close()
        self.directives.close()


class SidecarFleet:
    def __init__(self, lab: Lab):
        self.lab = lab
        self._sidecars: dict[str, Sidecar] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def running(self, name: str) -> bool:
        return name in self._sidecars

    def start(self, name: str) -> Sidecar:
        if name in self._sidecars:
            return self._sidecars[name]
        shadow = self.lab.get(name)
        sidecar = Sidecar(self.lab.settings, shadow.config)
        self._sidecars[name] = sidecar
        self._tasks[name] = asyncio.get_running_loop().create_task(sidecar.serve())
        return sidecar

    def start_all(self) -> list[Sidecar]:
        return [self.start(shadow.name) for shadow in self.lab.shadows()]

    def stop_all(self) -> None:
        for sidecar in self._sidecars.values():
            sidecar.stop()
        self._sidecars.clear()
        self._tasks.clear()

    async def wait(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks.values())


def main() -> None:
    async def run() -> None:
        lab = Lab()
        st, reason = lab.state()
        if st != "ok":
            raise SystemExit(f"lab is {st}{': ' + reason if reason else ''}"
                             " — run shadowbox to initialize")
        fleet = SidecarFleet(lab)
        fleet.start_all()
        for shadow in lab.shadows():
            print(f"{shadow.name}: http://127.0.0.1:{shadow.config.mcp_port}/mcp")
        await fleet.wait()

    asyncio.run(run())