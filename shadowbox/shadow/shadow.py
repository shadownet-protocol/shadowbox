from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from shadowbox.address import Address
from shadowbox.config import Settings, ShadowConfig, TrustConfig, load_trust
from shadowbox.crypto import PublicKey, SigningKey
from shadowbox.data.contacts import ContactStore, SqliteContactStore
from shadowbox.data.credential import CredentialStore, SqliteCredentialStore
from shadowbox.data.directives import DirectiveStore, SqliteDirectiveStore
from shadowbox.data.messages import MessageStore, SqliteMessageStore
from shadowbox.shadow.agent import Agent
from shadowbox.shadow.gateway import Gateway
from shadowbox.shadow.wire import Wire

if TYPE_CHECKING:
    from shadowbox.orchestrator import Orchestrator


class Shadow:
    """One Shadownet identity: its key, stores, gateway, agent, and wire."""

    def __init__(
        self,
        settings: Settings,
        config: ShadowConfig,
        orchestrator: Orchestrator | None = None,
    ):
        self.settings = settings
        self.config = config
        self.orchestrator = orchestrator
        self._key: SigningKey | None = None
        self._contacts: ContactStore | None = None
        self._directives: DirectiveStore | None = None
        self._messages: MessageStore | None = None
        self._credentials: CredentialStore | None = None
        self._trust: TrustConfig | None = None
        self._gateway: Gateway | None = None
        self._agent: Agent | None = None
        self._wire: Wire | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def key_file(self) -> Path:
        return self.settings.keys_dir / f"{self.config.name}.pem"

    @property
    def signing_key(self) -> SigningKey:
        if self._key is None:
            self._key = SigningKey.load(self.key_file)
        return self._key

    @property
    def public_key(self) -> PublicKey:
        return self.signing_key.public

    @property
    def address(self) -> Address:
        return Address.direct(self.public_key, "localhost", self.config.port)

    @property
    def uri(self) -> str:
        return self.address.uri

    @property
    def contacts(self) -> ContactStore:
        if self._contacts is None:
            self._contacts = SqliteContactStore(self.settings, self.name)
        return self._contacts

    @property
    def directives(self) -> DirectiveStore:
        if self._directives is None:
            self._directives = SqliteDirectiveStore(self.settings, self.name)
        return self._directives

    @property
    def messages(self) -> MessageStore:
        if self._messages is None:
            self._messages = SqliteMessageStore(self.settings, self.name)
        return self._messages

    @property
    def credentials(self) -> CredentialStore:
        if self._credentials is None:
            self._credentials = SqliteCredentialStore(self.settings, self.name)
        return self._credentials

    @property
    def trust(self) -> TrustConfig:
        if self._trust is None:
            self._trust = (
                load_trust(self.settings)
                if self.settings.trust_file.exists()
                else TrustConfig()
            )
        return self._trust

    @property
    def gateway(self) -> Gateway:
        if self._gateway is None:
            self._gateway = Gateway(self)
        return self._gateway

    @property
    def agent(self) -> Agent:
        if self._agent is None:
            self._agent = Agent(self)
        return self._agent

    @property
    def wire(self) -> Wire:
        if self._wire is None:
            self._wire = Wire(self)
        return self._wire

    def close(self) -> None:
        for component in (self._gateway, self._wire):
            if component is not None:
                component.stop()
        for store in (
            self._contacts,
            self._directives,
            self._messages,
            self._credentials,
        ):
            if store is not None:
                store.close()
        self._contacts = self._directives = self._messages = None
        self._credentials = None
