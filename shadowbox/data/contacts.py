import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from shadowbox.address import Address
from shadowbox.config import Settings
from shadowbox.models import (
    AddContactInput,
    AddContactResult,
    ContactDetail,
    ContactProfile,
    ContactsResult,
    ContactSummary,
    Ok,
    ResolveResult,
)

KNOWN_GRANTS = {"messaging"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    persona TEXT NOT NULL,
    shadowname TEXT NOT NULL,
    display_name TEXT,
    pk TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    grants TEXT NOT NULL,
    profile TEXT,
    added_at TEXT NOT NULL,
    last_seen TEXT,
    tls_pin TEXT,
    PRIMARY KEY (persona, shadowname)
)
"""


class ToolError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def resolve(name: str) -> ResolveResult:
    try:
        addr = Address.parse(name)
    except ValueError:
        raise ToolError("resolve_failed") from None
    if addr.public_key is None or addr.endpoint is None:
        raise ToolError("resolve_failed")
    return ResolveResult(
        shadowname=addr.wire_name, pk=addr.public_key.multibase, endpoint=addr.endpoint
    )


class ContactStore(ABC):
    persona: str

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def add_contact(self, inp: AddContactInput) -> AddContactResult: ...

    @abstractmethod
    def add_peer(self, addr: Address) -> None: ...

    @abstractmethod
    def contacts(self, query: str | None = None) -> ContactsResult: ...

    @abstractmethod
    def contact_detail(self, name: str) -> ContactDetail: ...

    @abstractmethod
    def try_detail(self, name: str) -> ContactDetail | None: ...

    @abstractmethod
    def grant(self, name: str, grant: str, allowed: bool) -> Ok: ...

    @abstractmethod
    def set_contact_profile(self, name: str, profile: ContactProfile) -> Ok: ...


class SqliteContactStore(ContactStore):
    def __init__(self, settings: Settings, persona: str):
        self.persona = persona
        self.db = sqlite3.connect(settings.db_file, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _row(self, name: str) -> sqlite3.Row:
        row = self._try_row(name)
        if row is None:
            raise ToolError("not_contact")
        return row

    def _try_row(self, name: str) -> sqlite3.Row | None:
        try:
            wire = Address.parse(name).wire_name
        except ValueError:
            return None
        return self.db.execute(
            "SELECT * FROM contacts WHERE persona = ? AND shadowname = ?",
            (self.persona, wire),
        ).fetchone()

    def _insert(
        self,
        addr: Address,
        display_name: str | None,
        grants: list[str],
        profile: ContactProfile | None,
    ) -> None:
        assert addr.public_key is not None and addr.endpoint is not None
        self.db.execute(
            "INSERT INTO contacts (persona, shadowname, display_name, pk,"
            " endpoint, grants, profile, added_at, tls_pin)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.persona,
                addr.wire_name,
                display_name,
                addr.public_key.multibase,
                addr.endpoint,
                json.dumps(grants),
                profile.model_dump_json(by_alias=True) if profile else None,
                _now(),
                addr.pin,
            ),
        )

    def add_contact(self, inp: AddContactInput) -> AddContactResult:
        try:
            addr = Address.parse(inp.name)
        except ValueError:
            raise ToolError("resolve_failed") from None
        if addr.public_key is None or addr.endpoint is None:
            raise ToolError("resolve_failed")
        for grant in inp.grants:
            if grant not in KNOWN_GRANTS:
                raise ToolError("unknown_grant")
        try:
            self._insert(addr, inp.display_name, inp.grants, inp.profile)
        except sqlite3.IntegrityError:
            raise ToolError("already_contact") from None
        self.db.commit()
        return AddContactResult(shadowname=addr.wire_name)

    def add_peer(self, addr: Address) -> None:
        if self._try_row(addr.wire_name) is not None:
            return
        self._insert(addr, None, ["messaging"], None)
        self.db.commit()

    def contacts(self, query: str | None = None) -> ContactsResult:
        rows = self.db.execute(
            "SELECT * FROM contacts WHERE persona = ? ORDER BY added_at",
            (self.persona,),
        ).fetchall()
        summaries = [
            ContactSummary(
                shadowname=r["shadowname"],
                display_name=r["display_name"],
                grants=json.loads(r["grants"]),
                last_seen=r["last_seen"],
            )
            for r in rows
        ]
        if query:
            q = query.lower()
            summaries = [
                s
                for s in summaries
                if q in s.shadowname.lower() or q in (s.display_name or "").lower()
            ]
        return ContactsResult(contacts=summaries)

    def _detail(self, r: sqlite3.Row) -> ContactDetail:
        return ContactDetail(
            shadowname=r["shadowname"],
            display_name=r["display_name"],
            pk=r["pk"],
            endpoint=r["endpoint"],
            grants=json.loads(r["grants"]),
            profile=(
                ContactProfile.model_validate_json(r["profile"])
                if r["profile"]
                else None
            ),
            added_at=r["added_at"],
            last_seen=r["last_seen"],
            tls_pin=r["tls_pin"],
        )

    def contact_detail(self, name: str) -> ContactDetail:
        return self._detail(self._row(name))

    def try_detail(self, name: str) -> ContactDetail | None:
        row = self._try_row(name)
        return self._detail(row) if row is not None else None

    def grant(self, name: str, grant: str, allowed: bool) -> Ok:
        if grant not in KNOWN_GRANTS:
            raise ToolError("unknown_grant")
        row = self._row(name)
        grants = set(json.loads(row["grants"]))
        grants.add(grant) if allowed else grants.discard(grant)
        self.db.execute(
            "UPDATE contacts SET grants = ? WHERE persona = ? AND shadowname = ?",
            (json.dumps(sorted(grants)), self.persona, row["shadowname"]),
        )
        self.db.commit()
        return Ok()

    def set_contact_profile(self, name: str, profile: ContactProfile) -> Ok:
        row = self._row(name)
        self.db.execute(
            "UPDATE contacts SET profile = ? WHERE persona = ? AND shadowname = ?",
            (profile.model_dump_json(by_alias=True), self.persona, row["shadowname"]),
        )
        self.db.commit()
        return Ok()