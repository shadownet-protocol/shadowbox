from shadowbox.crypto import Jcs, Jws, PublicKey, SigningKey
from shadowbox.ids import ulid

URN = "urn:shadownet:0.2"
ENVELOPE_TYP = "shadownet-env+jwt"
MAX_LIFETIME = 300


class WireError(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


class Envelope:
    def __init__(
        self,
        sender: str,
        recipient: str,
        context_id: str,
        message_id: str,
        body: dict,
        iat: int,
        exp: int,
    ):
        self.sender = sender
        self.recipient = recipient
        self.context_id = context_id
        self.message_id = message_id
        self.body = body
        self.iat = iat
        self.exp = exp

    @staticmethod
    def _canonical_input(message: dict) -> dict:
        out = {
            "messageId": message["messageId"],
            "role": message["role"],
            "parts": message["parts"],
            "contextId": message["contextId"],
        }
        if message.get("taskId"):
            out["taskId"] = message["taskId"]
        metadata = dict(message.get("metadata") or {})
        metadata.pop(URN, None)
        out["metadata"] = metadata
        return out

    @classmethod
    def mint(
        cls,
        key: SigningKey,
        sender: str,
        recipient: str,
        body: dict,
        context_id: str,
        now: int,
    ) -> dict:
        message = {
            "role": "ROLE_USER",
            "parts": [{"text": body.get("text") or ""}],
            "messageId": ulid(),
            "contextId": context_id,
            "extensions": [URN],
            "metadata": {},
        }
        payload = {
            "v": "0.2",
            "from": sender,
            "to": recipient,
            "iat": now,
            "exp": now + MAX_LIFETIME,
            "msgHash": Jcs.sha256(cls._canonical_input(message)),
            "body": body,
        }
        header = {"alg": "EdDSA", "typ": ENVELOPE_TYP, "kid": sender}
        message["metadata"] = {URN: key.sign_jws(header, payload)}
        return message

    @classmethod
    def validate(cls, message: dict, recipient_pk: str, now: int) -> "Envelope":
        if URN not in (message.get("extensions") or []):
            raise WireError("parse_error", 400)
        token = (message.get("metadata") or {}).get(URN)
        if not isinstance(token, str):
            raise WireError("parse_error", 400)
        try:
            header, payload, signing_input, signature = Jws.parse(token)
        except ValueError:
            raise WireError("parse_error", 400) from None
        if header.get("typ") != ENVELOPE_TYP or payload.get("v") != "0.2":
            raise WireError("parse_error", 400)
        if payload.get("to") != recipient_pk:
            raise WireError("unknown_recipient", 404)
        sender = payload.get("from")
        if not isinstance(sender, str) or header.get("kid") != sender:
            raise WireError("parse_error", 400)
        iat, exp = payload.get("iat"), payload.get("exp")
        if not isinstance(iat, int) or not isinstance(exp, int):
            raise WireError("parse_error", 400)
        if exp <= now - 60 or iat >= now + 60 or exp - iat > MAX_LIFETIME:
            raise WireError("parse_error", 400)
        try:
            sender_key = PublicKey.from_multibase(sender)
        except ValueError:
            raise WireError("parse_error", 400) from None
        if not sender_key.verify(signature, signing_input):
            raise WireError("signature", 401)
        if Jcs.sha256(cls._canonical_input(message)) != payload.get("msgHash"):
            raise WireError("parse_error", 400)
        return cls(
            sender,
            recipient_pk,
            message["contextId"],
            message["messageId"],
            payload["body"],
            iat,
            exp,
        )
