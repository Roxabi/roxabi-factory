# factory — Architecture & Decisions

> Living hub. Routes to domain pages (SSoT) + ADRs (historical why). Updated as decisions are made.

---

## How to read this docs tree

**Start here for current state**: the living domain pages below, each the single source of truth for its area.

| Domain page | What it owns |
|---|---|
| [messaging.md](architecture/messaging.md) | NATS planes & subject naming, routing key, hub dispatch, KV readiness, bot roster |
| [llm-streaming.md](architecture/llm-streaming.md) | LlmEvent → StreamProcessor → RenderEvent pipeline, driver stack, chunk protocol + codec, schema versioning |
| [adapters.md](architecture/adapters.md) | Telegram, Discord, CLI inbound, audio routing, TTS overlay |
| [storage.md](architecture/storage.md) | Agent / thread / blob stores, memory scope, event bus DI |
| [security-routing.md](architecture/security-routing.md) | Auth, trust, command parser, memory isolation, NATS infra security |
| [deployment.md](architecture/deployment.md) | C3 container split, Quadlet ecosystem, autodeploy, hardware specs |
| [contracts.md](architecture/contracts.md) | roxabi-nats SDK, roxabi-contracts schemas, transport layer, voice routing + lifecycle |
| [workers-tooling.md](architecture/workers-tooling.md) | Tool taxonomy & runtime vocabulary, CliPool, processor registry, tool integration, importlinter |
| [scrape-placement.md](architecture/scrape-placement.md) | Web scrape placement — hub subprocess → HTTP service / agent tool; vault-add removed |
| [job-model.md](architecture/job-model.md) | Job model — `job_id`=run, lifecycle, active-jobs registry, `factory.job.<id>.*` taxonomy, transport tiers, sub-jobs, runtime control |
| [dev-factory.md](architecture/dev-factory.md) | **Dev Factory** (design) — usine de dev Gosilex/Spark : architecture, flow, Work vs Job, sandboxes ; vocabulaire → [dev-factory-terminology.md](architecture/dev-factory-terminology.md) |
| [observability.md](architecture/observability.md) | Observability planes, control-plane dashboard, trace + log engines, operator audit, fleet/pipeline read models, ingress |

**Dev Factory — bases de réflexion** (`status: reflection`, non-normative — ¬SSoT runtime) :

| Note | What it explores |
|---|---|
| [dev-factory-work-contract.md](architecture/dev-factory-work-contract.md) | Champs & états du Work |
| [dev-factory-runners.md](architecture/dev-factory-runners.md) | Catalogue runners + StepResult |
| [dev-factory-spark-ingress.md](architecture/dev-factory-spark-ingress.md) | Ingress Spark + Outbound |
| [dev-factory-implementation-slices.md](architecture/dev-factory-implementation-slices.md) | Ordre de build / slices |
| [engineering-standards.md](architecture/engineering-standards.md) | **Cross-repo doctrine** (all Roxabi repos) — Clean/Hexagonal/Kernel layering, error contract, testing conventions, CI quality gates |
| [CURRENT.generated.md](architecture/CURRENT.generated.md) | **Generated** — machine-generated inventory SSoT: layers, subjects, topology, entry points |

**Decision archive** — the ADRs in [`architecture/adr/`](architecture/adr/) preserve historical reasoning; superseded records move to `adr/archive/`. Each ADR has a redirect banner to its domain page and a decision-stating title — the title IS the index. **Read ADRs only when you need the *why* behind a decision**, not the *what*. Per-domain listings (status + one-line summary) live in each domain page's "ADR archive" table. Two ADRs live outside the domain-page system: ADR-086 (Documentation Architecture — the meta-decision for this tree itself) and ADR-101 (Runtime Brand Tokens — current truth in `brand/` + `packages/shared/`).

---

## Retrieval ladder — one question, one hop, one home

The single entry index (materializes ADR-086 § Retrieval ladder). The comprehension **axes** below — glossary · topology · structure · behavior · dynamics · delta · procedure — are retrieval *intents* (query columns), **not** storage folders: each question resolves to exactly **one** home. Facts live at the **altitude** whose freshness a machine can enforce — generated (L0) → intent/invariant (L1) → procedure (L2) → history (L3). Never hand-copy a fact that already has a home elsewhere; link to it.

| Your question | → one hop | Axis · altitude |
|---|---|---|
| Is this rule repo-local or org-wide? | org-wide → `~/projects/ssot/*.ssot.md`; repo-local → the domain pages above | routing |
| What does a term mean / which sense? | grep the name in its owning domain page; homonyms (plane, event, axial) carry a "distinguish from" note | glossary · L1 |
| What runs where / talks to whom? | [CURRENT.generated.md](architecture/CURRENT.generated.md) · `deploy/quadlet.toml` · `deploy/nats/acl-matrix.json` | topology · L0 |
| Who **may** import whom / who **does**? | `.importlinter` (the rule) · CURRENT.generated.md layer map (the fact) | structure · L1/L0 |
| What must always stay true here? | the owning domain page's **Key invariants** → follow to the enforcing gate in `.dev/stack.yml` | behavior · L1→gate |
| What is the job/turn lifecycle or stream sequence? | [job-model.md](architecture/job-model.md) + [llm-streaming.md](architecture/llm-streaming.md) | dynamics · L1 |
| What does my change endanger? | run the gates (`scripts/qg`) + the axial-review PR label; each invariant names its gate | delta · derived |
| Where is the code for Y? | `ccc` (semantic) or grep (exact) — discovery, **no** normative doc home | — |
| Why is it built this way? | the domain page's **ADR archive** table → the ADR | provenance · L3 |
| How do I do X right now? | [runbooks/README.md](runbooks/README.md) | procedure · L2 |

Design rationale: ADR-086 (this tree's meta-decision). "axial" in the delta row = the layer-crossing PR review label, **not** ADR-073's stage-of-pipeline decomposition axis (same word, different concept).

---

## What is factory

Hub-and-spoke AI factory engine. One hub routes inbound messages from multiple platforms (Telegram, Discord, CLI) to per-conversation pools backed by a Claude CLI subprocess. Responses stream back through NATS to the originating adapter. All state is per-pool; agents are immutable singletons. Multiple bots per platform are supported via independent bindings.

```
factory_telegram                     factory_hub                      factory_discord
─────────────                   ─────────────                  ─────────────
aiogram long-poll                NatsBus                       discord.py gateway
      │                              │                              │
      │ factory.inbound.telegram.<bot>  │ factory.inbound.discord.<bot>   │
      ├─────────────────────────────▶│◄─────────────────────────────┤
      │                              │ InboundBus → Hub → resolve_binding()
      │                              │                              │
      │                              │ get_or_create_pool()         │
      │                              │                              │
      │                    factory.jobs.claude ──▶ lyra_clipool process│
      │                              │         │                    │
      │                    factory.clipool.heartbeat ◄─┘              │
      │                              │                              │
      │ factory.outbound.telegram.<bot>   │  factory.outbound.discord.<bot> │
      ◄──────────────────────────────┴──────────────────────────────▶
```

The Quadlet containers on Machine 1 (`factory-hub` role) are the enabled `[component.*]` sections in the manifest `deploy/quadlet.toml` (SSoT); Langfuse and the legacy `factory-otel-collector` units ship disabled. Do not maintain a parallel list here — `deploy/quadlet.toml` and `docs/architecture/CURRENT.generated.md § topology` enumerate them. NATS topics: `factory.inbound.<platform>.<bot_id>` (adapter→hub), `factory.outbound.<platform>.<bot_id>` (hub→adapter). `factory start` runs hub + adapters in one process with embedded NATS.

### Jobs & workers

- **`OmpWorker`** — `omp_rpc` backend worker container (`factory-omp`); CLI entry `factory adapter omp` (#1812). → [workers-tooling.md](architecture/workers-tooling.md)
- **FACTORY_JOBS WorkQueue + DLQ router** — JetStream work-queue stream for job dispatch; dead-letter routing on failure (#1203). → [messaging.md](architecture/messaging.md)
- **factory-active-jobs KV registry** — NATS KV bucket tracking in-flight jobs, written by hub on dispatch (#1796). → [job-model.md](architecture/job-model.md)
- **Work envelope + job_id invariant** — every dispatched unit carries a stable `job_id`; lifecycle contract defined in ADR-084 (#1619). → [job-model.md](architecture/job-model.md)
- **`factory.job.<id>.*` taxonomy** — unified NATS subject namespace for per-job control and status (#1793). → [messaging.md](architecture/messaging.md)

---

## Key Invariants

1. **Library first** — `factory` is a Python library; CLI is a thin shell. `uv add --editable path/to/lyra` works.
2. **No side effects on import** — engines load lazily; `import factory` is instant.
3. **Agent = stateless singleton** — immutable config (prompt, permissions, namespace). All mutable state lives in the Pool.
4. **Adapters send `trust=PUBLIC`** — trust resolution is Hub-side only (C3 pattern). Adapters never decide who may speak.
5. **User-scoped memory** — every memory query must include `user_id`. Even stats, aggregate per user.
6. **Bounded queues** — `asyncio.Queue(maxsize=100)` per channel. Full → immediate ack + blocking `await put()`.
7. **Module ≤300 LOC** — enforced by `check_file_length.sh`. Hub, agent, pool, adapters all decomposed.

---

## Agents, Bots, and Bindings

| Concept | What it is | Where it lives | Managed by |
|---|---|---|---|
| **Bot** | Platform identity (`@RoxabiLyraBot`). Owns token, `bot_id`, `platform`. | `config.toml` → `BotStore` (`config.db` `bots`) | `factory bot init` |
| **Agent** | AI brain. `model`, `backend`, `persona`, `tools`, `plugins`. | `src/factory/agents/*.toml` → `AgentStore` (`config.db` `agents`) | `factory agent init` |
| **Binding** | `bot_id` → `agent_name` for a conversation scope. | `config.db` `bot_agent_map` | `factory agent assign` / `unassign` |

Routing: `(platform, bot_id, scope_id)` → `(agent, pool_id)`. One pool per scope. Scope = `chat:{id}` | `thread:{id}` | `channel:{id}`.

---

## Current Status

**Phase 1b complete.** All items shipped. Phase 2 (atomic SLMs) deferred until Machine 1 VRAM budget is validated.

See [GitHub issues](https://github.com/Roxabi/roxabi-factory/issues) for backlog and priorities.
