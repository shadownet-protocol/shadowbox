from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from shadowbox.data.agentcard import AgentCard
from shadowbox.data.credential import satisfies
from shadowbox.data.envelope import URN, Envelope, WireError
from shadowbox.ids import ulid
from shadowbox.models import SendResult

if TYPE_CHECKING:
    from shadowbox.shadow.shadow import Shadow

A2A_HEADERS = {
    "A2A-Extensions": URN,
    "A2A-Version": "1.0",
    "Content-Type": "application/a2a+json",
}


class Wire:
    """The A2A transport for a Shadow: serves the receive endpoint, sends envelopes."""

    def __init__(self, shadow: Shadow):
        self.shadow = shadow
        self._server: uvicorn.Server | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.shadow.config.port}"

    async def send(
        self, to: str, body: dict, context_id: str | None = None
    ) -> SendResult:
        cid = context_id or ulid()
        try:
            recipient_pk, endpoint = await self.shadow.orchestrator.discover(
                self.shadow, to
            )
        except WireError as exc:
            return SendResult(
                message_id=ulid(), context_id=cid, status="rejected", error=exc.code
            )
        now = int(time.time())
        message = Envelope.mint(
            self.shadow.signing_key,
            self.shadow.public_key.multibase,
            recipient_pk,
            body,
            cid,
            now,
            self.shadow.credentials.valid_tokens(now),
        )
        message_id = message["messageId"]
        self.shadow.messages.record_outbound(message_id, cid, recipient_pk, body)
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{endpoint}/message:send",
                    json={"message": message},
                    headers=A2A_HEADERS,
                    timeout=10,
                )
        except httpx.HTTPError:
            self.shadow.messages.set_status(message_id, "failed")
            return SendResult(
                message_id=message_id,
                context_id=cid,
                status="sending",
                error="unreachable",
            )
        if resp.status_code == 200:
            self.shadow.messages.set_status(message_id, "accepted")
            return SendResult(message_id=message_id, context_id=cid, status="accepted")
        self.shadow.messages.set_status(message_id, "failed")
        return SendResult(
            message_id=message_id,
            context_id=cid,
            status="rejected",
            error=self._error_code(resp),
        )

    async def respond(self, context_id: str, body: dict) -> SendResult:
        history = self.shadow.messages.history(context_id=context_id, limit=1)
        if not history.items:
            return SendResult(
                message_id=ulid(),
                context_id=context_id,
                status="rejected",
                error="unknown_context",
            )
        return await self.send(history.items[0].peer, body, context_id)

    @staticmethod
    def _error_code(resp: httpx.Response) -> str:
        try:
            return resp.json().get("type", "").split(":")[-1] or "policy"
        except ValueError:
            return "policy"

    def build_app(self) -> Starlette:
        return Starlette(
            routes=[
                Route(
                    "/.well-known/agent-card.json", self._agent_card, methods=["GET"]
                ),
                Route("/message:send", self._receive, methods=["POST"]),
            ]
        )

    async def _agent_card(self, _request: Request) -> Response:
        card = AgentCard.build(self.shadow.public_key.multibase, self.url).sign(
            self.shadow.signing_key
        )
        return JSONResponse(card.to_dict(), media_type="application/a2a+json")

    async def _receive(self, request: Request) -> Response:
        try:
            payload = await request.json()
            message = payload["message"]
        except (ValueError, KeyError, TypeError):
            return self._problem(WireError("parse_error", 400))
        now = int(time.time())
        try:
            envelope = Envelope.validate(message, self.shadow.public_key.multibase, now)
            if self.shadow.messages.seen(envelope.sender, envelope.message_id):
                raise WireError("replay", 409)
            route = self._classify(envelope)
            self.shadow.messages.remember(
                envelope.sender, envelope.message_id, envelope.exp
            )
            self.shadow.messages.record_inbound(
                envelope.message_id,
                envelope.context_id,
                envelope.sender,
                route,
                envelope.body,
            )
            data = {
                "messageId": envelope.message_id,
                "contextId": envelope.context_id,
                "from": envelope.sender,
                "status": route,
            }
            if envelope.body.get("intent"):
                data["intent"] = envelope.body["intent"]
            self.shadow.events.emit("inbox.message", data)
        except WireError as exc:
            return self._problem(exc)
        return JSONResponse(
            self._accept(envelope.context_id),
            media_type="application/a2a+json",
            headers={"A2A-Extensions": URN},
        )

    def _classify(self, envelope) -> str:
        detail = self.shadow.contacts.try_detail(envelope.sender)
        if detail is not None and "messaging" in detail.grants:
            return "inbox"
        if self.shadow.messages.has_recent_outbound(
            envelope.sender, envelope.context_id
        ):
            address = self.shadow.orchestrator.address_for_pk(envelope.sender)
            if address is not None:
                self.shadow.contacts.add_peer(address)
            return "inbox"
        trust = self.shadow.trust
        if satisfies(trust, envelope.creds, trust.from_stranger, int(time.time())):
            return "stranger_review"
        raise WireError("creds_rejected", 403)

    @staticmethod
    def _accept(context_id: str) -> dict:
        return {
            "message": {
                "role": "ROLE_AGENT",
                "parts": [{"text": "accepted"}],
                "messageId": ulid(),
                "contextId": context_id,
            }
        }

    @staticmethod
    def _problem(exc: WireError) -> Response:
        return JSONResponse(
            {
                "type": f"urn:shadownet:error:{exc.code}",
                "title": exc.code,
                "status": exc.status,
            },
            status_code=exc.status,
            media_type="application/problem+json",
        )

    async def serve(self) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.build_app(),
                host="127.0.0.1",
                port=self.shadow.config.port,
                log_level="warning",
            )
        )
        await self._server.serve()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
