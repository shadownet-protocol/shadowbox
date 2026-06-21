import asyncio
import shutil
import socket
import time
from secrets import token_urlsafe

import httpx

from shadowbox.address import Address
from shadowbox.config import (
    AGENT_FILE,
    IDENTITY_FILE,
    SIDECAR_FILE,
    AgentConfig,
    Personas,
    ProviderCred,
    ProviderKind,
    Secrets,
    Settings,
    SidecarConfig,
    TelegramCred,
    TrustConfig,
    TrustEntry,
    load_personas,
    load_secrets,
    load_sidecar,
    save_agent,
    save_personas,
    save_secrets,
    save_sidecar,
)
from shadowbox.crypto import SigningKey
from shadowbox.data.agentcard import AgentCard
from shadowbox.data.credential import Credential
from shadowbox.data.envelope import WireError
from shadowbox.defaults import DEFAULT_SHADOWS, DEFAULT_TEMPLATES
from shadowbox.shadow import Shadow


def free_ports(count: int) -> list[int]:
    socks = [socket.socket() for _ in range(count)]
    try:
        for sock in socks:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in socks]
    finally:
        for sock in socks:
            sock.close()


class OrchestratorError(Exception):
    pass


class Orchestrator:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self._shadows: dict[str, Shadow] | None = None
        self._tasks: dict[str, dict[str, asyncio.Task]] = {}
        self._log: list[str] = []

    def _scan(self) -> dict[str, Shadow]:
        shadows: dict[str, Shadow] = {}
        if not self.settings.home_dir.exists():
            return shadows
        for directory in sorted(self.settings.home_dir.iterdir()):
            if not directory.is_dir():
                continue
            if not (directory / SIDECAR_FILE).exists():
                continue
            if not (directory / IDENTITY_FILE).exists():
                continue
            shadow = Shadow(self.settings, directory, self)
            shadows[shadow.name] = shadow
        return shadows

    def _map(self) -> dict[str, Shadow]:
        if self._shadows is None:
            self._shadows = self._scan()
        return self._shadows

    def _invalidate(self) -> None:
        if self._shadows is not None:
            for shadow in self._shadows.values():
                shadow.close()
        self._shadows = None
        self._tasks.clear()

    @property
    def shadows(self) -> list[Shadow]:
        return list(self._map().values())

    def get(self, name: str) -> Shadow:
        shadows = self._map()
        if name not in shadows:
            raise OrchestratorError(f"no shadow named {name}")
        return shadows[name]

    def address_for_pk(self, pk: str) -> Address | None:
        for shadow in self.shadows:
            if shadow.public_key.multibase == pk:
                return shadow.address
        return None

    async def discover(self, requester: Shadow, to: str) -> tuple[str, str]:
        detail = requester.contacts.try_detail(to)
        if detail is not None:
            recipient_pk, endpoint = detail.pk, detail.endpoint
        else:
            try:
                addr = Address.parse(to)
            except ValueError:
                raise WireError("unknown_recipient", 404) from None
            if addr.public_key is None:
                raise WireError("unknown_recipient", 404)
            recipient_pk = addr.public_key.multibase
            if addr.endpoint is not None:
                endpoint = addr.endpoint
            else:
                local = self.address_for_pk(recipient_pk)
                if local is None:
                    raise WireError("unknown_recipient", 404)
                endpoint = local.endpoint
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    f"{endpoint}/.well-known/agent-card.json", timeout=10
                )
            except httpx.HTTPError:
                raise WireError("unknown_recipient", 404) from None
        if resp.status_code != 200:
            raise WireError("unknown_recipient", 404)
        try:
            card = AgentCard.verify(resp.json(), recipient_pk)
        except ValueError:
            raise WireError("signature", 401) from None
        return recipient_pk, card.url

    def log(self, line: str) -> None:
        self._log.append(line)

    def drain_log(self) -> list[str]:
        lines = self._log[:]
        self._log.clear()
        return lines

    def running(self, name: str) -> bool:
        return name in self._tasks

    def subsystems(self, name: str) -> dict:
        tasks = self._tasks.get(name, {})

        def live(key: str) -> bool:
            task = tasks.get(key)
            return task is not None and not task.done()

        shadow = self.get(name)
        agent = shadow.agent.status() if shadow.has_agent else None
        return {"gateway": live("gateway"), "a2a": live("a2a"), "agent": agent}

    async def _guard(self, coro, name: str, which: str) -> None:
        try:
            await coro
        except SystemExit:
            self.log(f"{name} {which} could not bind (port busy)")
        except Exception as exc:
            self.log(f"{name} {which} crashed: {exc}")

    def start(self, name: str) -> None:
        if name in self._tasks:
            return
        shadow = self.get(name)
        loop = asyncio.get_running_loop()
        self._tasks[name] = {
            "gateway": loop.create_task(
                self._guard(shadow.gateway.serve(), name, "gateway")
            ),
            "a2a": loop.create_task(self._guard(shadow.wire.serve(), name, "a2a")),
        }
        self.log(f"{name} gateway :{shadow.mcp_port} + a2a :{shadow.port} up")

    def start_all(self) -> None:
        for shadow in self.shadows:
            self.start(shadow.name)

    async def up(self, name: str) -> None:
        self.start(name)
        shadow = self.get(name)
        if shadow.has_agent:
            try:
                await shadow.agent.start()
                self.log(f"{name} host LLM up")
            except RuntimeError as exc:
                self.log(f"{name} host LLM failed: {exc}")

    async def down(self, name: str) -> None:
        shadow = self.get(name)
        shadow.gateway.stop()
        shadow.wire.stop()
        tasks = self._tasks.pop(name, {})
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        if shadow.has_agent:
            await shadow.agent.stop()
        self.log(f"{name} down")

    def stop_all(self) -> None:
        for tasks in self._tasks.values():
            for task in tasks.values():
                task.cancel()
        self._tasks.clear()
        if self._shadows is not None:
            for shadow in self._shadows.values():
                shadow.close()

    async def start_agent(self, name: str) -> None:
        await self.get(name).agent.start()

    async def stop_agent(self, name: str) -> None:
        await self.get(name).agent.stop()

    def agent_status(self, name: str) -> str:
        return self.get(name).agent.status()

    async def wait(self) -> None:
        tasks = [t for subsystems in self._tasks.values() for t in subsystems.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def state(self) -> tuple[str, str]:
        s = self.settings
        lab_files = [s.personas_file, s.secrets_file, s.issuer_key_file]
        if not s.home_dir.exists():
            return "fresh", ""
        present = [f for f in lab_files if f.exists()]
        shadow_dirs = [
            d
            for d in s.home_dir.iterdir()
            if d.is_dir() and (d / SIDECAR_FILE).exists()
        ]
        if not present and not shadow_dirs:
            return "fresh", ""
        for f in lab_files:
            if not f.exists():
                return "broken", f"{f.name} missing"
        for d in shadow_dirs:
            if not (d / IDENTITY_FILE).exists():
                return "broken", f"{d.name}/{IDENTITY_FILE} missing"
            try:
                load_sidecar(d)
            except Exception as exc:
                return "broken", f"{d.name} unreadable: {exc}"
        return "ok", ""

    def plan(self) -> list[str]:
        s = self.settings
        return [
            f"create at {s.home_dir}:",
            "  lab-issuer.pem  (stranger-review issuer)",
            "  personas.yaml  (seed templates)" if not s.personas_file.exists() else "",
            "  secrets.yaml  (empty pool)" if not s.secrets_file.exists() else "",
            f"  one dir per shadow ({', '.join(DEFAULT_SHADOWS)}), named by key,",
            "    each: identity.pem · sidecar.yaml · sidecar.db",
        ]

    def _lab_trust(self, issuer: SigningKey) -> TrustConfig:
        return TrustConfig(
            issuers=[TrustEntry(issuer=issuer.multibase, accept=["org_affiliation"])]
        )

    def _create_shadow(
        self,
        name: str,
        port: int,
        mcp_port: int,
        issuer: SigningKey,
        trust: TrustConfig,
        agent_cfg: AgentConfig | None,
    ) -> Shadow:
        key = SigningKey.generate()
        directory = self.settings.home_dir / key.multibase
        directory.mkdir(parents=True)
        key.save(directory / IDENTITY_FILE)
        save_sidecar(
            directory,
            SidecarConfig(
                name=name,
                port=port,
                mcp_port=mcp_port,
                token=token_urlsafe(16),
                trust=trust,
            ),
        )
        if agent_cfg is not None:
            save_agent(directory, agent_cfg)
        shadow = Shadow(self.settings, directory, self)
        now = int(time.time())
        shadow.credentials.add(
            Credential.mint(issuer, key.multibase, now), now + 30 * 86400
        )
        if agent_cfg is not None:
            shadow.agent.generate()
        shadow.close()
        return shadow

    def initialize(self) -> list[str]:
        s = self.settings
        s.home_dir.mkdir(parents=True, exist_ok=True)
        lines = [f"created {s.home_dir}"]
        issuer = SigningKey.generate()
        issuer.save(s.issuer_key_file)
        lines.append(f"wrote lab-issuer.pem ({issuer.multibase[:16]}…)")
        if not s.personas_file.exists():
            save_personas(s, Personas(personas=DEFAULT_TEMPLATES))
            lines.append("wrote personas.yaml")
        if not s.secrets_file.exists():
            save_secrets(s, Secrets())
            lines.append("wrote secrets.yaml (add provider/telegram keys)")
        trust = self._lab_trust(issuer)
        ports = free_ports(2 * len(DEFAULT_SHADOWS))
        for i, name in enumerate(DEFAULT_SHADOWS):
            shadow = self._create_shadow(
                name, ports[2 * i], ports[2 * i + 1], issuer, trust, None
            )
            lines.append(f"{name}: {shadow.uri}")
        self._invalidate()
        return lines

    def wipe(self) -> list[str]:
        s = self.settings
        self._invalidate()
        keep = {s.personas_file.name, s.secrets_file.name}
        lines: list[str] = []
        if s.home_dir.exists():
            for entry in s.home_dir.iterdir():
                if entry.name in keep:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                lines.append(f"deleted {entry.name}")
        return lines

    def _agent_config(
        self, persona: str | None, provider: str | None, telegram: str | None
    ) -> AgentConfig | None:
        s = self.settings
        template = None
        if persona is not None:
            template = load_personas(s).get(persona)
            if template is None:
                raise OrchestratorError(f"unknown persona {persona}")
        secrets = load_secrets(s)
        provider_cred = None
        if provider is not None:
            provider_cred = secrets.provider(provider)
            if provider_cred is None:
                raise OrchestratorError(f"unknown provider {provider}")
        telegram_cred = None
        if telegram is not None:
            telegram_cred = secrets.telegram_cred(telegram)
            if telegram_cred is None:
                raise OrchestratorError(f"unknown telegram key {telegram}")
        if (template or telegram_cred) and provider_cred is None:
            raise OrchestratorError("persona/telegram need a provider")
        if provider_cred is None:
            return None
        return AgentConfig(
            provider=provider_cred,
            persona_id=persona,
            soul=template.soul if template else None,
            telegram=telegram_cred,
        )

    def add_shadow(
        self,
        name: str,
        persona: str | None = None,
        provider: str | None = None,
        telegram: str | None = None,
    ) -> tuple[Shadow, list[str]]:
        if name in self._map():
            raise OrchestratorError(f"shadow {name} already exists")
        agent_cfg = self._agent_config(persona, provider, telegram)
        issuer = SigningKey.load(self.settings.issuer_key_file)
        port, mcp_port = free_ports(2)
        self._create_shadow(
            name, port, mcp_port, issuer, self._lab_trust(issuer), agent_cfg
        )
        self._invalidate()
        shadow = self.get(name)
        lines = [f"created {shadow.uri}"]
        if agent_cfg is not None:
            lines.append(f"host LLM configured ({provider})")
        return shadow, lines

    async def configure_shadow(
        self,
        name: str,
        persona: str | None = None,
        provider: str | None = None,
        telegram: str | None = None,
    ) -> None:
        shadow = self.get(name)
        agent_cfg = self._agent_config(persona, provider, telegram)
        running = shadow.has_agent and shadow.agent.status() == "running"
        if shadow.has_agent:
            await shadow.agent.stop()
        if agent_cfg is None:
            (shadow.directory / AGENT_FILE).unlink(missing_ok=True)
            if shadow.hermes_home.exists():
                shutil.rmtree(shadow.hermes_home)
        else:
            save_agent(shadow.directory, agent_cfg)
        shadow.reload()
        if agent_cfg is not None:
            shadow.agent.generate()
            if running or self.running(name):
                try:
                    await shadow.agent.start()
                except RuntimeError as exc:
                    self.log(f"{name} host LLM failed: {exc}")
        self.log(f"{name} reconfigured")

    async def remove_shadow(self, name: str) -> None:
        directory = self.get(name).directory
        await self.down(name)
        self._invalidate()
        shutil.rmtree(directory)
        self.log(f"{name} removed")

    def providers(self) -> list[ProviderCred]:
        return load_secrets(self.settings).providers

    def telegrams(self) -> list[TelegramCred]:
        return load_secrets(self.settings).telegram

    def add_provider(
        self, name: str, kind: ProviderKind, model: str, api_key: str
    ) -> None:
        secrets = load_secrets(self.settings)
        secrets.providers = [p for p in secrets.providers if p.name != name]
        secrets.providers.append(
            ProviderCred(name=name, kind=kind, model=model, api_key=api_key)
        )
        save_secrets(self.settings, secrets)

    def remove_provider(self, name: str) -> None:
        secrets = load_secrets(self.settings)
        secrets.providers = [p for p in secrets.providers if p.name != name]
        save_secrets(self.settings, secrets)

    def add_telegram(self, name: str, token: str, allowed_users: list[str]) -> None:
        secrets = load_secrets(self.settings)
        secrets.telegram = [t for t in secrets.telegram if t.name != name]
        secrets.telegram.append(
            TelegramCred(name=name, token=token, allowed_users=allowed_users)
        )
        save_secrets(self.settings, secrets)

    def remove_telegram(self, name: str) -> None:
        secrets = load_secrets(self.settings)
        secrets.telegram = [t for t in secrets.telegram if t.name != name]
        save_secrets(self.settings, secrets)


def main() -> None:
    async def run() -> None:
        orchestrator = Orchestrator()
        st, reason = orchestrator.state()
        if st != "ok":
            raise SystemExit(
                f"lab is {st}{': ' + reason if reason else ''}"
                " — run shadowbox to initialize"
            )
        orchestrator.start_all()
        for shadow in orchestrator.shadows:
            print(
                f"{shadow.name}: gateway http://127.0.0.1:{shadow.mcp_port}/mcp"
                f"  wire http://127.0.0.1:{shadow.port}"
            )
        await orchestrator.wait()

    asyncio.run(run())
