import asyncio

from shadowbox.shadow.channel.base import Channel


class TuiChannel(Channel):
    """In-process channel for the lab: the user side is a TUI input + transcript."""

    def __init__(self):
        self._inbound: asyncio.Queue[str] = asyncio.Queue()
        self.outbound: asyncio.Queue[str] = asyncio.Queue()

    async def next_user_message(self) -> str:
        return await self._inbound.get()

    async def send_to_user(self, text: str) -> None:
        await self.outbound.put(text)

    async def submit(self, text: str) -> None:
        """Transport side: the TUI calls this when the user sends a message."""
        await self._inbound.put(text)
