from __future__ import annotations

import asyncio
import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from shadowbox.defaults import SHADOW_SOUL

if TYPE_CHECKING:
    from shadowbox.shadow.shadow import Shadow

SKILLS_SRC = Path(__file__).resolve().parent.parent / "skills"

ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai-api": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


class Agent(ABC):
    """A supervised host-LLM process behind a Shadow. One subclass per backend."""

    def __init__(self, shadow: Shadow):
        self.shadow = shadow
        self._proc: asyncio.subprocess.Process | None = None
        self._log = None

    @property
    @abstractmethod
    def home(self) -> Path: ...

    @property
    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    def generate(self) -> list[str]: ...

    @abstractmethod
    def launch_command(self) -> str: ...

    @abstractmethod
    def _command(self) -> list[str] | None: ...

    @property
    def log_file(self) -> Path:
        return self.home / "agent.log"

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def status(self) -> str:
        if self._proc is None:
            return "stopped"
        if self._proc.returncode is None:
            return "running"
        return f"exited({self._proc.returncode})"

    async def start(self) -> None:
        if self.running:
            return
        if not self.configured:
            raise RuntimeError(f"{self.shadow.name} has no host LLM configured")
        command = self._command()
        if command is None:
            raise RuntimeError("host LLM executable not found on PATH")
        self.home.mkdir(parents=True, exist_ok=True)
        self._log = self.log_file.open("ab")
        self._proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.home,
            env={**os.environ, "HOME": str(self.home)},
            stdout=self._log,
            stderr=self._log,
        )

    async def stop(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._close_log()
        self._proc = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    def kill(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.kill()
        self._close_log()
        self._proc = None

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None

    def log_tail(self, lines: int = 50) -> str:
        if not self.log_file.exists():
            return ""
        return "\n".join(
            self.log_file.read_text(errors="replace").splitlines()[-lines:]
        )


class HermesAgent(Agent):
    """Nous Research hermes-agent, supervised in an isolated HOME."""

    role = "agent"

    @property
    def home(self) -> Path:
        return self.shadow.hermes_home / self.role

    @property
    def dot(self) -> Path:
        return self.home / ".hermes"

    @property
    def configured(self) -> bool:
        return (self.dot / "config.yaml").exists()

    def launch_command(self) -> str:
        return f"HOME={self.home} uv run hermes gateway run"

    def _command(self) -> list[str] | None:
        exe = shutil.which("hermes")
        return [exe, "gateway", "run"] if exe else None

    def generate(self) -> list[str]:
        cfg = self.shadow.agent_config
        if cfg is None:
            return []
        self.dot.mkdir(parents=True, exist_ok=True)
        config: dict = {
            "model": {"default": cfg.provider.model, "provider": cfg.provider.kind},
            "mcp_servers": {
                "shadownet": {
                    "url": f"http://127.0.0.1:{self.shadow.mcp_port}/mcp/agent",
                    "headers": {
                        "Authorization": f"Bearer {self.shadow.token_for('agent')}"
                    },
                }
            },
        }
        env = [f"{ENV_KEYS[cfg.provider.kind]}={cfg.provider.api_key}"]
        if cfg.telegram:
            env.append(f"TELEGRAM_BOT_TOKEN={cfg.telegram.token}")
            if cfg.telegram.allowed_users:
                env.append(
                    "TELEGRAM_ALLOWED_USERS=" + ",".join(cfg.telegram.allowed_users)
                )
                config["gateway"] = {
                    "platforms": {
                        "telegram": {
                            "extra": {"allow_from": cfg.telegram.allowed_users}
                        }
                    }
                }
        (self.dot / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        env_file = self.dot / ".env"
        env_file.write_text("\n".join(env) + "\n")
        env_file.chmod(0o600)
        soul = SHADOW_SOUL.format(name=self.shadow.name)
        if cfg.soul:
            soul += f"\n\n# Persona\n\n{cfg.soul}\n"
        (self.dot / "SOUL.md").write_text(soul)
        self._install_skills()
        return [f"wrote {self.dot}", f"launch: {self.launch_command()}"]

    def _install_skills(self) -> None:
        src = SKILLS_SRC / self.role
        if not src.is_dir():
            return
        dest = self.dot / "skills"
        for skill in src.iterdir():
            if skill.is_dir() and (skill / "SKILL.md").exists():
                shutil.copytree(skill, dest / skill.name, dirs_exist_ok=True)


AGENTS = {"hermes": HermesAgent}


def build_agent(shadow: Shadow) -> Agent:
    cfg = shadow.agent_config
    kind = cfg.kind if cfg is not None else "hermes"
    return AGENTS[kind](shadow)
