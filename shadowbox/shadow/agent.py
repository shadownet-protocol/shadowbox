from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from shadowbox.config import PersonaTemplate, ProviderCred, TelegramCred

if TYPE_CHECKING:
    from shadowbox.shadow.shadow import Shadow

ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


class Agent:
    """The host LLM behind a Shadow: a Hermes installation in an isolated HOME."""

    def __init__(self, shadow: Shadow):
        self.shadow = shadow

    @property
    def home(self) -> Path:
        return self.shadow.settings.hermes_dir / self.shadow.name

    @property
    def dot(self) -> Path:
        return self.home / ".hermes"

    @property
    def configured(self) -> bool:
        return (self.dot / "config.yaml").exists()

    def launch_command(self) -> str:
        return f"HOME={self.home} uv run hermes"

    def generate(
        self,
        provider: ProviderCred,
        persona: PersonaTemplate | None = None,
        telegram: TelegramCred | None = None,
    ) -> list[str]:
        self.dot.mkdir(parents=True, exist_ok=True)
        config: dict = {
            "model": {"default": provider.model, "provider": provider.kind},
            "mcp_servers": {
                "shadownet": {
                    "url": f"http://127.0.0.1:{self.shadow.config.mcp_port}/mcp",
                    "headers": {"Authorization": f"Bearer {self.shadow.config.token}"},
                }
            },
        }
        env = [f"{ENV_KEYS[provider.kind]}={provider.api_key}"]
        if telegram:
            env.append(f"TELEGRAM_BOT_TOKEN={telegram.token}")
            if telegram.allowed_users:
                env.append("TELEGRAM_ALLOWED_USERS=" + ",".join(telegram.allowed_users))
                config["gateway"] = {
                    "platforms": {
                        "telegram": {"extra": {"allow_from": telegram.allowed_users}}
                    }
                }
        (self.dot / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        env_file = self.dot / ".env"
        env_file.write_text("\n".join(env) + "\n")
        env_file.chmod(0o600)
        if persona:
            (self.dot / "SOUL.md").write_text(persona.soul)
        return [f"wrote {self.dot}", f"launch: {self.launch_command()}"]
