# Quickstart

Get factory running as containers and send your first message.

factory runs **only as Podman/Quadlet containers** — the same path in dev and in
production. There is no standalone "run it in one process" onboarding mode; the
documented path below is the production path on a single machine.

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.12+ | `python --version` |
| [uv](https://docs.astral.sh/uv/) | latest | `pip install uv` |
| Podman | rootless | `podman --version` — ships natively on Ubuntu 24.04+/Pop!_OS |
| [Claude Code CLI](https://claude.ai/download) | latest | Default LLM backend — requires a Claude subscription |
| Node.js | 22.13+ | Needed by the Claude Code CLI and `bun install` in dev-setup |
| Telegram bot token | — | Create one via [@BotFather](https://t.me/BotFather) |
| Discord bot token | — | Create one via [Discord Developer Portal](https://discord.com/developers) |

> You only need the channels you plan to use. A platform with no
> `[[telegram.bots]]` / `[[discord.bots]]` entry in `config.toml` is silently
> skipped — provision only the bots you want.

## 1. Clone and set up the dev environment

```bash
git clone https://github.com/Roxabi/roxabi-factory
cd roxabi-factory
tools/dev-setup.sh          # uv sync + bun install + git hooks (SSoT: .dev/stack.yml commands.dev_setup)
```

CLI commands below are shown as `uv run factory …`. To drop the `uv run` prefix,
activate the venv (`source .venv/bin/activate`) or add `.venv/bin` to your PATH.

## 2. Create a bot on the platform

**Telegram:**
1. Open Telegram, search `@BotFather`, send `/newbot`, follow the prompts.
2. Copy the token (format `123456789:ABCdef...`).

**Discord:**
1. [discord.com/developers/applications](https://discord.com/developers/applications) → New Application.
2. Bot tab → Reset Token → copy the token.
3. Enable **Message Content Intent** (required to read messages).
4. OAuth2 → URL Generator → scope `bot` + permission `Send Messages` → invite the bot to your server.

## 3. Configure `config.toml`

`config.toml` holds *which bots exist* and their adapter settings — **never
tokens**. Copy the example and edit it:

```bash
cp config.toml.example config.toml
```

Set your admin IDs and one `bot_id` per bot:

```toml
[admin]
# Telegram ID: message @userinfobot   Discord ID: Developer Mode → Copy User ID
user_ids = ["tg:user:123456789", "dc:user:123456789012345678"]

[[telegram.bots]]
bot_id = "lyra"          # must match the id you pass to `factory bot secret install`
# agent = "lyra_default" # fallback if the DB has no bot→agent mapping yet

[[discord.bots]]
bot_id = "lyra"
```

There is **no `token` field** and **no token env file** — tokens are delivered as
Podman secrets in step 5.

## 4. Seed the agent and bot stores

Agents and bots live in SQLite (`~/.roxabi/factory/config.db`). TOML files under
`src/factory/agents/` (defaults) and `~/.roxabi/factory/agents/` (overrides) are
seed sources; `config.toml` is the bot seed source. Import both into the DB:

```bash
uv run factory agent init    # seed AgentStore from bundled agent TOML
uv run factory bot init      # seed BotStore from config.toml [[telegram.bots]]/[[discord.bots]]
```

`factory bot init` is idempotent (skips existing rows; `--force` overwrites). It
is also re-run by `deploy/install.sh` in step 6, so you never hand-edit BotStore.

## 5. Install the bot token as a Podman secret

Tokens are stored as Podman `type=mount` secrets, mounted into the adapter
container at `/run/secrets/bot_token-<bot_id>` — never in `config.toml`:

```bash
uv run factory bot secret install telegram lyra   # prompts for the token (hidden input)
uv run factory bot secret install discord  lyra
```

Non-interactive alternative — read from an env var:

```bash
TELEGRAM_TOKEN=123456789:ABCdef... uv run factory bot secret install telegram lyra --from-env TELEGRAM_TOKEN
```

This creates the Podman secret `factory-bot-telegram-lyra`. `deploy/install.sh`
renders the matching `Secret=` directive into `factory-telegram.container` from
BotStore — see `docs/CONFIGURATION.md` § Bot credentials.

## 6. Generate NATS identities and install the Quadlet units

```bash
make nats-setup        # render nkey seeds + auth.conf under ~/.roxabi/factory/nkeys/
deploy/install.sh      # idempotent: install Quadlet units + Podman secrets, run `factory bot init`, daemon-reload
```

`deploy/install.sh` installs but **does not start** the units (it defers to you).
It prints the exact `systemctl --user start …` line to run next.

## 7. Enable linger and start the containers

```bash
loginctl enable-linger "$USER"   # let systemd --user run without a login session

systemctl --user start \
  factory-nats factory-hub factory-telegram factory-discord \
  factory-clipool factory-gh-helper factory-turn-writer factory-blobstore
```

## 8. Authenticate the Claude CLI

The default agent uses the `claude-cli` backend (spawns the `claude` subprocess).
Authenticate once:

```bash
claude
```

## 9. Verify and send your first message

```bash
systemctl --user status 'factory-*.service'   # units should be active
make factory logs                             # journalctl for factory-hub
```

Send a message to your Telegram bot (DM) or Discord bot (`@YourBot hello!`) — you
should get a reply within a few seconds.

## Troubleshooting

**`no such secret "factory-bot-telegram-lyra"`**
The adapter container failed to start because the token secret is missing on this
host. Podman secrets are host-local — run `factory bot secret install <platform>
<bot_id>` on every host that runs the adapter, then `deploy/install.sh` and
restart the unit.

**Discord bot doesn't respond**
Enable **Message Content Intent** in the Discord Developer Portal → Bot settings.
Without it the bot receives events but cannot read message content.

**Claude CLI errors**
The `claude-cli` backend shells out to the `claude` CLI — run `claude` once to
authenticate (requires a Claude subscription). For multi-provider access
(Ollama, llama.cpp, Fireworks, OpenAI, …) use the `nats` backend → llmCLI worker.

**A bot is silently absent at startup**
Each bot needs a matching `[[auth.telegram_bots]]` / `[[auth.discord_bots]]`
entry in `config.toml`. A bot with no auth entry is skipped — see
[MULTI-BOT.md](MULTI-BOT.md).

## Running multiple bots

To add a second bot (its own persona, model, and auth), see
[MULTI-BOT.md](MULTI-BOT.md) for the full configuration reference and the
step-by-step checklist (`bot init` + `bot secret install` per bot).

## Next steps

- [Architecture](ARCHITECTURE.md) — hub, bindings, pools, and memory model
- [Configuration](CONFIGURATION.md) — config files, load order, and bot credentials
- [Getting Started](GETTING-STARTED.md) — provisioning a dedicated hub host from scratch
- [ADRs](architecture/adr/) — key decisions and their rationale
