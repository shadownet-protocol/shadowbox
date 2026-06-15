from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import mcp.types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.session import ServerSession
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount

from shadowbox.data.contacts import resolve
from shadowbox.models import (
    TOOLS,
    Event,
    IdentityResult,
    InboxWaitResult,
)

if TYPE_CHECKING:
    from shadowbox.shadow.shadow import Shadow


class _Notification(BaseModel):
    method: str
    params: dict


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
        self._mcp: Server | None = None
        self._sessions: set[ServerSession] = set()
        self._pump: asyncio.Task | None = None
        self._stop = False
        self._handlers = {
            "identity": self._identity,
            "resolve": lambda inp: resolve(inp.name),
            "contacts": lambda inp: self.shadow.contacts.contacts(inp.query),
            "contact_detail": lambda inp: self.shadow.contacts.contact_detail(inp.name),
            "add_contact": self._add_contact,
            "grant": lambda inp: self.shadow.contacts.grant(
                inp.name, inp.grant, inp.allowed
            ),
            "set_contact_profile": lambda inp: self.shadow.contacts.set_contact_profile(
                inp.name, inp.profile
            ),
            "directives": lambda inp: self.shadow.directives.layers(
                inp.contact, inp.context_id
            ),
            "set_directives": self._set_directives,
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

    def _add_contact(self, inp):
        result = self.shadow.contacts.add_contact(inp)
        self.shadow.messages.graduate(result.shadowname)
        return result

    def _set_directives(self, inp):
        result = self.shadow.directives.set_layer(inp)
        data = {"scope": inp.scope}
        if inp.ref is not None:
            data["ref"] = inp.ref
        self.shadow.events.emit("directives.updated", data)
        return result

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

    def _track_session(self) -> None:
        if self._mcp is None:
            return
        try:
            self._sessions.add(self._mcp.request_context.session)
        except LookupError:
            pass

    async def call(self, name: str, arguments: dict | None) -> dict:
        self._track_session()
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
            events, next_id = await self.shadow.events.wait(
                inp.last_event_id, inp.timeout_seconds
            )
            result = InboxWaitResult(events=events, next_event_id=next_id)
        else:
            result = self._handlers[name](inp)
        return result.model_dump(mode="json", by_alias=True)

    def build_app(self) -> _BearerAuth:
        self._mcp = Server(f"shadownet-{self.shadow.name}")
        self._mcp.list_tools()(self._list_tools)
        self._mcp.call_tool()(self.call)
        manager = StreamableHTTPSessionManager(
            app=self._mcp, json_response=True, stateless=False
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
        self._track_session()
        return self.tool_list()

    async def _push_loop(self) -> None:
        cursor = self.shadow.events.since(None)[1]
        while not self._stop:
            events, cursor = await self.shadow.events.wait(cursor, 25)
            for event in events:
                await self._push(event)

    async def _push(self, event: Event) -> None:
        notification = _Notification(
            method=f"notifications/shadownet/{event.event}",
            params={**event.data, "eventId": event.event_id},
        )
        for session in list(self._sessions):
            try:
                await session.send_notification(notification)
            except Exception:
                self._sessions.discard(session)

    async def serve(self) -> None:
        self._pump = asyncio.get_running_loop().create_task(self._push_loop())
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
        self._stop = True
        if self._pump is not None:
            self._pump.cancel()
        if self._server is not None:
            self._server.should_exit = True
