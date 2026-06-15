import sqlite3
from abc import ABC, abstractmethod

from shadowbox.config import Settings, TrustConfig
from shadowbox.crypto import Jws, PublicKey, SigningKey

CRED_TYP = "shadownet-cred+jwt"
MAX_LIFETIME = {"org_affiliation": 30 * 86400}

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    persona TEXT NOT NULL,
    token TEXT NOT NULL,
    exp INTEGER NOT NULL,
    PRIMARY KEY (persona, token)
)
"""


class Credential:
    def __init__(self, iss: str, sub: str, org: str, kind: str, iat: int, exp: int):
        self.iss = iss
        self.sub = sub
        self.org = org
        self.kind = kind
        self.iat = iat
        self.exp = exp

    @classmethod
    def mint(
        cls,
        issuer_key: SigningKey,
        subject: str,
        now: int,
        kind: str = "org_affiliation",
        ttl: int | None = None,
    ) -> str:
        issuer = issuer_key.multibase
        payload = {
            "iss": issuer,
            "sub": subject,
            "kind": kind,
            "org": issuer,
            "iat": now,
            "exp": now + (ttl or MAX_LIFETIME[kind]),
        }
        return issuer_key.sign_jws({"alg": "EdDSA", "typ": CRED_TYP}, payload)

    @classmethod
    def validate(cls, token: str, now: int) -> "Credential":
        header, payload, signing_input, signature = Jws.parse(token)
        if header.get("typ") != CRED_TYP:
            raise ValueError("not a credential JWT")
        issuer = payload.get("iss")
        if not isinstance(issuer, str):
            raise ValueError("missing iss")
        if not PublicKey.from_multibase(issuer).verify(signature, signing_input):
            raise ValueError("credential signature invalid")
        kind = payload.get("kind")
        if kind not in MAX_LIFETIME:
            raise ValueError(f"unknown credential kind: {kind}")
        iat, exp = payload.get("iat"), payload.get("exp")
        if not isinstance(iat, int) or not isinstance(exp, int):
            raise ValueError("bad iat/exp")
        if exp <= now - 60 or iat >= now + 60:
            raise ValueError("credential expired or not yet valid")
        if exp - iat > MAX_LIFETIME[kind]:
            raise ValueError("credential lifetime too long")
        if payload.get("org") != issuer:
            raise ValueError("issuer not authorized for org")
        return cls(issuer, payload.get("sub"), issuer, kind, iat, exp)


def satisfies(
    trust: TrustConfig, tokens: list[str], required_kinds: list[str], now: int
) -> bool:
    for token in tokens:
        try:
            cred = Credential.validate(token, now)
        except ValueError:
            continue
        if cred.kind not in required_kinds:
            continue
        for entry in trust.issuers:
            if entry.issuer == cred.iss and cred.kind in entry.accept:
                return True
    return False


class CredentialStore(ABC):
    persona: str

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def add(self, token: str, exp: int) -> None: ...

    @abstractmethod
    def valid_tokens(self, now: int) -> list[str]: ...


class SqliteCredentialStore(CredentialStore):
    def __init__(self, settings: Settings, persona: str):
        self.persona = persona
        self.db = sqlite3.connect(settings.db_file, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def add(self, token: str, exp: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO credentials (persona, token, exp) VALUES (?, ?, ?)",
            (self.persona, token, exp),
        )
        self.db.commit()

    def valid_tokens(self, now: int) -> list[str]:
        rows = self.db.execute(
            "SELECT token FROM credentials WHERE persona = ? AND exp > ?",
            (self.persona, now - 60),
        ).fetchall()
        return [r["token"] for r in rows]
