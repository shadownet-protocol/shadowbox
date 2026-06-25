from abc import ABC, abstractmethod


class Channel(ABC):
    """The principal agent's link to its user.

    The agent loop pulls user messages from `next_user_message` and delivers its
    replies through `send_to_user`. The transport behind the channel (Telegram in
    production, the TUI in the lab) feeds and drains those two directions.
    """

    @abstractmethod
    async def next_user_message(self) -> str: ...

    @abstractmethod
    async def send_to_user(self, text: str) -> None: ...
