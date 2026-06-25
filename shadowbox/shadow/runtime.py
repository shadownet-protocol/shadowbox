from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from shadowbox.shadow.channel.base import Channel

if TYPE_CHECKING:
    from shadowbox.shadow.shadow import Shadow

_AFFIRMATIVE = {"yes", "y", "approve", "ok", "sure", "accept", "yep"}


class AgentBrain(ABC):
    """Turns a user message into a reply, acting through the agent's MCP tools."""

    @abstractmethod
    async def turn(self, agent: EmbeddedAgent, text: str) -> str: ...


class ScriptedAgentBrain(AgentBrain):
    """Keyless stand-in: `ask <name>: <message>` delegates to the office."""

    async def turn(self, agent: EmbeddedAgent, text: str) -> str:
        if text.lower().startswith("ask ") and ":" in text:
            target, message = text[4:].split(":", 1)
            target, message = target.strip(), message.strip()
            contacts = (await agent.call("contacts", {}))["contacts"]
            match = next(
                (
                    c
                    for c in contacts
                    if (c.get("displayName") or "").lower() == target.lower()
                    or c["shadowname"].startswith(target)
                ),
                None,
            )
            if match is None:
                return f"I don't have a contact called {target}."
            await agent.call(
                "delegate", {"to": match["shadowname"], "instruction": message}
            )
            return f"Okay — I asked {target}."
        return f"(noted: {text})"


class HermesBrain(AgentBrain):
    """Real reasoning via the Hermes SDK (needs a provider key; HOME-isolated)."""

    def __init__(self, home: str):
        self._home = home
        self._agent = None

    async def turn(self, agent: EmbeddedAgent, text: str) -> str:
        def _chat() -> str:
            from run_agent import AIAgent

            os.environ["HOME"] = self._home
            if self._agent is None:
                self._agent = AIAgent(quiet_mode=True)
            return self._agent.chat(text)

        return await asyncio.to_thread(_chat)


class EmbeddedAgent:
    """The principal agent run in-process, driven by a Channel (lab mode).

    Two concurrent loops: the user loop turns user input into replies (and answers
    pending escalations), and the event loop surfaces office escalations to the user.
    """

    def __init__(
        self, shadow: Shadow, channel: Channel, brain: AgentBrain | None = None
    ):
        self.shadow = shadow
        self.channel = channel
        self.brain = brain or ScriptedAgentBrain()
        self._stop = False
        self._pending: tuple[str, str | None] | None = None

    async def call(self, name: str, arguments: dict | None = None) -> dict:
        return await self.shadow.gateway.call_as("agent", name, arguments or {})

    async def run(self) -> None:
        self._stop = False
        await asyncio.gather(self._user_loop(), self._event_loop())

    async def _user_loop(self) -> None:
        while not self._stop:
            text = await self.channel.next_user_message()
            pending, self._pending = self._pending, None
            if pending is not None:
                await self._resolve(pending, text)
                continue
            await self.channel.send_to_user(await self.brain.turn(self, text))

    async def _resolve(self, pending: tuple[str, str | None], text: str) -> None:
        kind, ref = pending
        if kind == "escalation":
            await self.call("resolve_escalation", {"id": ref, "decision": text})
            await self.channel.send_to_user("Okay, I passed that along.")
        elif text.strip().lower() in _AFFIRMATIVE and ref is not None:
            await self.call("add_contact", {"name": ref})
            await self.channel.send_to_user("Okay — connected; I'll let them in.")
        elif text.strip().lower() in _AFFIRMATIVE:
            await self.channel.send_to_user("I can't reach them to connect, sorry.")
        else:
            await self.channel.send_to_user("Okay, I'll leave them on hold.")

    async def _event_loop(self) -> None:
        cursor = self.shadow.events.since(None)[1]
        while not self._stop:
            result = await self.call(
                "inbox_wait", {"timeout_seconds": 20, "last_event_id": cursor}
            )
            cursor = result["next_event_id"] or cursor
            for event in result["events"]:
                if event["event"] == "escalation.raised":
                    await self._surface_escalation(event["data"]["escalationId"])
                elif event["event"] == "review.pending":
                    await self._surface_review(event["data"])

    async def _surface_escalation(self, escalation_id: str) -> None:
        escalations = (await self.call("escalations", {}))["escalations"]
        match = next((e for e in escalations if e["id"] == escalation_id), None)
        if match is not None:
            self._pending = ("escalation", escalation_id)
            await self.channel.send_to_user(f"Your office asks: {match['question']}")

    async def _surface_review(self, data: dict) -> None:
        self._pending = ("review", data.get("addr"))
        who = data["from"][:12]
        await self.channel.send_to_user(
            f'A stranger ({who}…) wants to connect: "{data["text"]}" — approve?'
        )

    def stop(self) -> None:
        self._stop = True
