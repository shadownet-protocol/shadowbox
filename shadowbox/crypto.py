import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class Jws:
    @staticmethod
    def b64url_encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    @staticmethod
    def b64url_decode(text: str) -> bytes:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

    @classmethod
    def parse(cls, token: str) -> tuple[dict, dict, bytes, bytes]:
        try:
            header_b64, payload_b64, sig_b64 = token.split(".")
        except ValueError:
            raise ValueError("malformed JWS compact serialization") from None
        return (
            json.loads(cls.b64url_decode(header_b64)),
            json.loads(cls.b64url_decode(payload_b64)),
            f"{header_b64}.{payload_b64}".encode(),
            cls.b64url_decode(sig_b64),
        )


class Jcs:
    @staticmethod
    def encode(obj: Any) -> bytes:
        return json.dumps(
            obj,
            separators=(",", ":"),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ).encode()

    @classmethod
    def sha256(cls, obj: Any) -> str:
        return "sha256:" + Jws.b64url_encode(hashlib.sha256(cls.encode(obj)).digest())


class PublicKey:
    _B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    _MULTICODEC = b"\xed\x01"

    def __init__(self, raw: bytes):
        self._raw = raw
        self._key = Ed25519PublicKey.from_public_bytes(raw)

    @classmethod
    def _b58encode(cls, data: bytes) -> str:
        n = int.from_bytes(data, "big")
        out = ""
        while n:
            n, r = divmod(n, 58)
            out = cls._B58[r] + out
        pad = len(data) - len(data.lstrip(b"\x00"))
        return "1" * pad + out

    @classmethod
    def _b58decode(cls, text: str) -> bytes:
        n = 0
        for ch in text:
            n = n * 58 + cls._B58.index(ch)
        body = n.to_bytes((n.bit_length() + 7) // 8, "big")
        pad = len(text) - len(text.lstrip("1"))
        return b"\x00" * pad + body

    @classmethod
    def from_multibase(cls, multibase: str) -> "PublicKey":
        if not multibase.startswith("z"):
            raise ValueError(f"unsupported multibase prefix: {multibase[:1]!r}")
        data = cls._b58decode(multibase[1:])
        if data[:2] != cls._MULTICODEC:
            raise ValueError("not an Ed25519 multicodec key")
        return cls(data[2:])

    @property
    def multibase(self) -> str:
        return "z" + self._b58encode(self._MULTICODEC + self._raw)

    def verify(self, signature: bytes, data: bytes) -> bool:
        try:
            self._key.verify(signature, data)
            return True
        except InvalidSignature:
            return False

    def verify_jws(self, token: str) -> dict:
        _header, payload, signing_input, signature = Jws.parse(token)
        if not self.verify(signature, signing_input):
            raise ValueError("JWS signature does not verify")
        return payload

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PublicKey) and other._raw == self._raw

    def __hash__(self) -> int:
        return hash(self._raw)


class SigningKey:
    def __init__(self, key: Ed25519PrivateKey):
        self._key = key

    @classmethod
    def generate(cls) -> "SigningKey":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load(cls, path: Path) -> "SigningKey":
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(f"{path} is not an Ed25519 key")
        return cls(key)

    def save(self, path: Path) -> None:
        path.write_bytes(
            self._key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        path.chmod(0o600)

    @property
    def public(self) -> PublicKey:
        return PublicKey(self._key.public_key().public_bytes_raw())

    @property
    def multibase(self) -> str:
        return self.public.multibase

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)

    def sign_jws(self, header: dict, payload: dict) -> str:
        header_b64 = Jws.b64url_encode(Jcs.encode(header))
        payload_b64 = Jws.b64url_encode(Jcs.encode(payload))
        signature = self.sign(f"{header_b64}.{payload_b64}".encode())
        return f"{header_b64}.{payload_b64}.{Jws.b64url_encode(signature)}"
