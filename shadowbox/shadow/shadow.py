from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from shadowbox.address import Address
from shadowbox.config import (
    DB_FILE,
    HERMES_DIR,
    IDENTITY_FILE,
    AgentConfig,
    Settings,
    SidecarConfig,
    TrustConfig,
    load_agent,
    load_sidecar,
)
from shadowbox.crypto import PublicKey, SigningKey
from shadowbox.data.contacts import ContactStore, SqliteContactStore
from shadowbox.data.credential import CredentialStore, SqliteCredentialStore
from shadowbox.data.directives import DirectiveStore, SqliteDirectiveStore
from shadowbox.data.events import EventStore, SqliteEventStore
from shadowbox.data.messages import MessageStore, SqliteMessageStore
from shadowbox.shadow.agent import Agent, build_agent
from shadowbox.shadow.gateway import Gateway
from shadowbox.shadow.wire import Wire

if TYPE_CHECKING:
    from shadowbox.orchestrator import Orchestrator


class Shadow:
    """One Shadownet identity, self-contained in its own directory (named by pubkey)."""

    def __init__(
        self,
        settings: Settings,
        directory: Path,
        orchestrator: Orchestrator | None = None,
    ):
        self.settings = settings
        self.directory = directory
        self.orchestrator = orchestrator
        self._key: SigningKey | None = None
        self._sidecar: SidecarConfig | None = None
        self._agent_cfg: AgentConfig | None = None
        self._contacts: ContactStore | None = None
        self._directives: DirectiveStore | None = None
        self._messages: MessageStore | None = None
        self._credentials: CredentialStore | None = None
        self._events: EventStore | None = None
        self._gateway: Gateway | None = None
        self._agent: Agent | None = None
        self._wire: Wire | None = None

    @property
    def key_file(self) -> Path:
        return self.directory / IDENTITY_FILE

    @property
    def db_file(self) -> Path:
        return self.directory / DB_FILE

    @property
    def hermes_home(self) -> Path:
        return self.directory / HERMES_DIR

    @property
    def signing_key(self) -> SigningKey:
        if self._key is None:
            self._key = SigningKey.load(self.key_file)
        return self._key

    @property
    def public_key(self) -> PublicKey:
        return self.signing_key.public

    @property
    def sidecar(self) -> SidecarConfig:
        if self._sidecar is None:
            self._sidecar = load_sidecar(self.directory)
        return self._sidecar

    @property
    def agent_config(self) -> AgentConfig | None:
        if self._agent_cfg is None:
            self._agent_cfg = load_agent(self.directory)
        return self._agent_cfg

    def reload(self) -> None:
        self._sidecar = None
        self._agent_cfg = None
        self._agent = None

    @property
    def name(self) -> str:
        return self.sidecar.name

    @property
    def port(self) -> int:
        return self.sidecar.port

    @property
    def mcp_port(self) -> int:
        return self.sidecar.mcp_port

    @property
    def token(self) -> str:
        return self.sidecar.token

    @property
    def trust(self) -> TrustConfig:
        return self.sidecar.trust

    @property
    def persona(self) -> str | None:
        cfg = self.agent_config
        return cfg.persona_id if cfg else None

    @property
    def provider(self) -> str | None:
        cfg = self.agent_config
        return cfg.provider.name if cfg else None

    @property
    def has_agent(self) -> bool:
        return self.agent_config is not None

    @property
    def address(self) -> Address:
        return Address.direct(self.public_key, "localhost", self.port)

    @property
    def uri(self) -> str:
        return self.address.uri

    @property
    def contacts(self) -> ContactStore:
        if self._contacts is None:
            self._contacts = SqliteContactStore(self.db_file)
        return self._contacts

    @property
    def directives(self) -> DirectiveStore:
        if self._directives is None:
            self._directives = SqliteDirectiveStore(self.db_file)
        return self._directives

    @property
    def messages(self) -> MessageStore:
        if self._messages is None:
            self._messages = SqliteMessageStore(self.db_file)
        return self._messages

    @property
    def credentials(self) -> CredentialStore:
        if self._credentials is None:
            self._credentials = SqliteCredentialStore(self.db_file)
        return self._credentials

    @property
    def events(self) -> EventStore:
        if self._events is None:
            self._events = SqliteEventStore(self.db_file)
        return self._events

    @property
    def gateway(self) -> Gateway:
        if self._gateway is None:
            self._gateway = Gateway(self)
        return self._gateway

    @property
    def agent(self) -> Agent:
        if self._agent is None:
            self._agent = build_agent(self)
        return self._agent

    @property
    def wire(self) -> Wire:
        if self._wire is None:
            self._wire = Wire(self)
        return self._wire

    def close(self) -> None:
        if self._agent is not None:
            self._agent.kill()
        for component in (self._gateway, self._wire):
            if component is not None:
                component.stop()
        for store in (
            self._contacts,
            self._directives,
            self._messages,
            self._credentials,
            self._events,
        ):
            if store is not None:
                store.close()
        self._contacts = self._directives = self._messages = None
        self._credentials = self._events = None
