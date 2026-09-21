# Configuration Reference

factory uses two types of configuration files with distinct responsibilities:

- **System data** — versioned, ships with the code, defines what factory and its agents *are*
- **Instance config** — gitignored, per-machine, defines how THIS deployment runs

---

## Overview

| File | Type | Versioned | Purpose |
|------|------|-----------|---------|
| `config.toml` | Instance config | No | Deployment wiring: bots, tokens, auth, defaults |
| `config.toml` | Instance config | No | Monitoring thresholds — `[monitoring]` section of the same `config.toml`, read by `factory.monitoring` |
| `~/.roxabi/factory/config.db` | Runtime DB | No | Agents, credentials, grants, user prefs (SQLite) |
| `~/.roxabi/factory/turn-writer/turns.db` | Runtime DB | No | Conversation turns, pool sessions |
| `~/.roxabi/factory/discord.db` | Runtime DB | No | Discord thread data (owned by Discord adapter) |
| `~/.roxabi/factory/auth.db` | Runtime DB | No | Auth grants, identity aliases (legacy name, still used) |
| `~/.roxabi/factory/message_index.db` | Runtime DB | No | Message index for search/retrieval |
| `~/.roxabi/factory/agents/<name>.toml` | Seed source | No | Agent seed: imported into DB by `factory agent init` |
| `src/factory/agents/<name>.toml` | Seed source | Yes | Agent seed: system defaults, imported into DB |
| `src/factory/commands/<name>/plugin.toml` | System data | Yes | Plugin manifest: commands, handlers |
| `src/factory/data/messages.toml` | System data | Yes | i18n strings |
| `pyproject.toml` | System data | Yes | Package metadata, dependencies, tool config |

**Rule:** if a value is machine-specific, personal, or secret → `config.toml`. Everything else → versioned.

**Agent rule:** TOML files define what agents *should be*. The DB holds what they *are* at runtime. Startup reads from the DB only — TOML changes require `factory agent init` (or `--force`) to take effect.

---

## Config File Resolution

### `config.toml` — Hub and adapters

Resolution order (first match wins):

```
1. $FACTORY_CONFIG           (if set, must be under $HOME)
2. $ROXABI_FACTORY_DIR/config.toml
3. ./config.toml          (cwd)
4. Empty dict (defaults)
```

The path is validated to be under `$HOME` when set via `FACTORY_CONFIG`.

### config.toml — Monitoring (`[monitoring]` section)

Resolution order:

```
1. $FACTORY_CONFIG           (if set, must be under $HOME)
2. ./config.toml           (cwd)
3. Empty dict (defaults)
```

**Note:** Hub and monitoring both read `config.toml` (monitoring reads the `[monitoring]` section). If you set `$FACTORY_CONFIG`, it must contain `[monitoring]` plus any other sections you need.

### `messages.toml` — i18n strings

Resolution order:

```
1. $FACTORY_MESSAGES_CONFIG  (if set, must end in .toml and be under $HOME)
2. ./messages.toml        (cwd)
3. src/factory/data/messages.toml  (bundled)
```

### Store directory (`~/.roxabi/factory/`)

Controlled by `ROXABI_FACTORY_DIR`:

```
$ROXABI_FACTORY_DIR  (if set)
~/.roxabi/factory          (default)
```

Databases created under this directory:

| DB File | Tables |
|---------|--------|
| `config.db` | `agents`, `bot_agent_map`, `agent_runtime_state`, `bot_secrets`, `user_prefs` |
| `auth.db` | Auth grants, identity aliases |
| `turn-writer/turns.db` | Conversation turns, pool sessions (WAL siblings co-locate with the `factory-turn-writer` unit — `paths.py`) |
| `discord.db` | `discord_threads` (owned by Discord adapter) |
| `message_index.db` | Message index |

---

## `config.toml` Sections

### `[defaults]` — Machine-wide fallbacks

```toml
[defaults]
cwd = "~/projects"              # default working directory for agent subprocesses
persona = "lyra_default"        # fallback persona if agent doesn't specify one
workspaces.lyra = "~/projects/roxabi-factory"    # adds /lyra slash command
workspaces.projects = "~/projects"     # adds /projects slash command
```

Resolution order for agent overrides:

```
agents/<name>.toml value      (agent-specific, highest priority)
    ↓ fallback
config.toml [defaults]        (machine-wide default)
    ↓ fallback
hardcoded default              (cwd = "~", persona = none)
```

### `[agents.<name>]` — Per-agent overrides

```toml
[agents.lyra_default]
cwd = "~/projects/roxabi-factory"
persona = "dev-assistant"
workspaces.lyra = "~/projects/roxabi-factory"
```

Merged with `[defaults]` — agent-specific values win. Workspaces are deep-merged.

### `[admin]` — Admin users

```toml
[admin]
user_ids = [
    "tg:user:123456789",
    "dc:user:123456789012345678",
]
```

Format: `"tg:user:<numeric_id>"` or `"dc:user:<numeric_snowflake>"`.

### `[[telegram.bots]]` — Telegram bot instances

```toml
[[telegram.bots]]
bot_id = "lyra"
agent = "lyra_default"         # fallback if DB has no bot→agent mapping
```

Credentials (token, webhook_secret) are read from Podman secrets at bootstrap — see `## Bot credentials`.

### `[[discord.bots]]` — Discord bot instances

```toml
[[discord.bots]]
bot_id = "lyra"
auto_thread = true             # create thread per conversation (default: false; opt-in via BotStore)
agent = "lyra_default"         # fallback if DB has no bot→agent mapping
thread_hot_hours = 36          # hours before thread is considered cold (default: 36)
```

### `[[auth.telegram_bots]]` / `[[auth.discord_bots]]` — Auth rules

```toml
[[auth.telegram_bots]]
bot_id = "lyra"
default = "blocked"            # "blocked" | "trusted" | "owner"
owner_users = [123456789]      # numeric Telegram IDs — seeded into DB
trusted_users = [987654321]    # can interact, cannot admin
# Optional: webhook_enabled — bool, default false. When true, the
#   render_quadlet pipeline emits an additional Secret=…bot_webhook-<bot_id>
#   mount for the Telegram webhook secret verification path.
webhook_enabled = false

[[auth.discord_bots]]
bot_id = "lyra"
default = "blocked"
owner_users = [123456789012345678]
trusted_roles = [111222333444555666]  # numeric Discord role snowflakes
```

### `[hub]` — Hub configuration

```toml
[hub]
pool_ttl = 604800.0            # pool time-to-live in seconds (default: 7 days)
rate_limit = 20                # max messages per user per window (default: 20)
rate_window = 60               # rate limit window in seconds (default: 60)
```

### `[pool]` — Pool configuration

```toml
[pool]
safe_dispatch_timeout = 10.0   # timeout for safe dispatch operations (default: 10s)
```

### `[cli_pool]` — Claude CLI pool configuration

```toml
[cli_pool]
idle_ttl = 1200                # idle process TTL in seconds (default: 20 min)
default_timeout = 1200         # default turn timeout (default: 20 min)
turn_timeout = null            # optional turn timeout override
reaper_interval = 60           # reaper check interval (default: 60s)
kill_timeout = 5.0             # process kill timeout (default: 5s)
read_buffer_bytes = 1048576    # read buffer size (default: 1 MiB)
stdin_drain_timeout = 10.0     # stdin drain timeout (default: 10s)
max_idle_retries = 3           # max idle process retries (default: 3)
intermediate_timeout = 5.0     # intermediate response timeout (default: 5s)
```

### `[inbound_bus]` — Inbound message bus

```toml
[inbound_bus]
queue_depth_threshold = 100    # alert threshold for queue depth (default: 100)
staging_maxsize = 500          # staging queue max size (default: 500)
platform_queue_maxsize = 100   # per-platform queue max size (default: 100)
```

### `[debouncer]` — Message debouncing

```toml
[debouncer]
default_debounce_ms = 300      # debounce window (default: 300ms)
max_merged_chars = 4096        # max chars in merged message (default: 4096)
cancel_on_new_message = false  # cancel ongoing turn on new message (default: false)
```

### `[event_bus]` — Pipeline event bus

```toml
[event_bus]
queue_maxsize = 1000           # event queue max size (default: 1000)
```

### `[llm]` — LLM driver configuration

```toml
[llm]
max_retries = 3                # max retries on failure (default: 3)
backoff_base = 1.0             # exponential backoff base (default: 1.0)
```

### `[logging]` — Structured log output

```toml
[logging]
level = "info"                 # log level (default: "info")
```

### `[message_index]` — Message retention

```toml
[message_index]
retention_days = 90            # days to retain indexed messages (default: 90)
```

### `[pairing]` — Device pairing

```toml
[pairing]
enabled = false                # enable pairing system (default: false)
alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # safe alphabet for codes
code_length = 8                # pairing code length (default: 8)
ttl_seconds = 3600             # code validity in seconds (default: 1 hour)
max_pending = 3                # max pending codes per user (default: 3)
session_max_age_days = 30      # session max age (default: 30 days)
rate_limit_attempts = 5        # rate limit attempts (default: 5)
rate_limit_window = 300        # rate limit window in seconds (default: 5 min)
```

### `[tool_display]` — Tool call display during streaming

```toml
[tool_display]
names_threshold = 3            # edits to show per file before collapsing (default: 3)
group_threshold = 3            # files before grouped summary (default: 3)
bash_max_len = 60              # max bash command chars (default: 60)
throttle_ms = 2000             # min ms between tool-summary updates (default: 2000)

[tool_display.show]
edit = true                    # show edit tool calls (default: true)
write = true                   # show write tool calls (default: true)
bash = true                    # show bash tool calls (default: true)
web_fetch = true               # show web_fetch tool calls (default: true)
web_search = true              # show web_search tool calls (default: true)
agent = true                   # show agent tool calls (default: true)
read = false                   # silent by default (high-frequency, low signal)
grep = false                   # silent by default
glob = false                   # silent by default
```

### `[circuit_breaker.<service>]` — Circuit breaker per service

```toml
[circuit_breaker.claude-cli]
failure_threshold = 5          # failures before opening (default: 5)
recovery_timeout = 60          # seconds before retry (default: 60)

[circuit_breaker.telegram]
failure_threshold = 5
recovery_timeout = 60

[circuit_breaker.discord]
failure_threshold = 5
recovery_timeout = 60

[circuit_breaker.hub]
failure_threshold = 5
recovery_timeout = 60
```

Services: `claude-cli`, `telegram`, `discord`, `hub`.

---

## Bot credentials

Bot tokens and webhook secrets are stored as **Podman secrets**, not in `~/.roxabi/factory/config.db`. Adapter containers mount these via `Secret=` directives in `deploy/quadlet/factory-<platform>.container`; the adapter process reads each token at bootstrap from `/run/secrets/bot_token-<bot_id>` (and optionally `/run/secrets/bot_webhook-<bot_id>`).

### CLI

| Command | Purpose |
|---|---|
| `factory bot secret install <platform> <bot_id> [--from-env TOK] [--webhook-from-env WHK]` | Create or replace a bot's token (and optional webhook secret) |
| `factory bot secret rm <platform> <bot_id>` | Remove the bot's token + webhook secret |
| `factory bot secret list` | List provisioned bot secrets (filtered by `factory-bot-` prefix) |
| *(not a subcommand)* `python3 tools/migrate_bot_secrets_to_podman.py` | One-shot operator script — migrates pre-#1057 `bot_secrets` rows from `config.db` to Podman secrets; run once on M₁ then discard. See [runbooks/bot-secrets-migration.md](runbooks/bot-secrets-migration.md) |

`<bot_id>` must match `^[A-Za-z0-9_-]+$` (alphanumeric, hyphen, underscore — slash-free for safe Podman secret names and tmpfs mount targets).

### Quadlet wiring

Each bot expects one `Secret=` line per credential in the appropriate `.container` file:

```
Secret=factory-bot-telegram-<bot_id>,type=mount,target=bot_token-<bot_id>,mode=0400,uid=1500,gid=1500
Secret=factory-bot-telegram-<bot_id>-webhook,type=mount,target=bot_webhook-<bot_id>,mode=0400,uid=1500,gid=1500
```

(Omit the webhook line if the bot does not use webhooks.)

Re-render the Quadlet after any secret provisioning change:

```bash
make quadlet-install
# Renders per-bot Secret= directives from ~/.roxabi/factory/config.toml into
# factory-telegram.container and factory-discord.container, then reloads units.
```

After running `make quadlet-install`, restart the adapter to remount: `make telegram-adapter restart` (or `discord-adapter`). `type=mount` secrets are tmpfs binds; `podman secret create --replace` updates the store but the in-container file is stale until container restart.

### Migrating from pre-#1057 `bot_secrets` rows

→ Moved to the one-shot operator runbook: [runbooks/bot-secrets-migration.md](runbooks/bot-secrets-migration.md).

### Rationale

webhook_secret packing: separate secret (not packed into JSON). This matches the project's raw-bytes single-purpose convention (every other Podman secret in `Makefile:181-200`), keeps the failure-loud bootstrap path free of a JSON parser, and supports independent rotation of token vs. webhook.

### Production guard

`FACTORY_RUN_SECRETS_DIR` lets tests and local development point at a temporary directory instead of `/run/secrets`. In production this override is **ignored** as a defense-in-depth measure: an attacker with env-write access on a prod host cannot redirect token reads to a path they control.

The guard activates when **either** condition is true:

| Condition | Detection |
|---|---|
| Inside a container | `/run/.containerenv` exists (Podman runtime marker) |
| Explicit prod mode | `FACTORY_ENV=prod` |

When active, `load_bot_token` logs a warning and falls back to `/run/secrets` regardless of the env variable. Operators should **never** set `FACTORY_RUN_SECRETS_DIR=` in Quadlet `.container` files — the override is intended for local dev and CI only.

### Backup

Podman secrets are the authoritative copy of bot tokens. There is no automatic backup; operators must snapshot them explicitly.

**Snapshot all bot secrets:**

```bash
podman secret ls --filter name=factory-bot- --format '{{.Name}}' | \
  xargs -n1 --no-run-if-empty podman secret inspect --showsecret | \
  jq -s '[.[] | {name: .[0].Spec.Name, data: .[0].SecretData}]' \
  > factory-bot-secrets-$(date +%Y%m%d).json
```

**Recommended cadence:** snapshot after every token rotation or bot provisioning change. Store the JSON file in your usual infrastructure backup location (e.g. alongside `~/.roxabi/factory/config.db` backups, or in your password-manager/secret-manager's export path).

**Restore a secret from snapshot:**

```bash
# Re-create a single secret from the snapshot file
cat factory-bot-secrets-YYYYMMDD.json | \
  jq -r '.[] | select(.name == "factory-bot-telegram-mybot") | .data' | \
  podman secret create factory-bot-telegram-mybot -
```

After restoring, run `make quadlet-install` to re-render the Quadlet and restart the affected adapter. Per-bot Secret= directives are rendered at install time; no fragment files need separate backup. Source of truth is `~/.roxabi/factory/config.toml`.

**Note:** The snapshot contains raw secret data. Encryption-at-rest for the snapshot file is out of scope for this document; handle it according to your organization's secret-management policy.

---

## config.toml — Monitoring Only

Read exclusively by `factory.monitoring`. Hub does NOT read this file.

### `[monitoring]` — Thresholds

```toml
[monitoring]
check_interval_minutes = 5                    # timer interval (default: 5)
health_endpoint_timeout_s = 5                 # HTTP timeout (default: 5)
queue_depth_threshold = 80                    # alert threshold (default: 80)
idle_threshold_hours = 6                      # idle alert threshold (default: 6)
quiet_start = "00:00"                         # quiet period start (default: "00:00")
quiet_end = "08:00"                           # quiet period end (default: "08:00")
idle_check_enabled = false                    # enable idle checks (default: false)
min_disk_free_gb = 1                          # disk alert threshold (default: 1)
health_endpoint_url = "http://localhost:8443/health/detail"
diagnostic_model = "claude-haiku-4-5-20251001"
disk_check_path = "/"                         # filesystem to check (default: "/")
service_names = ["factory-hub", "factory-telegram", "factory-discord"]
health_secret = ""                            # optional health endpoint auth
```

---

## Environment Variables

### Core paths

| Variable | Default | Description |
|----------|---------|-------------|
| `FACTORY_CONFIG` | — | Path to `config.toml` (hub) or config.toml (monitoring) |
| `ROXABI_FACTORY_DIR` | `~/.roxabi/factory` | Store directory for all databases (`factory_data_dir()` in `paths.py`) |
| `FACTORY_MESSAGES_CONFIG` | bundled | Path to custom `messages.toml` |
| `FACTORY_DB` | — | Agent-store implementation selector (not a path): `json` → `JsonAgentStore` (test); unset → SQLite `AgentStore`. See `agent_store_factory.py`. |

### Telegram

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_TOKEN` | Yes (factory-log-monitor) | Bot token — live credential path for the `factory-log-monitor` Quadlet unit (`deploy/quadlet/factory-log-monitor.container`, #2245); multi-bot production adapters use Podman secrets instead — see `## Bot credentials` |
| `TELEGRAM_WEBHOOK_SECRET` | Yes (hub) | Webhook secret |
| `TELEGRAM_ADMIN_CHAT_ID` | Yes (factory-log-monitor) | Chat ID for alerts — live credential path for the `factory-log-monitor` Quadlet unit (`deploy/quadlet/factory-log-monitor.container`, #2245) |
| `TELEGRAM_BOT_USERNAME` | No | Bot username for help text |

### Discord

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | Yes (hub) | Bot token |
| `DISCORD_AUTO_THREAD` | No | `"true"/"1"/"yes"/"on"` to enable auto-thread (default: true) |

### NATS

| Variable | Default | Description |
|----------|---------|-------------|
| `NATS_URL` | `nats://localhost:4222` | NATS server URL (required for standalone hub) |

### BlobStore

Read by `init_blobstore()` in each adapter process (Telegram, Discord) + unified at startup.
Token is read **once** at startup (restart-not-HUP — the value is never re-read without a
process restart). If `FACTORY_BLOBSTORE_TOKEN_PATH` points to an absent file, `blob_store`
degrades to `None`: audio attachments are disabled and a warning is logged; no crash occurs.

| Variable | Default | Description |
|----------|---------|-------------|
| `FACTORY_BLOBSTORE_URL` | `http://localhost:8449` | BlobStore service base URL |
| `FACTORY_BLOBSTORE_TOKEN_PATH` | `~/.roxabi/factory/blobstore.tok` | Path to bearer-token file; read once at startup |

#### BlobStore env file

`~/.roxabi/factory/env/blobstore.env` and `~/.roxabi/factory/env/web.env` are Quadlet env files
consumed at container start via `EnvironmentFile=` by `factory-blobstore.container` and
`factory-dashboard.container` respectively. They are NOT loaded by the factory application itself — each
carries `TAILSCALE_IPV4` for the unit's Tailnet-bound publish port (see `deploy/AGENTS.md
§Network exposure tiers`).

| File | Versioned | Purpose |
|------|-----------|---------|
| `~/.roxabi/factory/env/blobstore.env` (on M₁) | No (operator copy) | Live env file read by `factory-blobstore` at startup |
| `~/.roxabi/factory/env/web.env` (on M₁) | No (operator copy) | Live env file read by `factory-dashboard` at startup |

**Bootstrap:** `deploy/install.sh` §1c generates this file idempotently — it skips creation
if the file already exists, and regenerates it with `--force`.

Variables written by install.sh:

| Variable | Source | Notes |
|----------|--------|-------|
| `TAILSCALE_IPV4` | `tailscale ip -4 \| head -1` at bootstrap | Empty string if Tailscale is absent at install time — the unit's ExecStartPre guard rejects start when unset (fail-closed; see `deploy/AGENTS.md §Known residual risk`) |
| `NATS_URL` | Omitted from the file | Supplied exclusively by the unit's inline `Environment=NATS_URL=nats://factory-nats:4222`; omitting it from the env file prevents an empty value in systemd scope from shadowing the inline directive |

File permissions: `0600` (set atomically via `umask 0077` subshell in install.sh).

**Recovery:** delete the file and re-run install.sh to regenerate.

```bash
rm ~/.roxabi/factory/env/blobstore.env
./deploy/install.sh --force
```

Load order: N/A — this is a Quadlet env file, not an application config file. The bearer
token and blob data path are delivered via `Secret=` and `Volume=` directives in
`deploy/quadlet/factory-blobstore.container` (not via env vars).

#### JetStream persistent storage

JetStream is enabled via the config file stanza in `deploy/nats/nats-container.conf` (the `-js` CLI flag was removed in #1055). Storage is backed by a Quadlet bind-mount volume:

| Unit | Host path | Container path |
|------|-----------|----------------|
| `factory-jetstream.volume` | `~/.roxabi/factory/nats/jetstream` | `/var/lib/nats/jetstream` |

**First-time setup (production, uid 1500):**

```bash
make quadlet-install                          # creates ~/.roxabi/factory/nats/jetstream at mode 0700
podman unshare chown 1500:1500 ~/.roxabi/factory/nats/jetstream
make quadlet-secrets-install                  # skip if secrets already installed
systemctl --user restart factory-nats
```

**Dev (no fixed uid mapping):** `make quadlet-install` is sufficient — omit the `podman unshare chown` step.

**Upgrade:** after `make quadlet-install`, run `systemctl --user daemon-reload && systemctl --user restart factory-nats` to pick up unit file changes.

**Lint:** before deploying, validate all Quadlet unit files locally:

```bash
make quadlet-lint
```

Runs `podman quadlet --dryrun` (parse errors) and a comment-guard that rejects inline `#` comments on value lines — Quadlet does not strip them and Podman receives the literal text as a mount-option string (incident 2026-05-06, issue #1083). CI enforces the same check on every PR that touches `deploy/quadlet/`.

### Voice (STT/TTS)

| Variable | Default | Description |
|----------|---------|-------------|
| `FACTORY_STT_MODEL` | `large-v3-turbo` | Whisper model size |
| `FACTORY_STT_TIMEOUT` | `15` | STT timeout in seconds |
| `FACTORY_TTS_ENGINE` | — | TTS engine (per-adapter in voiceCLI container) |
| `FACTORY_TTS_VOICE` | — | TTS voice ID |
| `FACTORY_TTS_LANGUAGE` | — | TTS language code |
| `FACTORY_TTS_TIMEOUT` | — | TTS timeout in seconds |
| `FACTORY_AUDIO_TMP` | — | Audio temp directory |
| `FACTORY_MAX_AUDIO_BYTES` | — | Max audio file size |

### Health endpoint

| Variable | Default | Description |
|----------|---------|-------------|
| `FACTORY_HEALTH_HOST` | — | Health endpoint host |
| `FACTORY_HEALTH_PORT` | — | Health endpoint port |
| `FACTORY_HEALTH_SECRET` | — | Health endpoint auth secret |

### Misc

| Variable | Default | Description |
|----------|---------|-------------|
| `FACTORY_AGENT_STORE_PATH` | — | Override agent store path |
| `FACTORY_CLAUDE_CWD` | — | Claude CLI working directory |
| `FACTORY_WEB_INTEL_PATH` | — | Web intel output path |
| `FACTORY_WEB_HOST` | `0.0.0.0` | Dashboard HTTP bind host (phase 1 name) |
| `FACTORY_WEB_PORT` | `8765` | Dashboard HTTP port |
| `FACTORY_DASHBOARD_E2E` | — | When `1`, dashboard BFF uses stub agents/sessions (Playwright/CI) |
| `FACTORY_DASHBOARD_AUTH_REQUIRED` | — | **Removed (ADR-103 Block 14)** — control-plane session/API-key is the auth path |
| `FACTORY_SMOKE_MODE` | — | ADR-094 phase 3: smoke platform identity in CI (not prod Quadlet) |

Phase 2 aliases (documented, not required yet): `FACTORY_DASHBOARD_HOST` / `FACTORY_DASHBOARD_PORT`
mirror `FACTORY_WEB_*`.

---

## Runtime databases — `~/.roxabi/factory/`

| Database | Contents |
|----------|----------|
| `config.db` | Agents, bot-agent map, agent runtime state, credentials, user prefs |
| `turn-writer/turns.db` | Conversation turns, pool sessions (co-located with the `factory-turn-writer` unit — `paths.py`) |
| `discord.db` | Discord thread data (owned by Discord adapter) |
| `auth.db` | Auth grants, identity aliases |
| `message_index.db` | Message index for search/retrieval |

**Migration:** On first startup after upgrading from pre-v15, factory automatically migrates existing rows from `auth.db` to `config.db`, `turns.db`, and `discord.db`. Old `auth.db` is kept as tombstone.

---

## Agent definitions — SQLite DB + TOML seeds

Agents are stored in **`~/.roxabi/factory/config.db`** (SQLite). This is the runtime source of truth.

TOML files are **seed sources** — imported via `factory agent init`. After import, TOML edits have no effect until re-imported.

### CLI workflow

```bash
factory agent init              # seed DB from TOML files
factory agent init --force      # force re-import (overwrites DB rows)
factory agent list              # list all agents in DB
factory agent show <name>       # show full config for one agent
factory agent edit <name>       # edit an agent in DB interactively
factory agent validate <name>   # schema + constraint checks
factory agent delete <name>     # delete an agent (refuses if bot assigned)
factory agent assign <name> --platform telegram --bot <bot_id>
factory agent unassign --platform telegram --bot <bot_id>
```

### TOML format (seed files)

```toml
[agent]
name = "lyra_default"
memory_namespace = "lyra"
permissions = []
persona = "lyra_default"          # loads system prompt from agent soul / config
show_intermediate = false

[model]
backend = "claude-cli"            # "claude-cli" | "nats" (future)
model = "claude-sonnet-4-6"
max_turns = 10
tools = ["Read", "Grep", "Glob", "WebFetch", "WebSearch"]

[plugins]
enabled = ["echo"]
```

**What belongs here:** model, tools allowlist, plugin list, persona name, memory namespace.

**What does NOT belong here:** `cwd`, `workspaces` — machine-specific, live in `config.toml [defaults]`.

---

## Load order summary

```
startup
  ├── _load_raw_config() → config.toml
  │     ├── $FACTORY_CONFIG (validated under $HOME)
  │     ├── $ROXABI_FACTORY_DIR/config.toml
  │     ├── cwd/config.toml
  │     └── {} (empty → all defaults)
  │
  ├── _load_circuit_config() → [circuit_breaker.*] + [admin]
  │
  ├── load_multibot_config() → [[telegram.bots]] + [[discord.bots]]
  │     └── backward compat: [auth.telegram]/[auth.discord] → synthesize bot_id="main"
  │
  ├── _load_*_config() → individual section models
  │     ├── [hub], [pool], [cli_pool], [llm]
  │     ├── [inbound_bus], [debouncer], [event_bus]
  │     ├── [logging], [message_index], [pairing]
  │     └── [tool_display]
  │
  └── AgentStore.connect() → ~/.roxabi/factory/config.db
        └── for each bot → resolve agent from DB
              ├── bot_agent_map row (highest priority)
              └── if missing → config.toml bot.agent → auto-seed
```

## `make quadlet-install` — deploy-time verification

→ Moved to the operator runbook: [runbooks/quadlet-install.md § Deploy-time verification](runbooks/quadlet-install.md#deploy-time-verification) (what `make quadlet-install` verifies, and the `NO_RESTART=1` escape hatch).

---

## Monitoring — mostly dormant; two modules live via `factory-log-monitor` (#2245)

The host-timer units (`lyra-monitor.{service,timer}`) have been removed from `deploy/`. Most of the Python module `src/factory/monitoring/` (`checks.py`'s aggregate Layer-1 runner, `check_process`/`check_http_health`, `__main__.py`) is retained for [Monitoring v2 (#1035)](https://github.com/Roxabi/roxabi-factory/issues/1035) spec mining — it pokes `systemctl --user`, `podman logs`, and host loopback ports, none of which translate cleanly to a containerised world, and offers no UI beyond a Telegram message. This dormant path still emits a `DeprecationWarning` on import (`checks.py`, suppressed suite-wide in `pyproject.toml`'s `filterwarnings`).

`checks_log.py`, `log_watch.py`, and `escalation.py`'s raw-alert path are the exception: as of #2245 they are live production code, shipped and run continuously by the standalone `factory-log-monitor` Quadlet container (`deploy/quadlet/factory-log-monitor.container`) as a thin, V1-pull safety net (ADR-091 plane③ — pull, not subscribe; see `docs/architecture/observability.md`). They never emit the deprecation warning and should be retired/subsumed once Monitoring v2 (#1035) ships log-scanning as a hub-native consumer.

For ad-hoc hub-health probes, hit `/health/detail` directly:

```bash
curl -fsS -H "Authorization: Bearer $FACTORY_HEALTH_SECRET" \
  http://127.0.0.1:8443/health/detail | jq .
```

---

## Voice (optional)

Enable when running `voicecli_tts` / `voicecli_stt` via voiceCLI:

```bash
FACTORY_STT_MODEL=large-v3-turbo   # faster-whisper model
```

Hub probes STT/TTS adapters at startup via NATS heartbeats. Workers are discovered dynamically — no explicit enable flag needed.

---

## WorkerError error codes

All valid error codes and their `domain`, `retryable`, and `description` fields are documented in `packages/roxabi-contracts/docs/error-codes.md`. The Markdown is **auto-generated** from `KNOWN_CODES` in `packages/roxabi-contracts/src/roxabi_contracts/errors.py` via the `codes-sync` pre-commit hook (`scripts/check_codes_sync.py --write`). Edits to `errors.py` regenerate the doc table automatically on commit; CI verifies the two stay in lockstep.

See also: ADR-066 (`docs/architecture/adr/066-unified-worker-error-envelope-nats-reply-contracts.mdx`).
