from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderKind = Literal["anthropic", "openai", "openrouter"]

SIDECAR_FILE = "sidecar.yaml"
AGENT_FILE = "agent.yaml"
IDENTITY_FILE = "identity.pem"
DB_FILE = "sidecar.db"
HERMES_DIR = "hermes"


class PersonaTemplate(BaseModel):
    id: str
    display: str
    soul: str


class Personas(BaseModel):
    personas: list[PersonaTemplate] = []

    def get(self, persona_id: str) -> PersonaTemplate | None:
        return next((p for p in self.personas if p.id == persona_id), None)


class ProviderCred(BaseModel):
    name: str
    kind: ProviderKind
    api_key: str
    model: str


class TelegramCred(BaseModel):
    name: str
    token: str
    allowed_users: list[str] = []


class Secrets(BaseModel):
    providers: list[ProviderCred] = []
    telegram: list[TelegramCred] = []

    def provider(self, name: str) -> ProviderCred | None:
        return next((p for p in self.providers if p.name == name), None)

    def telegram_cred(self, name: str) -> TelegramCred | None:
        return next((t for t in self.telegram if t.name == name), None)


class TrustEntry(BaseModel):
    issuer: str
    accept: list[str] = ["org_affiliation"]


class TrustConfig(BaseModel):
    issuers: list[TrustEntry] = []
    from_contact: list[str] = []
    from_stranger: list[str] = ["org_affiliation"]


class SidecarConfig(BaseModel):
    name: str
    port: int
    mcp_port: int
    token: str
    trust: TrustConfig = TrustConfig()


class AgentConfig(BaseModel):
    provider: ProviderCred
    persona_id: str | None = None
    soul: str | None = None
    telegram: TelegramCred | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHADOWBOX_")

    home: Path = Path("~/.shadowbox")

    @property
    def home_dir(self) -> Path:
        return self.home.expanduser()

    @property
    def personas_file(self) -> Path:
        return self.home_dir / "personas.yaml"

    @property
    def secrets_file(self) -> Path:
        return self.home_dir / "secrets.yaml"

    @property
    def issuer_key_file(self) -> Path:
        return self.home_dir / "lab-issuer.pem"


def load_personas(settings: Settings) -> Personas:
    return Personas.model_validate(yaml.safe_load(settings.personas_file.read_text()))


def save_personas(settings: Settings, personas: Personas) -> None:
    settings.personas_file.write_text(
        yaml.safe_dump(personas.model_dump(), sort_keys=False)
    )


def load_secrets(settings: Settings) -> Secrets:
    return Secrets.model_validate(
        yaml.safe_load(settings.secrets_file.read_text()) or {}
    )


def save_secrets(settings: Settings, secrets: Secrets) -> None:
    settings.secrets_file.write_text(
        yaml.safe_dump(secrets.model_dump(), sort_keys=False)
    )
    settings.secrets_file.chmod(0o600)


def load_sidecar(directory: Path) -> SidecarConfig:
    return SidecarConfig.model_validate(
        yaml.safe_load((directory / SIDECAR_FILE).read_text())
    )


def save_sidecar(directory: Path, config: SidecarConfig) -> None:
    (directory / SIDECAR_FILE).write_text(
        yaml.safe_dump(config.model_dump(), sort_keys=False)
    )


def load_agent(directory: Path) -> AgentConfig | None:
    path = directory / AGENT_FILE
    if not path.exists():
        return None
    return AgentConfig.model_validate(yaml.safe_load(path.read_text()))


def save_agent(directory: Path, config: AgentConfig) -> None:
    path = directory / AGENT_FILE
    path.write_text(yaml.safe_dump(config.model_dump(), sort_keys=False))
    path.chmod(0o600)
