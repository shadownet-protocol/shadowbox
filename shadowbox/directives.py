import json
import sqlite3
from datetime import UTC, datetime

from shadowbox.config import Settings
from shadowbox.contacts import SCHEMA as CONTACTS_SCHEMA
from shadowbox.contacts import ToolError, wire_name
from shadowbox.models import (
    DirectiveItem,
    DirectiveLayer,
    DirectivesResult,
    Ok,
    SetDirectivesInput,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS directives (
    persona TEXT NOT NULL,
    scope TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT '',
    items TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (persona, scope, ref)
)
"""


class DirectiveStore:
    def __init__(self, settings: Settings, persona: str):
        self.persona = persona
        self.db = sqlite3.connect(settings.db_file, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(CONTACTS_SCHEMA)
        self.db.execute(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _live_items(self, raw: str) -> list[DirectiveItem]:
        now = datetime.now(UTC)
        live: list[DirectiveItem] = []
        for item in (DirectiveItem.model_validate(i) for i in json.loads(raw)):
            if item.expires_at is not None:
                try:
                    expires = datetime.fromisoformat(item.expires_at)
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=UTC)
                    if expires <= now:
                        continue
                except ValueError:
                    pass
            live.append(item)
        return live

    def _layer(self, scope: str, ref: str) -> DirectiveLayer | None:
        row = self.db.execute(
            "SELECT * FROM directives WHERE persona = ? AND scope = ? AND ref = ?",
            (self.persona, scope, ref),
        ).fetchone()
        if row is None:
            return None
        items = self._live_items(row["items"])
        if not items:
            return None
        return DirectiveLayer(
            scope=scope, ref=ref or None, items=items, updated_at=row["updated_at"]
        )

    def layers(
        self, contact: str | None = None, context_id: str | None = None
    ) -> DirectivesResult:
        result: list[DirectiveLayer] = []
        if (layer := self._layer("global", "")) is not None:
            result.append(layer)
        if contact is not None:
            if (layer := self._layer("contact", wire_name(contact))) is not None:
                result.append(layer)
        if context_id is not None:
            if (layer := self._layer("context", context_id)) is not None:
                result.append(layer)
        return DirectivesResult(directives=result)

    def set_layer(self, inp: SetDirectivesInput) -> Ok:
        if inp.scope == "global":
            if inp.ref is not None:
                raise ValueError("ref must be absent for global scope")
            ref = ""
        elif inp.ref is None:
            raise ValueError(f"ref required for {inp.scope} scope")
        elif inp.scope == "contact":
            ref = wire_name(inp.ref)
            known = self.db.execute(
                "SELECT 1 FROM contacts WHERE persona = ? AND shadowname = ?",
                (self.persona, ref),
            ).fetchone()
            if known is None:
                raise ToolError("not_contact")
        else:
            raise ToolError("unknown_context")

        if inp.items:
            self.db.execute(
                "INSERT INTO directives (persona, scope, ref, items, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(persona, scope, ref) DO UPDATE SET"
                " items = excluded.items, updated_at = excluded.updated_at",
                (
                    self.persona,
                    inp.scope,
                    ref,
                    json.dumps(
                        [
                            i.model_dump(by_alias=True, exclude_none=True)
                            for i in inp.items
                        ]
                    ),
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )
        else:
            self.db.execute(
                "DELETE FROM directives WHERE persona = ? AND scope = ? AND ref = ?",
                (self.persona, inp.scope, ref),
            )
        self.db.commit()
        return Ok()