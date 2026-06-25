import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

from shadowbox.ids import ulid
from shadowbox.models import Escalation, Task

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    context_id TEXT,
    to_peer TEXT NOT NULL,
    intent TEXT,
    instruction TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    result TEXT
);
CREATE TABLE IF NOT EXISTS escalations (
    id TEXT PRIMARY KEY,
    context_id TEXT NOT NULL,
    question TEXT NOT NULL,
    options TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class TaskStore(ABC):
    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def add(
        self,
        to_peer: str,
        instruction: str,
        intent: str | None = None,
        context_id: str | None = None,
    ) -> Task: ...

    @abstractmethod
    def open(self) -> list[Task]: ...

    @abstractmethod
    def claim(self, task_id: str) -> Task: ...

    @abstractmethod
    def complete(self, task_id: str, result: str | None = None) -> Task: ...


class EscalationStore(ABC):
    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def raise_(
        self, context_id: str, question: str, options: list[str] | None = None
    ) -> Escalation: ...

    @abstractmethod
    def open(self) -> list[Escalation]: ...

    @abstractmethod
    def resolve(self, escalation_id: str, decision: str) -> Escalation: ...


class _Db:
    def __init__(self, db_path: Path):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()


class SqliteTaskStore(_Db, TaskStore):
    def _task(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            context_id=row["context_id"],
            to_peer=row["to_peer"],
            intent=row["intent"],
            instruction=row["instruction"],
            status=row["status"],
            created_at=row["created_at"],
            result=row["result"],
        )

    def add(
        self,
        to_peer: str,
        instruction: str,
        intent: str | None = None,
        context_id: str | None = None,
    ) -> Task:
        task = Task(
            id=ulid(),
            context_id=context_id,
            to_peer=to_peer,
            intent=intent,
            instruction=instruction,
            status="open",
            created_at=_now(),
        )
        self.db.execute(
            "INSERT INTO tasks (id, context_id, to_peer, intent, instruction,"
            " status, created_at, result) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                task.id,
                task.context_id,
                task.to_peer,
                task.intent,
                task.instruction,
                task.status,
                task.created_at,
            ),
        )
        self.db.commit()
        return task

    def open(self) -> list[Task]:
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status != 'done' ORDER BY created_at"
        ).fetchall()
        return [self._task(r) for r in rows]

    def _get(self, task_id: str) -> Task:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task(row)

    def claim(self, task_id: str) -> Task:
        self.db.execute("UPDATE tasks SET status = 'claimed' WHERE id = ?", (task_id,))
        self.db.commit()
        return self._get(task_id)

    def complete(self, task_id: str, result: str | None = None) -> Task:
        self.db.execute(
            "UPDATE tasks SET status = 'done', result = ? WHERE id = ?",
            (result, task_id),
        )
        self.db.commit()
        return self._get(task_id)


class SqliteEscalationStore(_Db, EscalationStore):
    def _escalation(self, row: sqlite3.Row) -> Escalation:
        return Escalation(
            id=row["id"],
            context_id=row["context_id"],
            question=row["question"],
            options=json.loads(row["options"]),
            status=row["status"],
            decision=row["decision"],
            created_at=row["created_at"],
        )

    def raise_(
        self, context_id: str, question: str, options: list[str] | None = None
    ) -> Escalation:
        escalation = Escalation(
            id=ulid(),
            context_id=context_id,
            question=question,
            options=options or [],
            status="open",
            created_at=_now(),
        )
        self.db.execute(
            "INSERT INTO escalations (id, context_id, question, options,"
            " status, decision, created_at) VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (
                escalation.id,
                escalation.context_id,
                escalation.question,
                json.dumps(escalation.options),
                escalation.status,
                escalation.created_at,
            ),
        )
        self.db.commit()
        return escalation

    def open(self) -> list[Escalation]:
        rows = self.db.execute(
            "SELECT * FROM escalations WHERE status = 'open' ORDER BY created_at"
        ).fetchall()
        return [self._escalation(r) for r in rows]

    def resolve(self, escalation_id: str, decision: str) -> Escalation:
        self.db.execute(
            "UPDATE escalations SET status = 'resolved', decision = ? WHERE id = ?",
            (decision, escalation_id),
        )
        self.db.commit()
        row = self.db.execute(
            "SELECT * FROM escalations WHERE id = ?", (escalation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(escalation_id)
        return self._escalation(row)
