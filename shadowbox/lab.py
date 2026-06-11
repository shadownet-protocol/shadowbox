import shutil
import sqlite3
from pathlib import Path
from secrets import token_urlsafe

from shadowbox import crypto
from shadowbox.config import (
    Config,
    Personas,
    PersonaTemplate,
    Secrets,
    Settings,
    ShadowConfig,
    load_config,
    load_personas,
    load_secrets,
    save_config,
    save_personas,
    save_secrets,
)
from shadowbox.contacts import ContactStore
from shadowbox.hermes import HermesHome

DEFAULT_SHADOWS = [("alice", 7401, 8401), ("bob", 7402, 8402)]

DEFAULT_TEMPLATES = [
    PersonaTemplate(
        id="negotiator",
        display="hard-nosed negotiator",
        soul=(
            "You negotiate firmly but fairly on your Subject's behalf. You never"
            " accept a first offer, you always know your walk-away point, and you"
            " keep your Subject's interests above rapport."
        ),
    ),
    PersonaTemplate(
        id="scheduler",
        display="calendar wrangler",
        soul=(
            "You manage your Subject's time. You propose concrete slots, decline"
            " politely when the calendar is full, and never double-book."
        ),
    ),
]


class LabError(Exception):
    pass


class Shadow:
    def __init__(self, settings: Settings, config: ShadowConfig):
        self.settings = settings
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def key_file(self) -> Path:
        return self.settings.keys_dir / f"{self.config.name}.pem"

    @property
    def uri(self) -> str:
        key = crypto.load_key(self.key_file)
        return (
            f"shadow://key:{crypto.public_multibase(key)}"
            f"@localhost:{self.config.port}"
        )

    def contacts(self) -> ContactStore:
        return ContactStore(self.settings, self.config.name)

    def hermes(self) -> HermesHome:
        return HermesHome(self.settings, self.config)


class Lab:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()

    def state(self) -> tuple[str, str]:
        s = self.settings
        if not any(p.exists() for p in (s.config_file, s.db_file, s.keys_dir)):
            return "fresh", ""
        if not s.config_file.exists():
            return "broken", f"{s.config_file.name} missing"
        try:
            config = load_config(s)
        except Exception as exc:
            return "broken", f"{s.config_file.name} unreadable: {exc}"
        for shadow in config.shadows:
            if not (s.keys_dir / f"{shadow.name}.pem").exists():
                return "broken", f"keys/{shadow.name}.pem missing"
        for required in (s.db_file, s.personas_file, s.secrets_file):
            if not required.exists():
                return "broken", f"{required.name} missing"
        return "ok", ""

    def plan(self) -> list[str]:
        s = self.settings
        lines = [f"create at {s.home_dir}:"]
        for name, port, mcp_port in DEFAULT_SHADOWS:
            lines.append(
                f"  keys/{name}.pem  (new Ed25519 identity,"
                f" wire :{port}, mcp :{mcp_port})"
            )
        lines.append(f"  config.yaml  ({len(DEFAULT_SHADOWS)} shadows)")
        lines.append("  shadowbox.db")
        if not s.personas_file.exists():
            lines.append("  personas.yaml  (seed persona templates)")
        if not s.secrets_file.exists():
            lines.append("  secrets.yaml  (empty; add provider/telegram keys)")
        lines.append("  hermes/")
        return lines

    def initialize(self) -> list[str]:
        s = self.settings
        s.keys_dir.mkdir(parents=True, exist_ok=True)
        s.hermes_dir.mkdir(exist_ok=True)
        lines = [f"created {s.home_dir}"]

        shadows: list[ShadowConfig] = []
        for name, port, mcp_port in DEFAULT_SHADOWS:
            key = crypto.generate_key()
            crypto.save_key(key, s.keys_dir / f"{name}.pem")
            shadows.append(
                ShadowConfig(
                    name=name, port=port, mcp_port=mcp_port, token=token_urlsafe(16)
                )
            )
            lines.append(
                f"{name}: shadow://key:{crypto.public_multibase(key)}"
                f"@localhost:{port}"
            )
        save_config(s, Config(shadows=shadows))
        lines.append(f"wrote {s.config_file}")

        if not s.personas_file.exists():
            save_personas(s, Personas(personas=DEFAULT_TEMPLATES))
            lines.append(f"wrote {s.personas_file}")
        if not s.secrets_file.exists():
            save_secrets(s, Secrets())
            lines.append(f"wrote {s.secrets_file} (add your keys here)")

        sqlite3.connect(s.db_file).close()
        lines.append(f"created {s.db_file}")
        return lines

    def wipe(self) -> list[str]:
        s = self.settings
        lines: list[str] = []
        for directory in (s.keys_dir, s.hermes_dir):
            if directory.exists():
                shutil.rmtree(directory)
                lines.append(f"deleted {directory}")
        for f in (s.config_file, s.db_file):
            if f.exists():
                f.unlink()
                lines.append(f"deleted {f}")
        return lines

    def shadows(self) -> list[Shadow]:
        return [Shadow(self.settings, c) for c in load_config(self.settings).shadows]

    def get(self, name: str) -> Shadow:
        shadow = next((s for s in self.shadows() if s.name == name), None)
        if shadow is None:
            raise LabError(f"no shadow named {name}")
        return shadow

    def add_shadow(
        self,
        name: str,
        persona: str | None = None,
        provider: str | None = None,
        telegram: str | None = None,
    ) -> tuple[Shadow, list[str]]:
        s = self.settings
        config = load_config(s)
        if any(sh.name == name for sh in config.shadows):
            raise LabError(f"shadow {name} already exists")

        template = None
        if persona is not None:
            template = load_personas(s).get(persona)
            if template is None:
                raise LabError(f"unknown persona {persona}")
        secrets = load_secrets(s)
        provider_cred = None
        if provider is not None:
            provider_cred = secrets.provider(provider)
            if provider_cred is None:
                raise LabError(f"unknown provider {provider}")
        telegram_cred = None
        if telegram is not None:
            telegram_cred = secrets.telegram_cred(telegram)
            if telegram_cred is None:
                raise LabError(f"unknown telegram key {telegram}")
        if (template or telegram_cred) and provider_cred is None:
            raise LabError("persona/telegram require a provider")

        shadow_config = ShadowConfig(
            name=name,
            port=max((sh.port for sh in config.shadows), default=7400) + 1,
            mcp_port=max((sh.mcp_port for sh in config.shadows), default=8400) + 1,
            token=token_urlsafe(16),
            persona=persona,
            provider=provider,
            telegram=telegram,
        )
        key = crypto.generate_key()
        crypto.save_key(key, s.keys_dir / f"{name}.pem")
        config.shadows.append(shadow_config)
        save_config(s, config)

        shadow = Shadow(s, shadow_config)
        lines = [f"created {shadow.uri}"]
        if provider_cred is not None:
            lines += shadow.hermes().generate(provider_cred, template, telegram_cred)
        return shadow, lines