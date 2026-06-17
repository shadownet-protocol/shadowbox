from shadowbox.crypto import Jcs, Jws, PublicKey, SigningKey
from shadowbox.data.envelope import URN


class AgentCard:
    def __init__(self, data: dict):
        self.data = data

    @classmethod
    def build(cls, pk: str, url: str, name: str) -> "AgentCard":
        return cls(
            {
                "name": name,
                "description": "Shadownet Shadow",
                "version": "0.2.0",
                "supportedInterfaces": [{"url": url, "protocolBinding": "JSONRPC"}],
                "capabilities": {
                    "extensions": [
                        {
                            "uri": URN,
                            "required": True,
                            "description": "Shadownet identity envelope",
                        }
                    ]
                },
                "defaultInputModes": ["application/a2a+json"],
                "defaultOutputModes": ["application/a2a+json"],
                "skills": [],
                "securitySchemes": {
                    "shadownet:pinned-self-signed": {"type": "mutualTLS"}
                },
                "shadownet:v": "0.2",
                "shadownet:pk": pk,
            }
        )

    @property
    def pk(self) -> str:
        return self.data["shadownet:pk"]

    @property
    def url(self) -> str:
        return self.data["supportedInterfaces"][0]["url"]

    def sign(self, key: SigningKey) -> "AgentCard":
        protected_b64 = Jws.b64url_encode(Jcs.encode({"alg": "EdDSA", "kid": self.pk}))
        payload_b64 = Jws.b64url_encode(Jcs.encode(self.data))
        signature = key.sign(f"{protected_b64}.{payload_b64}".encode())
        return AgentCard(
            {
                **self.data,
                "signatures": [
                    {
                        "protected": protected_b64,
                        "signature": Jws.b64url_encode(signature),
                    }
                ],
            }
        )

    @classmethod
    def verify(cls, data: dict, expected_pk: str) -> "AgentCard":
        signatures = data.get("signatures") or []
        body = {k: v for k, v in data.items() if k != "signatures"}
        if body.get("shadownet:pk") != expected_pk:
            raise ValueError("AgentCard pk does not match expected identity")
        key = PublicKey.from_multibase(expected_pk)
        payload_b64 = Jws.b64url_encode(Jcs.encode(body))
        for sig in signatures:
            signing_input = f"{sig['protected']}.{payload_b64}".encode()
            if key.verify(Jws.b64url_decode(sig["signature"]), signing_input):
                return cls(body)
        raise ValueError("AgentCard signature does not verify")

    def to_dict(self) -> dict:
        return self.data
