from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shadowbox.shadow.shadow import Shadow


@dataclass
class Reply:
    text: str | None = None
    escalate: str | None = None


class Brain(ABC):
    """Decides how the office acts on a delegated task or an inbound message."""

    @abstractmethod
    async def open(self, instruction: str) -> str: ...

    @abstractmethod
    async def reply(self, text: str) -> Reply: ...


class ScriptedBrain(Brain):
    """Deterministic stand-in used until a Hermes-backed brain is wired (phase 3)."""

    async def open(self, instruction: str) -> str:
        return instruction

    async def reply(self, text: str) -> Reply:
        if "?" in text:
            return Reply(escalate=text)
        if text.startswith("ack:"):
            return Reply()
        return Reply(text=f"ack: {text}")


class Office:
    """The peer-facing desk: consumes the sidecar's queues and answers the wire."""

    def __init__(self, shadow: Shadow, brain: Brain | None = None):
        self.shadow = shadow
        self.brain = brain or ScriptedBrain()
        self._stop = False

    async def _call(self, name: str, arguments: dict | None = None) -> dict:
        return await self.shadow.gateway.call_as("office", name, arguments or {})

    async def run(self) -> None:
        self._stop = False
        cursor = self.shadow.events.since(None)[1]
        while not self._stop:
            result = await self._call(
                "inbox_wait", {"timeout_seconds": 20, "last_event_id": cursor}
            )
            cursor = result["next_event_id"] or cursor
            for event in result["events"]:
                try:
                    await self._on_event(event)
                except Exception:
                    pass

    async def _on_event(self, event: dict) -> None:
        data = event["data"]
        kind = event["event"]
        if kind == "task.created":
            await self._on_task(data["taskId"])
        elif kind == "inbox.message" and data.get("status") == "inbox":
            await self._on_inbound(data["contextId"])
        elif kind == "escalation.resolved":
            await self._call(
                "respond",
                {"contextId": data["contextId"], "body": {"text": data["decision"]}},
            )

    async def _on_task(self, task_id: str) -> None:
        task = await self._call("claim_task", {"id": task_id})
        text = await self.brain.open(task["instruction"])
        await self._call(
            "send",
            {
                "to": task["toPeer"],
                "body": {"text": text},
                "contextId": task["contextId"],
            },
        )
        await self._call("complete_task", {"id": task_id})

    async def _on_inbound(self, context_id: str) -> None:
        history = await self._call("history", {"contextId": context_id, "limit": 1})
        if not history["items"]:
            return
        item = history["items"][0]
        if item["direction"] != "inbound":
            return
        reply = await self.brain.reply(item["body"].get("text", ""))
        if reply.escalate is not None:
            await self._call(
                "escalate", {"contextId": context_id, "question": reply.escalate}
            )
        elif reply.text is not None:
            await self._call(
                "respond", {"contextId": context_id, "body": {"text": reply.text}}
            )

    def stop(self) -> None:
        self._stop = True
