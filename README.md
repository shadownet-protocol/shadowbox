# shadowbox

This project is a simple standalone all-in-one implementation of the Shadownet protocol.
It is intended for experimentation and testing of the protocol, its proposals and future amendments.
No hermes, no sidecar, no server-client, everything in a box.

# TUI

The TUI is a the simple and only interface to the shadowbox.

```sh
uv sync
uv run shadowbox
```

## Giving a shadow a host LLM

When you get started and initialize, it automatically creates two shadows, `alice` and `bob`. To get started: 

1. `c` — open config. `p` adds an API provider (name, kind `anthropic` /
   `openai-api` / `openrouter`, model id, API key); `t` adds a Telegram bot
   (name, BotFather token, allowed chat IDs). `esc` to go back.
2. Highlight a shadow, press `e` — pick the provider (and optionally a persona
   and the bot), confirm.
3. Press `u` to bring it up. The `L` lamp turns green; DM your bot.
