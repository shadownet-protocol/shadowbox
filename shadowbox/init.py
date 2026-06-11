import shutil
import sqlite3

from shadowbox import crypto
from shadowbox.config import (
    Config,
    PersonaConfig,
    Settings,
    load_config,
    save_config,
)

DEFAULT_PERSONAS = [("alice", 7401), ("bob", 7402)]

State = tuple[str, str]


def state(settings: Settings) -> State:
    artifacts = [settings.config_file, settings.db_file, settings.keys_dir]
    if not any(a.exists() for a in artifacts):
        return "fresh", ""
    if not settings.config_file.exists():
        return "broken", f"{settings.config_file.name} missing"
    try:
        config = load_config(settings)
    except Exception as exc:
        return "broken", f"{settings.config_file.name} unreadable: {exc}"
    for p in config.personas:
        if not (settings.keys_dir / f"{p.name}.pem").exists():
            return "broken", f"keys/{p.name}.pem missing"
    if not settings.db_file.exists():
        return "broken", f"{settings.db_file.name} missing"
    return "ok", ""


def plan(settings: Settings) -> list[str]:
    lines = [f"create at {settings.home_dir}:"]
    for name, port in DEFAULT_PERSONAS:
        lines.append(f"  keys/{name}.pem  (new Ed25519 identity, localhost:{port})")
    lines.append(f"  config.yaml  ({len(DEFAULT_PERSONAS)} personas)")
    lines.append("  shadowbox.db")
    return lines


def wipe(settings: Settings) -> list[str]:
    lines: list[str] = []
    if settings.keys_dir.exists():
        shutil.rmtree(settings.keys_dir)
        lines.append(f"deleted {settings.keys_dir}")
    for f in (settings.config_file, settings.db_file):
        if f.exists():
            f.unlink()
            lines.append(f"deleted {f}")
    return lines


def initialize(settings: Settings) -> list[str]:
    lines: list[str] = []
    settings.keys_dir.mkdir(parents=True, exist_ok=True)
    lines.append(f"created {settings.home_dir}")

    personas: list[PersonaConfig] = []
    for name, port in DEFAULT_PERSONAS:
        key = crypto.generate_key()
        crypto.save_key(key, settings.keys_dir / f"{name}.pem")
        personas.append(PersonaConfig(name=name, port=port))
        lines.append(
            f"{name}: shadow://key:{crypto.public_multibase(key)}@localhost:{port}"
        )

    save_config(settings, Config(personas=personas))
    lines.append(f"wrote {settings.config_file}")

    sqlite3.connect(settings.db_file).close()
    lines.append(f"created {settings.db_file}")
    return lines