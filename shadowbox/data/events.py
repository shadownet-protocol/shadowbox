import asyncio
import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from shadowbox.config import Settings
from shadowbox.models import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    shadow TEXT NOT NULL,
    event TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

MAX_WAIT = 90


class EventStore(ABC):
    shadow: str

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def emit(self, event: str, data: dict) -> Event: ...

    @abstractmethod
    def since(self, last_event_id: str | None) -> tuple[list[Event], str | None]: ...

    @abstractmethod
    async def wait(
        self, last_event_id: str | None, timeout: int
    ) -> tuple[list[Event], str | None]: ...


class SqliteEventStore(EventStore):
    def __init__(self, settings: Settings, shadow: str):
        self.shadow = shadow
        self.db = sqlite3.connect(settings.db_file, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self.db.commit()
        self._waiters: list[asyncio.Future] = []

    def close(self) -> None:
        self.db.close()

    def _notify(self) -> None:
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(None)
        self._waiters.clear()

    def emit(self, event: str, data: dict) -> Event:
        occurred_at = datetime.now(UTC).isoformat(timespec="seconds")
        cur = self.db.execute(
            "INSERT INTO events (shadow, event, occurred_at, data) VALUES (?, ?, ?, ?)",
            (self.shadow, event, occurred_at, json.dumps(data)),
        )
        self.db.commit()
        self._notify()
        return Event(
            event_id=str(cur.lastrowid),
            event=event,
            occurred_at=occurred_at,
            data=data,
        )

    def _high_water(self) -> str | None:
        row = self.db.execute(
            "SELECT MAX(seq) AS hw FROM events WHERE shadow = ?", (self.shadow,)
        ).fetchone()
        return str(row["hw"]) if row["hw"] is not None else None

    def since(self, last_event_id: str | None) -> tuple[list[Event], str | None]:
        cursor = int(last_event_id) if last_event_id is not None else 0
        rows = self.db.execute(
            "SELECT * FROM events WHERE shadow = ? AND seq > ? ORDER BY seq",
            (self.shadow, cursor),
        ).fetchall()
        events = [
            Event(
                event_id=str(r["seq"]),
                event=r["event"],
                occurred_at=r["occurred_at"],
                data=json.loads(r["data"]),
            )
            for r in rows
        ]
        return events, (events[-1].event_id if events else self._high_water())

    async def wait(
        self, last_event_id: str | None, timeout: int
    ) -> tuple[list[Event], str | None]:
        events, cursor = self.since(last_event_id)
        if events:
            return events, cursor
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            await asyncio.wait_for(fut, min(timeout, MAX_WAIT))
        except TimeoutError:
            return [], cursor
        finally:
            if fut in self._waiters:
                self._waiters.remove(fut)
        return self.since(last_event_id)
