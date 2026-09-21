# Contributing

This guide covers the contributor dev loop (clone → change → PR). For the full
production install (bootable USB, Ubuntu Server, config.toml, bot tokens, NATS
setup, Quadlet auto-start) see **[docs/GETTING-STARTED.md](docs/GETTING-STARTED.md)** —
it is not duplicated here.

## Dev environment setup

```bash
# 1. Clone and bootstrap (deps + git hooks)
git clone https://github.com/Roxabi/roxabi-factory.git
cd roxabi-factory
make dev-setup   # uv + bun + yq + git hooks (stack.yml → commands.dev_setup)

# 2. Configure environment
cp .env.example .env
# Fill in DEPLOY_HOST / DEPLOY_DIR if using make remote or make push (see docs/DEPLOYMENT.md §8)
# Bot tokens are Podman secrets, not .env: seed config.toml then `factory bot init`,
# and `factory bot secret install <platform> <bot_id>` (see docs/GETTING-STARTED.md §8, docs/bot-management.md)

# 3. Run the test suite to verify your setup
uv run pytest
```

> [!NOTE]
> Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required. Install uv with `curl -LsSf https://astral.sh/uv/install.sh | sh`.

## Workflow

All development goes through the `staging` branch. `main` is the stable release branch.

```
feature/fix branch → PR → staging → (promote) → main
```

1. Create a branch from `staging` with a descriptive name: `feat/discord-voice`, `fix/pool-lock-timeout`
2. Open a PR targeting `staging`
3. Pass CI (lint, typecheck, tests)
4. Merge — auto-merge is enabled once a PR carries the `reviewed` label (no PR review count is required on `staging`); see [docs/runbooks/pr-automation.md](docs/runbooks/pr-automation.md) for the full mechanism (label gate, Renovate auto-labelling, merge queue)

## Commit conventions

[Conventional Commits](https://www.conventionalcommits.org/) — enforced by CI:

```
feat(hub): add wildcard binding support
fix(telegram): handle bot message filter edge case
chore: bump aiogram to 3.27
docs(adr): add ADR-008 phase 1 memory scope
test(hub): cover dispatch_response error path
refactor(pool): extract pool_id generation to RoutingKey
```

- Scope is optional but encouraged (`hub`, `telegram`, `discord`, `pool`, `agent`, `memory`)
- Breaking changes: add `!` after scope — `feat(hub)!: remove BindingKey`
- English only for all commits, code, and documentation

## PR conventions

- Title = the commit message that will land on `staging` (Conventional Commits format)
- Link the GitHub issue in the PR body: `Closes #42`
- Keep PRs focused — one logical change per PR
- All tests must pass before merge

## Code style

**Python**

```bash
uv run ruff check .      # lint — must pass
uv run ruff format .     # format — auto-fix
uv run pyright           # type check — must pass
uv run pytest            # tests — must pass
```

**Dashboard / JS-TS** (`apps/`, `packages/`, `brand/` — see `.dev/stack.yml` → `frontend`)

```bash
bun run lint             # biome check — must pass (CI + pre-commit hook)
bun run format           # biome check --write — auto-fix (run if lint-js fails, then re-stage)
bun run --filter @roxabi-factory/dashboard-v2 test   # vitest — pre-push when dashboard changes
```

Dashboard SPA lives in `apps/dashboard-v2/` (shadcn + TanStack).

Git hooks run quality gates locally:

- **commit** — ruff, pyright, biome (`lint-js` when FE paths change), file/folder size, import layers, …
- **pre-push** — dashboard vitest (when FE paths change), trufflehog, ACL drift, deploy integrity (`secrets_drift`, `quadlet_manifest_install`, `volumes_table`, `secrets_source`), debt expiry, architecture snapshot, … (full list: `docs/runbooks/quality-gates.md`)

Install both hook types once:

```bash
make hooks-install   # → bash tools/install-hooks.sh
```

> Do **not** use raw `uv run pre-commit install` — it refuses to install
> whenever `core.hooksPath` is set at any scope ("Cowardly refusing"), which is
> the norm on machines with global git hooks. `tools/install-hooks.sh` writes
> `pre-commit hook-impl` dispatchers into the effective hooks dir instead (and
> re-chains any global hooks); the hooks then fire in the main checkout and
> every linked worktree.

Pre-push hooks require [trufflehog](https://github.com/trufflesecurity/trufflehog/releases) on your `PATH`. Frontend hooks require [bun](https://bun.sh) (see root `package.json` → `packageManager`). Quality gate orchestration requires [yq](https://github.com/mikefarah/yq) — installed by `make dev-setup`.

## Language & layout

| Layer | Language | Location | Role |
|-------|----------|----------|------|
| Product backend | Python | `src/factory/`, `packages/` | Runtime, adapters, contracts |
| Product frontend | JS/TS | `apps/`, `packages/`, `brand/` | Dashboard and shared UI |
| Quality gates | bash (+ Python when parsing) | `tools/` | Gate implementations declared in `stack.yml` |
| Platform orchestration | **bash** | `scripts/`, `tools/dev-setup.sh` | Run gates, CI wrappers, drift checks |
| Domain ops (ACL, deploy) | bash entry → Python | `scripts/` + `tools/` | Scanners declared in `stack.yml` `quality_gates` |

### `scripts/` vs `tools/`

Both are dev tooling — not product code. The split is **who invokes them**:

| | `scripts/` | `tools/` |
|---|------------|----------|
| **What** | Factory platform ops (ACL render/check, `qg` runner, drift guards) | Generic quality gates wired from dev-core |
| **Caller** | `scripts/qg run` (primary); Makefile / `factory-acl` for direct ops | Gate scripts in `tools/` or `scripts/` |
| **New work** | ACL render pipeline, evidence scripts, one-off migrations | Lint/size/import/doc/deploy gates (dev-core pattern) |

**Rules**

- New **quality gate** → implement in `tools/` (or `scripts/` for ACL-specific), declare in `quality_gates` **and** `qg.run_order.<stage>` for each target stage. No pre-commit/CI edit for standard gates.
- ACL **render** pipeline stays in `scripts/` (`render_acl_*.py`); drift wrappers `check-acl-*-drift.sh` in `scripts/`.
- **Orchestration only** (run order, stage filters, `yq` parsing) → bash in `scripts/` (`qg`, `check-*-drift.sh`).

See `scripts/AGENTS.md`, `tools/AGENTS.md`, and `docs/runbooks/quality-gates.md`.

## Adding a channel adapter

A channel adapter normalizes messages from one platform into `InboundMessage` objects and
sends `OutboundMessage` objects back via the platform API. The shape (which base class to
inherit, what to override, how the hub registers the adapter over NATS) is exact and moves
fast — read it from the source rather than from a snapshot here:

- **Base contract** — `src/factory/adapters/shared/_base_outbound.py` (`OutboundAdapterBase`):
  `send_streaming()` is provided; subclasses implement `send()`, `_make_emitter()`,
  `_start_typing()`, `_cancel_typing()`. Shared normalization / render helpers live alongside
  it in `src/factory/adapters/shared/`.
- **A recent adapter to copy** — any platform package under `src/factory/adapters/<platform>/`
  (e.g. `telegram/`, `web/`) shows the full slice: `_normalize()`, platform render methods,
  the `Platform` enum variant in `src/factory/core/messaging/message.py`, hub/adapter
  registration in `src/factory/bootstrap/`, and matching tests under `tests/adapters/`.

## Adding an agent

An agent is a stateless singleton defined by a TOML seed file and stored in the AgentStore (SQLite at `~/.roxabi/factory/config.db` — the config SSoT; `auth.db` holds grants and identity only). Full CLI verbs: `docs/agent-management.md`.

**1. Create a TOML seed** in `src/factory/agents/my_agent.toml`:

```toml
[agent]
name = "my_agent"
memory_namespace = "my_agent"
permissions = []

[model]
backend = "claude-cli"
model = "claude-sonnet-4-6"
max_turns = 10
tools = ["Read", "Grep", "Glob"]

[prompt]
system = """You are ..."""
```

**2. Import into the AgentStore**:

```bash
factory agent init          # imports all TOML seeds into the DB
factory agent list          # verify it appears
```

**3. Wire a bot to the agent** in `config.toml`:

```toml
[[telegram.bots]]
bot_id = "my_bot"
token = "env:MY_BOT_TOKEN"
agent = "my_agent"
```

**4. Assign the bot** (if not auto-assigned at startup):

```bash
factory agent assign my_agent --platform telegram --bot my_bot
```

For a custom agent class (beyond `SimpleAgent`), subclass `AgentBase` from `src/factory/core/agent.py` and implement `process()`.

## Adding a config renderer

A *renderer* is any tool or static file that produces output consumed by an external binary (`nats-server`, `podman`/Quadlet, `systemd`, `openssl`). Any new renderer in `deploy/` or any new CLI that writes a config file must come with a roundtrip test in `tools/check_renderer_roundtrip.sh` + a CI job in `.github/workflows/renderer-roundtrip.yml`.

See **[docs/standards/renderer-roundtrip.md](docs/standards/renderer-roundtrip.md)** for the pattern, the required shape (render → consumer parse → invariant assertions), the worked example (`factory-acl`), and the reviewer checklist. Skipping this leads to bugs accepted at write-time and rejected at reboot — see #1083 and #1089.

## Code review expectations

Reviews focus on correctness, clarity, and architectural consistency — not style (ruff handles that).

**For authors:**
- Keep PRs focused; one logical change per PR makes review fast
- Add context in the PR description for non-obvious decisions
- Respond to review comments within 2 business days

**For reviewers:**
- Use [Conventional Comments](https://conventionalcomments.org/) to signal intent (`suggestion:`, `nit:`, `issue:`)
- Distinguish blocking from non-blocking feedback — prefix optional suggestions with `nit:`
- Approve once all `issue:` comments are resolved; `nit:` items can merge at author's discretion

## Workpack-style task files (multi-hour agent runs)

For any plan with 3 or more tasks, split the plan into a workpack — a directory of small,
self-contained files that an agent can execute sequentially without human input.

### When to use a workpack

| Situation | Use workpack? |
|-----------|---------------|
| 1–2 task plan, ≤30 min estimated | No — keep the single `.mdx` plan file |
| 3+ task plan, any estimated length | Yes |
| Expected run time > 1 hour | Yes |
| Unattended overnight / CI run | Yes |

### Directory layout

```
artifacts/plans/<issue>/
  rules.md        # agent behavior contract: autonomy, error handling, quality, commits
  scope.md        # one-paragraph goal, deliverables, out-of-scope, task table
  task-0001.md    # one logical unit: context + action + files changed + expected outcome + commit
  task-0002.md
  ...
```

The `<issue>` directory name uses the GitHub issue number (e.g., `401`).

### Template

Copy from `artifacts/plans/TEMPLATE/` and fill in the blanks:

```bash
cp -r artifacts/plans/TEMPLATE artifacts/plans/<issue>
```

Then edit each file:

- **`rules.md`** — keep as-is unless the run has special constraints (e.g., read-only filesystem,
  no network access).
- **`scope.md`** — fill in the issue number, goal sentence, deliverables checklist, and task table.
- **`task-NNNN.md`** — one file per task. Delete the template and create numbered files.
  Add more (`task-0002.md`, `task-0003.md`, …) as needed.

### Task file anatomy

Each `task-NNNN.md` must include:

| Section | Purpose |
|---------|---------|
| **Context** | Why this task exists; reference files to read first |
| **Action** | Numbered, imperative steps — no ambiguity |
| **Files changed** | Table of `file → change type` (create / modify / delete) |
| **Expected outcome** | Runnable verify command + expected output |
| **Commit** | Exact commit message for this task |

### Running a workpack

Queue the tasks in Claude Code as sequential prompts (one per task file), or write a driver
script that feeds them one at a time. The agent reads `rules.md` once, then executes each task
file in order without pausing.

```
Read artifacts/plans/<issue>/rules.md and artifacts/plans/<issue>/scope.md.
Then execute artifacts/plans/<issue>/task-0001.md.
```

The first prompt loads the behavior contract and kicks off task-0001 in one go. After each task completes, the next prompt:

```
Execute artifacts/plans/<issue>/task-0002.md.
```

Repeat until all tasks are done. This enables 4–12 hour unattended runs.

## File Size Policy

All Python source files must be ≤ 300 lines. This limit keeps files within a single agent
context window and encourages focused modules.

### Adding to the allowlist

If you need to defer a refactor:

1. Add the file path to `tools/file_exemptions.txt`
2. Include a comment with the line count and a tracking issue number: `# 450 lines — #396`
3. Open a dedicated refactor issue if one doesn't exist
4. Remove the exemption once the file is refactored below 300 lines

The allowlist is not a permanent exemption — every entry must have a tracking issue.

## Folder Size Policy

All folders must contain ≤ 12 Python source files. This limit forces early splits and keeps
folder lists graspable without scrolling.

### Adding to the allowlist

If you need to defer a refactor:

1. Add the folder path to `tools/folder_exemptions.txt`
2. Include a comment with the file count and a tracking issue number: `# 49 files — #753`
3. Open a dedicated refactor issue if one doesn't exist
4. Remove the exemption once the folder is split below 12 files

The allowlist is not a permanent exemption — every entry must have a tracking issue.

## Architecture decisions (ADRs)

Significant architectural choices — especially irreversible ones — are recorded as ADRs in `docs/architecture/adr/`.

When to write an ADR:
- You're choosing between two or more real alternatives
- The decision will be painful to reverse after code is written
- Future contributors need to understand why the current approach was chosen

Use the dev-core skill to create one:

```
/adr "title of the decision"
```

Or copy an existing ADR file and follow the structure: **Status → Context → Options Considered → Decision → Consequences**.

There is no hand-maintained ADR index to update — the consolidation (2026-07-02, ADR-086) dropped
it. Discovery is instead: (1) a **decision-stating title** — the filename slug and frontmatter
`title:` must state what was decided, so a reader with no index never needs to open the file; and
(2) a one-line entry (status + summary) in the relevant domain page's **"ADR archive"** table. Add
a redirect banner from the ADR to its domain page. The living current-state hub is
`docs/ARCHITECTURE.md`; see `docs/architecture/adr/086-documentation-architecture.mdx` for the doctrine.

## Project structure

```
src/factory/
  core/           — hub, pool, agent, message (no external I/O)
  adapters/       — one package per channel (telegram/, discord/, web/, ...)
  agents/         — agent implementations + TOML configs
tests/
  core/           — unit tests for core primitives
  adapters/       — adapter tests (mock external SDKs)
docs/
  architecture/
    adr/          — one .mdx per decision
artifacts/        — dev-core outputs (frames, plans, specs, analyses)
```

The `core/` layer has no imports from `adapters/` or `agents/`. Dependency direction: `adapters → core`, `agents → core`.
