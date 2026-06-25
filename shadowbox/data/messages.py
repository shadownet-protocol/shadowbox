import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from pathlib import Path

from shadowbox.models import (
    Body,
    Context,
    ContextsResult,
    HistoryItem,
    HistoryResult,
    InboxItem,
    InboxResult,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    context_id TEXT NOT NULL,
    peer TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    status TEXT NOT NULL,
    intent TEXT,
    body TEXT NOT NULL,
    PRIMARY KEY (message_id, direction)
);
CREATE TABLE IF NOT EXISTS replay (
    sender TEXT NOT NULL,
    message_id TEXT NOT NULL,
    exp INTEGER NOT NULL,
    PRIMARY KEY (sender, message_id)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class MessageStore(ABC):
    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def record_outbound(
        self, message_id: str, context_id: str, peer: str, body: dict
    ) -> None: ...

    @abstractmethod
    def set_status(self, message_id: str, status: str) -> None: ...

    @abstractmethod
    def record_inbound(
        self, message_id: str, context_id: str, peer: str, status: str, body: dict
    ) -> None: ...

    @abstractmethod
    def has_recent_outbound(
        self, peer: str, context_id: str, days: int = 7
    ) -> bool: ...

    @abstractmethod
    def graduate(self, peer: str) -> list[tuple[str, str, str | None]]: ...

    @abstractmethod
    def seen(self, sender: str, message_id: str) -> bool: ...

    @abstractmethod
    def remember(self, sender: str, message_id: str, exp: int) -> None: ...

    @abstractmethod
    def inbox(
        self,
        since: str | None = None,
        contact: str | None = None,
        intent: str | None = None,
        include_review: bool = False,
        limit: int = 50,
    ) -> InboxResult: ...

    @abstractmethod
    def contexts(
        self,
        contact: str | None = None,
        include_review: bool = False,
        since: str | None = None,
        limit: int = 50,
    ) -> ContextsResult: ...

    @abstractmethod
    def history(
        self,
        context_id: str | None = None,
        contact: str | None = None,
        intent: str | None = None,
        include_review: bool = False,
        before: str | None = None,
        limit: int = 50,
    ) -> HistoryResult: ...


class SqliteMessageStore(MessageStore):
    def __init__(self, db_path: Path):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def record_outbound(
        self, message_id: str, context_id: str, peer: str, body: dict
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO messages (message_id, direction,"
            " context_id, peer, occurred_at, status, intent, body)"
            " VALUES (?, 'outbound', ?, ?, ?, 'sending', ?, ?)",
            (
                message_id,
                context_id,
                peer,
                _now(),
                body.get("intent"),
                json.dumps(body),
            ),
        )
        self.db.commit()

    def set_status(self, message_id: str, status: str) -> None:
        self.db.execute(
            "UPDATE messages SET status = ? WHERE message_id = ?"
            " AND direction = 'outbound'",
            (status, message_id),
        )
        self.db.commit()

    def record_inbound(
        self, message_id: str, context_id: str, peer: str, status: str, body: dict
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO messages (message_id, direction,"
            " context_id, peer, occurred_at, status, intent, body)"
            " VALUES (?, 'inbound', ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                context_id,
                peer,
                _now(),
                status,
                body.get("intent"),
                json.dumps(body),
            ),
        )
        self.db.commit()

    def has_recent_outbound(self, peer: str, context_id: str, days: int = 7) -> bool:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        row = self.db.execute(
            "SELECT 1 FROM messages WHERE direction = 'outbound'"
            " AND peer = ? AND context_id = ? AND occurred_at >= ? LIMIT 1",
            (peer, context_id, cutoff),
        ).fetchone()
        return row is not None

    def graduate(self, peer: str) -> list[tuple[str, str, str | None]]:
        rows = self.db.execute(
            "SELECT message_id, context_id, intent FROM messages"
            " WHERE direction = 'inbound' AND peer = ? AND status = 'stranger_review'",
            (peer,),
        ).fetchall()
        self.db.execute(
            "UPDATE messages SET status = 'inbox' WHERE direction = 'inbound'"
            " AND peer = ? AND status = 'stranger_review'",
            (peer,),
        )
        self.db.commit()
        return [(r["message_id"], r["context_id"], r["intent"]) for r in rows]

    def seen(self, sender: str, message_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM replay WHERE sender = ? AND message_id = ?",
            (sender, message_id),
        ).fetchone()
        return row is not None

    def remember(self, sender: str, message_id: str, exp: int) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO replay (sender, message_id, exp) VALUES (?, ?, ?)",
            (sender, message_id, exp),
        )
        self.db.commit()

    def _body(self, raw: str) -> Body:
        return Body.model_validate(json.loads(raw))

    def _where(self, clauses: list[str]) -> str:
        return (" WHERE " + " AND ".join(clauses)) if clauses else ""

    def inbox(
        self,
        since: str | None = None,
        contact: str | None = None,
        intent: str | None = None,
        include_review: bool = False,
        limit: int = 50,
    ) -> InboxResult:
        statuses = ["inbox", "stranger_review"] if include_review else ["inbox"]
        clauses = [
            "direction = 'inbound'",
            f"status IN ({','.join('?' for _ in statuses)})",
        ]
        params: list = [*statuses]
        if since:
            clauses.append("occurred_at > ?")
            params.append(since)
        if contact:
            clauses.append("peer = ?")
            params.append(contact)
        if intent:
            clauses.append("intent = ?")
            params.append(intent)
        params.append(limit)
        rows = self.db.execute(
            f"SELECT * FROM messages{self._where(clauses)}"
            " ORDER BY occurred_at ASC LIMIT ?",
            params,
        ).fetchall()
        items = [
            InboxItem(
                message_id=r["message_id"],
                context_id=r["context_id"],
                **{"from": r["peer"]},
                received_at=r["occurred_at"],
                status=r["status"],
                body=self._body(r["body"]),
            )
            for r in rows
        ]
        next_since = items[-1].received_at if len(items) == limit else None
        return InboxResult(items=items, next_since=next_since)

    def contexts(
        self,
        contact: str | None = None,
        include_review: bool = False,
        since: str | None = None,
        limit: int = 50,
    ) -> ContextsResult:
        clauses: list[str] = []
        params: list = []
        if not include_review:
            clauses.append("NOT (direction = 'inbound' AND status = 'stranger_review')")
        if contact:
            clauses.append("peer = ?")
            params.append(contact)
        rows = self.db.execute(
            f"SELECT * FROM messages{self._where(clauses)} ORDER BY occurred_at ASC",
            params,
        ).fetchall()
        by_context: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by_context.setdefault(r["context_id"], []).append(r)
        contexts: list[Context] = []
        for context_id, msgs in by_context.items():
            last = msgs[-1]
            if since and last["occurred_at"] <= since:
                continue
            contexts.append(
                Context(
                    context_id=context_id,
                    peers=sorted({m["peer"] for m in msgs}),
                    last_message_at=last["occurred_at"],
                    last_direction=last["direction"],
                    last_intent=last["intent"],
                )
            )
        contexts.sort(key=lambda c: c.last_message_at)
        contexts = contexts[:limit]
        next_since = contexts[-1].last_message_at if len(contexts) == limit else None
        return ContextsResult(contexts=contexts, next_since=next_since)

    def history(
        self,
        context_id: str | None = None,
        contact: str | None = None,
        intent: str | None = None,
        include_review: bool = False,
        before: str | None = None,
        limit: int = 50,
    ) -> HistoryResult:
        clauses: list[str] = []
        params: list = []
        if not include_review:
            clauses.append("NOT (direction = 'inbound' AND status = 'stranger_review')")
        if context_id:
            clauses.append("context_id = ?")
            params.append(context_id)
        if contact:
            clauses.append("peer = ?")
            params.append(contact)
        if intent:
            clauses.append("intent = ?")
            params.append(intent)
        if before:
            clauses.append("occurred_at < ?")
            params.append(before)
        params.append(limit)
        rows = self.db.execute(
            f"SELECT * FROM messages{self._where(clauses)}"
            " ORDER BY occurred_at DESC LIMIT ?",
            params,
        ).fetchall()
        items = [
            HistoryItem(
                message_id=r["message_id"],
                context_id=r["context_id"],
                direction=r["direction"],
                peer=r["peer"],
                occurred_at=r["occurred_at"],
                status=r["status"],
                body=self._body(r["body"]),
            )
            for r in rows
        ]
        next_before = items[-1].occurred_at if len(items) == limit else None
        return HistoryResult(items=items, next_before=next_before)
