from __future__ import annotations

import asyncio
import hmac
from contextlib import AsyncExitStack, asynccontextmanager
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
    EscalationsResult,
    Event,
    IdentityResult,
    InboxWaitResult,
    Ok,
    TasksResult,
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
    "delegate": "Delegate a peer conversation to the office.",
    "tasks": "List open delegated tasks.",
    "claim_task": "Claim a delegated task.",
    "complete_task": "Mark a delegated task complete.",
    "escalate": "Raise a decision to the principal.",
    "escalations": "List open escalations.",
    "resolve_escalation": "Answer an open escalation.",
}

ROLE_TOOLS = {
    "agent": (
        "identity",
        "resolve",
        "contacts",
        "contact_detail",
        "add_contact",
        "grant",
        "set_contact_profile",
        "directives",
        "set_directives",
        "inbox",
        "inbox_wait",
        "contexts",
        "history",
        "delegate",
        "escalations",
        "resolve_escalation",
    ),
    "office": (
        "identity",
        "resolve",
        "contact_detail",
        "directives",
        "inbox",
        "inbox_wait",
        "contexts",
        "history",
        "send",
        "respond",
        "tasks",
        "claim_task",
        "complete_task",
        "escalate",
    ),
}

ROLE_EVENTS = {
    "agent": {
        "escalation.raised",
        "review.pending",
        "task.completed",
        "directives.updated",
    },
    "office": {"inbox.message", "task.created", "escalation.resolved", "outbox.status"},
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


class _RoleMount:
    """One MCP endpoint scoped to a role's tool set and event subscriptions."""

    def __init__(self, gateway: Gateway, role: str):
        self.gateway = gateway
        self.role = role
        self.tools = set(ROLE_TOOLS[role])
        self.events = ROLE_EVENTS[role]
        self.sessions: set[ServerSession] = set()
        self.server = Server(f"shadownet-{gateway.shadow.name}-{role}")
        self.server.list_tools()(self._list_tools)
        self.server.call_tool()(self._call)
        self.manager = StreamableHTTPSessionManager(
            app=self.server, json_response=True, stateless=False
        )

    def _track(self) -> None:
        try:
            self.sessions.add(self.server.request_context.session)
        except LookupError:
            pass

    async def _list_tools(self) -> list[types.Tool]:
        self._track()
        return self.gateway.tool_list(self.tools)

    async def _call(self, name: str, arguments: dict | None) -> dict:
        self._track()
        return await self.gateway.call_as(self.role, name, arguments)

    def endpoint(self):
        async def _ep(scope, receive, send):
            await self.manager.handle_request(scope, receive, send)

        return _BearerAuth(_ep, self.gateway.shadow.token_for(self.role))

    async def push_loop(self) -> None:
        cursor = self.gateway.shadow.events.since(None)[1]
        while not self.gateway._stop:
            events, cursor = await self.gateway.shadow.events.wait(cursor, 25)
            for event in events:
                if event.event in self.events:
                    await self._push(event)

    async def _push(self, event: Event) -> None:
        notification = _Notification(
            method=f"notifications/shadownet/{event.event}",
            params={**event.data, "eventId": event.event_id},
        )
        for session in list(self.sessions):
            try:
                await session.send_notification(notification)
            except Exception:
                self.sessions.discard(session)


class Gateway:
    """The RFC 0002 MCP control plane, split into agent and office role mounts."""

    def __init__(self, shadow: Shadow):
        self.shadow = shadow
        self._server: uvicorn.Server | None = None
        self._stop = False
        self._pumps: list[asyncio.Task] = []
        self._mounts = {role: _RoleMount(self, role) for role in ("agent", "office")}
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
            "delegate": self._delegate,
            "tasks": lambda inp: TasksResult(tasks=self.shadow.tasks.open()),
            "claim_task": lambda inp: self.shadow.tasks.claim(inp.id),
            "complete_task": self._complete_task,
            "escalate": self._escalate,
            "escalations": lambda inp: EscalationsResult(
                escalations=self.shadow.escalations.open()
            ),
            "resolve_escalation": self._resolve_escalation,
        }

    def _identity(self, _inp) -> IdentityResult:
        return IdentityResult(
            direct_uri=self.shadow.uri, pk=self.shadow.public_key.multibase
        )

    def _add_contact(self, inp):
        result = self.shadow.contacts.add_contact(inp)
        for message_id, context_id, intent in self.shadow.messages.graduate(
            result.shadowname
        ):
            data = {
                "messageId": message_id,
                "contextId": context_id,
                "from": result.shadowname,
                "status": "inbox",
            }
            if intent:
                data["intent"] = intent
            self.shadow.events.emit("inbox.message", data)
        return result

    def _set_directives(self, inp):
        result = self.shadow.directives.set_layer(inp)
        data = {"scope": inp.scope}
        if inp.ref is not None:
            data["ref"] = inp.ref
        self.shadow.events.emit("directives.updated", data)
        return result

    def _delegate(self, inp):
        task = self.shadow.tasks.add(
            inp.to, inp.instruction, inp.intent, inp.context_id
        )
        self.shadow.events.emit(
            "task.created",
            {"taskId": task.id, "to": task.to_peer, "contextId": task.context_id},
        )
        return task

    def _complete_task(self, inp):
        self.shadow.tasks.complete(inp.id, inp.result)
        self.shadow.events.emit("task.completed", {"taskId": inp.id})
        return Ok()

    def _escalate(self, inp):
        escalation = self.shadow.escalations.raise_(
            inp.context_id, inp.question, inp.options
        )
        self.shadow.events.emit(
            "escalation.raised",
            {"escalationId": escalation.id, "contextId": escalation.context_id},
        )
        return escalation

    def _resolve_escalation(self, inp):
        escalation = self.shadow.escalations.resolve(inp.id, inp.decision)
        self.shadow.events.emit(
            "escalation.resolved",
            {
                "escalationId": escalation.id,
                "contextId": escalation.context_id,
                "decision": escalation.decision,
            },
        )
        return escalation

    def tool_list(self, names: set[str]) -> list[types.Tool]:
        return [
            types.Tool(
                name=name,
                description=DESCRIPTIONS[name],
                inputSchema=inp.model_json_schema(by_alias=True),
                outputSchema=out.model_json_schema(by_alias=True),
            )
            for name, (inp, out) in TOOLS.items()
            if name in names
        ]

    async def call_as(self, role: str, name: str, arguments: dict | None) -> dict:
        mount = self._mounts[role]
        if name not in mount.tools:
            raise ValueError(f"tool {name!r} is not available to the {role}")
        return await self.dispatch(name, arguments, mount.events)

    async def dispatch(
        self, name: str, arguments: dict | None, events_filter: set[str] | None = None
    ) -> dict:
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
            if events_filter is not None:
                events = [e for e in events if e.event in events_filter]
            result = InboxWaitResult(events=events, next_event_id=next_id)
        else:
            result = self._handlers[name](inp)
        return result.model_dump(mode="json", by_alias=True)

    def build_app(self) -> Starlette:
        routes = [
            Mount(f"/mcp/{role}", app=mount.endpoint())
            for role, mount in self._mounts.items()
        ]

        @asynccontextmanager
        async def lifespan(_app):
            async with AsyncExitStack() as stack:
                for mount in self._mounts.values():
                    await stack.enter_async_context(mount.manager.run())
                yield

        return Starlette(routes=routes, lifespan=lifespan)

    async def serve(self) -> None:
        self._stop = False
        loop = asyncio.get_running_loop()
        self._pumps = [loop.create_task(m.push_loop()) for m in self._mounts.values()]
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.build_app(),
                host="127.0.0.1",
                port=self.shadow.mcp_port,
                log_level="warning",
            )
        )
        await self._server.serve()

    def stop(self) -> None:
        self._stop = True
        for pump in self._pumps:
            pump.cancel()
        if self._server is not None:
            self._server.should_exit = True
