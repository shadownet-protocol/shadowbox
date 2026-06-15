from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import mcp.types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount

from shadowbox.data.contacts import resolve
from shadowbox.models import (
    TOOLS,
    IdentityResult,
    InboxWaitResult,
)

if TYPE_CHECKING:
    from shadowbox.shadow.shadow import Shadow

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

MAX_WAIT_SECONDS = 60


class _BearerAuth:
    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(auth, self.expected):
                await PlainTextResponse("unauthorized", status_code=401)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


class Gateway:
    """The RFC 0002 MCP control plane a Shadow's host LLM connects to."""

    def __init__(self, shadow: Shadow):
        self.shadow = shadow
        self._server: uvicorn.Server | None = None
        self._handlers = {
            "identity": self._identity,
            "resolve": lambda inp: resolve(inp.name),
            "contacts": lambda inp: self.shadow.contacts.contacts(inp.query),
            "contact_detail": lambda inp: self.shadow.contacts.contact_detail(inp.name),
            "add_contact": lambda inp: self.shadow.contacts.add_contact(inp),
            "grant": lambda inp: self.shadow.contacts.grant(
                inp.name, inp.grant, inp.allowed
            ),
            "set_contact_profile": lambda inp: self.shadow.contacts.set_contact_profile(
                inp.name, inp.profile
            ),
            "directives": lambda inp: self.shadow.directives.layers(
                inp.contact, inp.context_id
            ),
            "set_directives": lambda inp: self.shadow.directives.set_layer(inp),
            "inbox": lambda inp: self.shadow.messages.inbox(
                inp.since, inp.contact, inp.intent, inp.include_review, inp.limit
            ),
            "contexts": lambda inp: self.shadow.messages.contexts(
                inp.contact, inp.include_review, inp.since, inp.limit
            ),
            "history": lambda inp: self.shadow.messages.history(
                inp.context_id,
                inp.contact,
                inp.intent,
                inp.include_review,
                inp.before,
                inp.limit,
            ),
        }

    def _identity(self, _inp) -> IdentityResult:
        return IdentityResult(
            direct_uri=self.shadow.uri, pk=self.shadow.public_key.multibase
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
        inp = TOOLS[name][0].model_validate(arguments or {})
        if name == "send":
            result = await self.shadow.wire.send(
                inp.to,
                inp.body.model_dump(by_alias=True, exclude_none=True),
                inp.context_id,
            )
        elif name == "respond":
            result = await self.shadow.wire.respond(
                inp.context_id, inp.body.model_dump(by_alias=True, exclude_none=True)
            )
        elif name == "inbox_wait":
            await asyncio.sleep(max(0, min(inp.timeout_seconds, MAX_WAIT_SECONDS)))
            result = InboxWaitResult(events=[], next_event_id=inp.last_event_id)
        else:
            result = self._handlers[name](inp)
        return result.model_dump(mode="json", by_alias=True)

    def build_app(self) -> _BearerAuth:
        server = Server(f"shadownet-{self.shadow.name}")
        server.list_tools()(self._list_tools)
        server.call_tool()(self.call)
        manager = StreamableHTTPSessionManager(
            app=server, json_response=True, stateless=True
        )

        async def endpoint(scope, receive, send):
            await manager.handle_request(scope, receive, send)

        @asynccontextmanager
        async def lifespan(_app):
            async with manager.run():
                yield

        app = Starlette(routes=[Mount("/mcp", app=endpoint)], lifespan=lifespan)
        return _BearerAuth(app, self.shadow.config.token)

    async def _list_tools(self) -> list[types.Tool]:
        return self.tool_list()

    async def serve(self) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.build_app(),
                host="127.0.0.1",
                port=self.shadow.config.mcp_port,
                log_level="warning",
            )
        )
        await self._server.serve()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
