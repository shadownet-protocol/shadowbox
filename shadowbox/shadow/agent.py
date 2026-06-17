from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from shadowbox.shadow.shadow import Shadow

ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


class Agent:
    """The host LLM behind a Shadow: a supervised Hermes process in an isolated HOME."""

    def __init__(self, shadow: Shadow):
        self.shadow = shadow
        self._proc: asyncio.subprocess.Process | None = None
        self._log = None

    @property
    def home(self) -> Path:
        return self.shadow.hermes_home

    @property
    def dot(self) -> Path:
        return self.home / ".hermes"

    @property
    def log_file(self) -> Path:
        return self.home / "agent.log"

    @property
    def configured(self) -> bool:
        return (self.dot / "config.yaml").exists()

    def launch_command(self) -> str:
        return f"HOME={self.home} uv run hermes"

    def generate(self) -> list[str]:
        cfg = self.shadow.agent_config
        if cfg is None:
            return []
        self.dot.mkdir(parents=True, exist_ok=True)
        config: dict = {
            "model": {"default": cfg.provider.model, "provider": cfg.provider.kind},
            "mcp_servers": {
                "shadownet": {
                    "url": f"http://127.0.0.1:{self.shadow.mcp_port}/mcp",
                    "headers": {"Authorization": f"Bearer {self.shadow.token}"},
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
        if cfg.soul:
            (self.dot / "SOUL.md").write_text(cfg.soul)
        return [f"wrote {self.dot}", f"launch: {self.launch_command()}"]

    def _command(self) -> list[str] | None:
        exe = shutil.which("hermes")
        return [exe, "gateway", "run"] if exe else None

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
            raise RuntimeError("hermes executable not found on PATH")
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
