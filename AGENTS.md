# AGENTS.md — factory (roxabi-factory)

Let:
  A := ~/.roxabi/factory/auth.db (grants, identity only) | C := ~/.roxabi/factory/config.db (agents, bots, prefs) | T := TOML seed

## Project

**factory** — AI factory engine (hub-spoke, asyncio, multi-channel)
→ `docs/ARCHITECTURE.md`

## TL;DR

- **Core rule first** (before any cross-part or non-trivial edit): state the axis + part touched. See below.
- Entry: `/dev #N` → tier (S/F-lite/F-full) → lifecycle
- Close checklist (pre-cleanup, from worktree): `docs/process/dev-cycle.md`
- ¬`--force` | ¬`--hard` | ¬`--amend`

## Core (highest importance — non-negotiable)

**PRIMARY AXIS**: stage/pipeline slice, **not** platform/adapter → `docs/architecture/job-model.md` (axis primacy).

- New concern → 1 stage module + compose primitives.
- New adapter → thin config only. **Never** duplicate parse/sanitize/route/emit per platform.
- Signals of drift: `class.*Client.*Client`, parallel logic across adapters/, leaks between inbound/streaming/outbound.

**Enforcement**: `.importlinter` (grep "stage-axis" or "stage-purity|per-part-stage-helpers-isolation|no-direct-bot-store|shared-modules-upper-boundary|shared-modules-independence|core-ports-purity|composition-root-isolation|application-no-direct-infra" contracts). Fails qg/CI.
See `docs/architecture/engineering-standards.md` § Key invariants for how these generalized contracts cover ADR-059 (Clean/Hex + CR + ports) and ADR-073 (stage axis + HELPERS per part).
AVOIDED PATCHWORK: prior low-consensus specifics (incl. commands-no-infra + agents-no-bootstrap) were generalized into broader contracts (stage-purity, per-part-stage-helpers-isolation, application-no-direct-infra, etc.). See .importlinter comments.

**HELPERS per part**: focused extract inside the part (inbound/, streaming/, etc.). Compose, respect boundaries. No god modules or cross-duplication.

**For any change touching >1 part or adding logic**:
- State: "stage X / per-part helper Y" (or "thin adapter").
- Run qg.
- Axial label on layer cross.

Full model = query `.importlinter` (contracts) + `ccc` / grep "stage-axis" / `scripts/qg`. No preloading docs.

## Axial Review (mandatory)

PRs that cross architectural layer boundaries MUST carry `dev-core:axial-adr-review` before merge.
`.github/workflows/axial-review.yml` applies labels automatically; do NOT strip them manually.

| Trigger | Labels applied |
|---------|---------------|
| PR touches top-level `inbound/` **and** top-level `adapters/` | `dev-core:axial-adr-review` |
| PR touches top-level `core/` **and** top-level `infrastructure/` | `dev-core:axial-adr-review` |
| PR adds `except Exception` (any binding form: `as e`, `as exc`, bare) | `dev-core:axial-adr-review` + `dev-core:security-auditor` |

Note: `core/ports/` and `core/hub/` are both sub-packages of `core/` — intra-core changes touching both do NOT trigger the label. Only changes that cross the top-level `factory.*` module boundary trigger `dev-core:axial-adr-review`.

Review checklist (applies when `dev-core:axial-adr-review` is present):
- Primary axis: respects stage (composition, no per-adapter duplication). Agent must have stated "stage X / thin config".
- Helpers per part extracted (no god modules).
- Layer boundaries clean. `except Exception` justified.
- qg clean.

## Design

UI/brand work: use roxabi-ui design-doctrine as source. State tokens/axis used. Run critique/audit. Avoid muddy defaults. See `roxabi-ui` skill.

## Key files (query via tools, do not preload)

| File | Role |
|---|---|
| `docs/ARCHITECTURE.md` | Routing hub — one hop to domain pages (L1) and runbooks (L2); ¬inventory |
| `.importlinter` | Executable model (stage-axis contracts live here) |
| `docs/architecture/engineering-standards.md` | Invariants (layers, ports, kernel) |
| `docs/architecture/CURRENT.generated.md` + domain pages | Current truth |
| roxabi-ui design-doctrine | Design rules |

## Agent management

Agents ∈ C (SQLite) | T files = seed only → `factory agent init` before use
Search: `~/.roxabi/factory/agents/` (override) → `src/factory/agents/` (default)
`cwd` → `config.toml [defaults]` (¬T)

→ `docs/agent-management.md` — CLI verbs: `init | list | show | edit | patch | validate | create | delete | assign | unassign | refine`

## Agent instructions hygiene

Content lives in `AGENTS.md` — one per subsystem, read by every harness (Claude, Cursor, Grok).

File/rename → update `AGENTS.md` + registry immediately.

→ `docs/claude-md-registry.md` — full path→scope table (the SSoT; one row per `AGENTS.md`). Update there on add/rename/delete.

Rules: add/delete/move → update `AGENTS.md` + register | new subdir with non-obvious invariants → add both + register | "invariants, not inventory" (¬file counts, ¬method dumps — let `ls`/`grep` answer that) | ¬`ADR-NNN` as operational rule — point to domain page (L1); gated by `agents_no_adr_refs`

## Production entry points

Each prod NATS process is a `factory <subcommand>` whose composition root is a
`_bootstrap_*_standalone()` in `src/factory/bootstrap/standalone/`. The authoritative
set of deployed processes is the enabled `[component.*]` sections in
`deploy/quadlet.toml`; enumerate the CLI surface with `factory --help` or
`git grep -nE '@(adapter_app|hub_app)\.command|add_typer' src/factory/cli/main.py`
(hub, `adapter {telegram,discord,web,clipool,omp}`, `turn-writer`, `ingress serve`,
`blobstore serve` — invariants, not a hand-maintained count).

Topics: `factory.inbound.<platform>.<bot_id>` | `factory.outbound.<platform>.<bot_id>`

Unified: `factory start` → hub + adapters in 1 process + embedded NATS

## Container deployment

Prod: Podman Quadlet (systemd `--user`) on M₁ (`factory-hub` role). The deployed
container set is the enabled `[component.*]` sections in `deploy/quadlet.toml` (SSoT;
Langfuse + `factory-otel-collector` ship disabled) — see also
`docs/architecture/CURRENT.generated.md § topology`. Install: `deploy/install.sh`
(idempotent).

Plus the llmCLI cloud-gateway units vendored from Roxabi/llmCLI (LiteLLM proxy
:18091 + xAI/Grok forwarder :18645 + Fireworks forwarder :18646 — ports are the
contract) — deployment owned here (M₁ always-on cloud LLM gateway), image built +
published by llmCLI CI and pinned by digest. Local GPU inference (llmcli-nats-worker,
M₂) stays in Roxabi/llmCLI. See `deploy/AGENTS.md` § llmCLI cloud gateway.

→ `docs/runbooks/README.md` — ops runbooks (install, secrets, diagnostic)
→ `~/projects/docs/container-deployment-standard.md` — deployment standards (S7 secret target, S8 naming, S12 RestartSec=10)
