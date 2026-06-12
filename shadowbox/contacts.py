import json
import sqlite3
from datetime import UTC, datetime
from urllib.parse import urlsplit

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


def parse_direct_uri(name: str) -> tuple[str, str, str | None] | None:
    if not name.startswith("shadow://key:"):
        return None
    parts = urlsplit(name)
    if not parts.password or not parts.hostname:
        return None
    port = f":{parts.port}" if parts.port else ""
    endpoint = f"https://{parts.hostname}{port}"
    pin = (
        parts.fragment.removeprefix("sha256:")
        if parts.fragment.startswith("sha256:")
        else None
    )
    return parts.password, endpoint, pin


def wire_name(name: str) -> str:
    direct = parse_direct_uri(name)
    return direct[0] if direct else name


def resolve(name: str) -> ResolveResult:
    direct = parse_direct_uri(name)
    if direct is None:
        raise ToolError("resolve_failed")
    pk, endpoint, _ = direct
    return ResolveResult(shadowname=pk, pk=pk, endpoint=endpoint)


class ContactStore:
    def __init__(self, settings: Settings, persona: str):
        self.persona = persona
        self.db = sqlite3.connect(settings.db_file, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _row(self, name: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM contacts WHERE persona = ? AND shadowname = ?",
            (self.persona, wire_name(name)),
        ).fetchone()
        if row is None:
            raise ToolError("not_contact")
        return row

    def add_contact(self, inp: AddContactInput) -> AddContactResult:
        resolved = resolve(inp.name)
        _, _, pin = parse_direct_uri(inp.name)
        for grant in inp.grants:
            if grant not in KNOWN_GRANTS:
                raise ToolError("unknown_grant")
        try:
            self.db.execute(
                "INSERT INTO contacts (persona, shadowname, display_name, pk,"
                " endpoint, grants, profile, added_at, tls_pin)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.persona,
                    resolved.shadowname,
                    inp.display_name,
                    resolved.pk,
                    resolved.endpoint,
                    json.dumps(inp.grants),
                    inp.profile.model_dump_json(by_alias=True) if inp.profile else None,
                    _now(),
                    pin,
                ),
            )
        except sqlite3.IntegrityError:
            raise ToolError("already_contact") from None
        self.db.commit()
        return AddContactResult(shadowname=resolved.shadowname)

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

    def contact_detail(self, name: str) -> ContactDetail:
        r = self._row(name)
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