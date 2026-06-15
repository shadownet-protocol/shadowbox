import asyncio
import shutil
import sqlite3
import time
from secrets import token_urlsafe

import httpx

from shadowbox.address import Address
from shadowbox.config import (
    Config,
    Personas,
    PersonaTemplate,
    Secrets,
    Settings,
    ShadowConfig,
    TrustConfig,
    TrustEntry,
    load_config,
    load_personas,
    load_secrets,
    save_config,
    save_personas,
    save_secrets,
    save_trust,
)
from shadowbox.crypto import SigningKey
from shadowbox.data.agentcard import AgentCard
from shadowbox.data.credential import Credential, SqliteCredentialStore
from shadowbox.data.envelope import WireError
from shadowbox.shadow import Shadow

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


class OrchestratorError(Exception):
    pass


class Orchestrator:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self._shadows: dict[str, Shadow] | None = None
        self._running: set[str] = set()
        self._tasks: list[asyncio.Task] = []

    def _map(self) -> dict[str, Shadow]:
        if self._shadows is None:
            self._shadows = {
                c.name: Shadow(self.settings, c, self)
                for c in load_config(self.settings).shadows
            }
        return self._shadows

    def _invalidate(self) -> None:
        if self._shadows is not None:
            for shadow in self._shadows.values():
                shadow.close()
        self._shadows = None
        self._running.clear()

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

    def running(self, name: str) -> bool:
        return name in self._running

    def start(self, name: str) -> None:
        if name in self._running:
            return
        shadow = self.get(name)
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(shadow.gateway.serve()))
        self._tasks.append(loop.create_task(shadow.wire.serve()))
        self._running.add(name)

    def start_all(self) -> None:
        for shadow in self.shadows:
            self.start(shadow.name)

    def stop_all(self) -> None:
        for shadow in self.shadows:
            shadow.close()
        self._running.clear()
        self._tasks.clear()

    async def wait(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks)

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
        for required in (
            s.db_file,
            s.personas_file,
            s.secrets_file,
            s.trust_file,
            s.issuer_key_file,
        ):
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
        lines.append("  keys/lab-issuer.pem + trust.yaml  (stranger-review issuer)")
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

        issuer = SigningKey.generate()
        issuer.save(s.issuer_key_file)
        save_trust(
            s,
            TrustConfig(
                issuers=[
                    TrustEntry(issuer=issuer.multibase, accept=["org_affiliation"])
                ]
            ),
        )
        lines.append(f"wrote {s.trust_file} (lab issuer {issuer.multibase[:16]}…)")

        shadows: list[ShadowConfig] = []
        keys: dict[str, SigningKey] = {}
        for name, port, mcp_port in DEFAULT_SHADOWS:
            key = SigningKey.generate()
            key.save(s.keys_dir / f"{name}.pem")
            keys[name] = key
            shadows.append(
                ShadowConfig(
                    name=name, port=port, mcp_port=mcp_port, token=token_urlsafe(16)
                )
            )
            lines.append(f"{name}: shadow://key:{key.multibase}@localhost:{port}")
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
        for name, key in keys.items():
            self._issue_credential(issuer, name, key.multibase)
        self._invalidate()
        return lines

    def _issue_credential(self, issuer: SigningKey, name: str, subject: str) -> None:
        now = int(time.time())
        token = Credential.mint(issuer, subject, now)
        store = SqliteCredentialStore(self.settings, name)
        store.add(token, now + 30 * 86400)
        store.close()

    def wipe(self) -> list[str]:
        s = self.settings
        self._invalidate()
        lines: list[str] = []
        for directory in (s.keys_dir, s.hermes_dir):
            if directory.exists():
                shutil.rmtree(directory)
                lines.append(f"deleted {directory}")
        for f in (s.config_file, s.db_file, s.trust_file):
            if f.exists():
                f.unlink()
                lines.append(f"deleted {f}")
        return lines

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
            raise OrchestratorError(f"shadow {name} already exists")

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
            raise OrchestratorError("persona/telegram require a provider")

        shadow_config = ShadowConfig(
            name=name,
            port=max((sh.port for sh in config.shadows), default=7400) + 1,
            mcp_port=max((sh.mcp_port for sh in config.shadows), default=8400) + 1,
            token=token_urlsafe(16),
            persona=persona,
            provider=provider,
            telegram=telegram,
        )
        key = SigningKey.generate()
        key.save(s.keys_dir / f"{name}.pem")
        config.shadows.append(shadow_config)
        save_config(s, config)
        self._issue_credential(SigningKey.load(s.issuer_key_file), name, key.multibase)
        self._invalidate()

        shadow = self.get(name)
        lines = [f"created {shadow.uri}"]
        if provider_cred is not None:
            lines += shadow.agent.generate(provider_cred, template, telegram_cred)
        return shadow, lines


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
                f"{shadow.name}: gateway http://127.0.0.1:{shadow.config.mcp_port}/mcp"
                f"  wire http://127.0.0.1:{shadow.config.port}"
            )
        await orchestrator.wait()

    asyncio.run(run())
