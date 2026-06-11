# shadowbox

All-in-one Shadownet lab. Spec source of truth: `../shadownet-specs/rfcs/` — when code and spec disagree, the spec wins or gets amended, never silently diverged from.

## Rules

- No code comments unless absolutely necessary.
- Collapse the deployment, never the protocol: real envelopes, real wire, real RFC 0002 tool surface. No shortcuts that skip a spec boundary.
- All state lives under the configurable project home (`SHADOWBOX_HOME`, default `~/.shadowbox`); never hardcode paths.
- Absolute imports only (`from shadowbox.config import ...`); no relative imports.
- The user writes the implementation; Claude contributes designs, models, config, and docs when asked.
