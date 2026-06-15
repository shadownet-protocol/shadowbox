from typing import Literal
from urllib.parse import urlsplit

from shadowbox.crypto import PublicKey

Mode = Literal["shadowname", "direct", "key"]

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Address:
    def __init__(
        self,
        mode: Mode,
        wire_name: str,
        public_key: PublicKey | None = None,
        host: str | None = None,
        port: int | None = None,
        pin: str | None = None,
    ):
        self.mode = mode
        self.wire_name = wire_name
        self.public_key = public_key
        self.host = host
        self.port = port
        self.pin = pin

    @classmethod
    def parse(cls, text: str) -> "Address":
        text = text.strip()
        if text.startswith("shadow://key:"):
            parts = urlsplit(text)
            if not parts.password or not parts.hostname:
                raise ValueError(f"malformed direct URI: {text}")
            pin = (
                parts.fragment.removeprefix("sha256:")
                if parts.fragment.startswith("sha256:")
                else None
            )
            return cls(
                "direct",
                parts.password,
                PublicKey.from_multibase(parts.password),
                parts.hostname,
                parts.port,
                pin,
            )
        if text.startswith("shadow://"):
            return cls("shadowname", text[len("shadow://") :])
        if "@" in text:
            return cls("shadowname", text)
        if text.startswith("z"):
            return cls("key", text, PublicKey.from_multibase(text))
        raise ValueError(f"unrecognized identifier: {text}")

    @classmethod
    def direct(
        cls, public_key: PublicKey, host: str, port: int, pin: str | None = None
    ) -> "Address":
        return cls("direct", public_key.multibase, public_key, host, port, pin)

    @property
    def endpoint(self) -> str | None:
        if self.host is None:
            return None
        scheme = "http" if self.host in _LOCAL_HOSTS else "https"
        port = f":{self.port}" if self.port else ""
        return f"{scheme}://{self.host}{port}"

    @property
    def uri(self) -> str:
        if self.mode != "direct":
            return self.wire_name
        out = f"shadow://key:{self.wire_name}@{self.host}"
        if self.port:
            out += f":{self.port}"
        if self.pin:
            out += f"#sha256:{self.pin}"
        return out
