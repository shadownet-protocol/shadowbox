from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderKind = Literal["anthropic", "openai", "openrouter"]


class ShadowConfig(BaseModel):
    name: str
    port: int
    mcp_port: int
    token: str
    persona: str | None = None
    provider: str | None = None
    telegram: str | None = None


class Config(BaseModel):
    shadows: list[ShadowConfig]


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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHADOWBOX_")

    home: Path = Path("~/.shadowbox")

    @property
    def home_dir(self) -> Path:
        return self.home.expanduser()

    @property
    def config_file(self) -> Path:
        return self.home_dir / "config.yaml"

    @property
    def db_file(self) -> Path:
        return self.home_dir / "shadowbox.db"

    @property
    def keys_dir(self) -> Path:
        return self.home_dir / "keys"

    @property
    def personas_file(self) -> Path:
        return self.home_dir / "personas.yaml"

    @property
    def secrets_file(self) -> Path:
        return self.home_dir / "secrets.yaml"

    @property
    def hermes_dir(self) -> Path:
        return self.home_dir / "hermes"

    @property
    def trust_file(self) -> Path:
        return self.home_dir / "trust.yaml"

    @property
    def issuer_key_file(self) -> Path:
        return self.keys_dir / "lab-issuer.pem"


def load_config(settings: Settings) -> Config:
    return Config.model_validate(yaml.safe_load(settings.config_file.read_text()))


def save_config(settings: Settings, config: Config) -> None:
    settings.config_file.write_text(
        yaml.safe_dump(config.model_dump(exclude_none=True), sort_keys=False)
    )


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


def load_trust(settings: Settings) -> TrustConfig:
    return TrustConfig.model_validate(
        yaml.safe_load(settings.trust_file.read_text()) or {}
    )


def save_trust(settings: Settings, trust: TrustConfig) -> None:
    settings.trust_file.write_text(yaml.safe_dump(trust.model_dump(), sort_keys=False))
