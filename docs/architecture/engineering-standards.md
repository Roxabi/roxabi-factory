---
title: Engineering Standards — Roxabi
description: Cross-repo engineering doctrine — Clean/Hexagonal/Kernel layering, ADR-anchored engineering invariants, testing conventions, and CI quality gates.
---

# Engineering Standards — Roxabi

> Status: LIVING — current truth for cross-repo engineering doctrine.
> Last updated: 2026-07-02.
> Source ADRs: 009 (archived), 058 (archived), 059, 089. Absorbed via 059: 048, 060.

## Scope

All Roxabi projects, maintained in roxabi-factory. This page defines the doctrine every
repo follows: the architectural pattern stack (Clean → Hexagonal → Kernel), the canonical
4-layer dependency model and Composition Root convention (ADR-059), the user-visible
error contract (ADR-058 as amended, ADR-089, archived ADR-009), mandatory testing
conventions, and how CI quality gates are declared and run. It does not cover
factory-specific domain behavior (messaging, storage, workers — see the sibling domain
pages) or container deployment standards (see `deployment.md`).

## Current state

### Pattern hierarchy

```
Clean Architecture (Martin)
    └──▶ Hexagonal Architecture (Cockburn — Ports & Adapters)
              └──▶ Kernel doctrine (Roxabi extension)
```

Each pattern **refines** the previous — adding structure, not replacing it. All three
apply simultaneously; they are layers of the same onion, not alternatives.

| Pattern | Key rule |
|---------|----------|
| Clean Architecture | Dependencies point inward |
| Hexagonal Architecture | Core isolated via ports/adapters |
| Kernel doctrine | Core is minimal, pure, immutable |

### Clean architecture — canonical 4-layer model

Four layers, innermost to outermost. Imports point inward only:

| Layer | Contains | May import from |
|-------|----------|-----------------|
| **Domain** | Entities, port interfaces (Protocol/ABC), domain exceptions, pure business rules | Nothing outside its own layer — zero I/O imports |
| **Application** | Use cases, command handlers, stateless orchestration | Domain only — type-hint against ports, never concretions |
| **Infrastructure** | Port implementations: stores, transport, model loaders; migration runners, connection pools | Domain (to implement ports) |
| **Adapters** | Platform I/O channels (chat platforms, CLI, NATS adapters); inbound/outbound translation | Application + Domain; never other adapters |

In factory the layers map to `factory.core` (Domain, ports under `factory.core.ports`),
the application packages above it, `factory.infrastructure`, and `factory.adapters`.
Sibling repos replicate the same tiering with their own module names. → ADR-059

**Composition Root.** The single location where concrete Infrastructure implementations
are wired to Domain ports — `factory.bootstrap` in factory, the equivalent bootstrap
entry point elsewhere. It is the only site that imports a port and its implementation
simultaneously. Factory functions that select concretions live here, never in Domain or
Application. → ADR-059

**Port placement (canonical fix shape, absorbed ADR-060).** When a port and its
Infrastructure consumer end up co-located (circular import risk), the fix is always the
same: define the port in Domain and have Infrastructure import it from there. Example:
the `LlmProvider` port is defined in `factory.core.ports.llm`; drivers in `factory.llm`
implement it.

**Enforcement.** Layer and forbidden-import contracts are machine-checked by
`.importlinter` (a layers contract plus forbidden contracts keeping I/O libraries out of
Domain), run as the `import_layers` gate. → `workers-tooling.md` (ADR-061)

### Hexagonal ports & adapters

**Principle.** The Domain Core knows no framework, no channel, no LLM provider.
Everything specific plugs in behind a port.

- A **port** is an interface (Python Protocol or ABC) that the domain defines and owns.
- An **adapter** implements a port for a specific technology and may hold framework/I/O
  dependencies.
- **Inbound** adapters (external → domain) normalize raw platform events into frozen
  domain types via `normalize()`; **outbound** adapters (domain → external) denormalize
  domain types into platform calls.

Invariants (one canonical statement — previously duplicated across the architecture
manifesto and pattern docs):

1. The Domain Core never imports platform SDKs, LLM-provider SDKs, or HTTP clients.
2. Core stream processing (`StreamProcessor`) is testable in isolation — no network.
3. A new outbound adapter only needs to understand `RenderEvent`.
4. A new LLM backend only needs to implement the `LlmProvider` port.

### Kernel doctrine (Roxabi extension)

The kernel is the innermost slice of Domain: protocols, frozen event/entity types, and
pure functions.

| Kernel constraint | Rationale |
|-------------------|-----------|
| No framework imports | Framework-agnostic |
| No file/network I/O, no side effects | Pure, deterministic |
| No mutable global state; types frozen | Immutability |
| Pure functions (input → output) | Testable in isolation |

| Plugin constraint | Rationale |
|-------------------|-----------|
| Must implement a kernel-defined port | Contract enforcement |
| May have framework/I/O dependencies | Isolated side effects |
| Must not import other plugins | Loose coupling |
| Communicates via kernel-defined event types | Decoupling |

### User-visible error contract

**Load-bearing principle (ADR-058).** A user-visible error needs an explicit reply path,
or it drops silently. Every family of user-visible failures has exactly one typed catch
site that maps the failure to a message-template key and dispatches a reply — never raw
exception text.

ADR-058's original unified mechanism (a typed user-error exception hierarchy, a pipeline
error-boundary middleware, and a null message manager) was **never implemented** — the
ADR was amended 2026-07-01 to record this; do not grep for its symbols. The live contract
is realized per catch site:

- **Adapter-level media failures** (download/size) — inline template-keyed replies via
  `MessageManager` directly in the platform adapters.
- **Inbound STT failures** — `SttMiddleware` catches speech-to-text exceptions inline in
  the pipeline, dispatches a template-keyed reply, then drops the message.
- **Terminal stream / outbound infrastructure errors** — `OutboundErrorHandler` maps
  terminal stream exceptions to template keys for the platform adapter. Its scope is
  infrastructure errors only; it must not absorb LLM soft errors.
- **LLM backend soft errors** — centralized resolution, below (ADR-089).

**Centralized LLM user-error resolution (ADR-089).** Classification and presentation are
separate responsibilities:

- Workers and parsers **classify** origin-specific failures into a structured
  `WorkerError` — a stable code from the shared registry in `roxabi_contracts.errors`
  plus a scrubbed message. All LLM backends converge on the same structured field of
  `LlmResult` before presentation; codecs and drivers do wire decode only, no i18n.
- A single domain resolver (`resolve_user_error()` in core messaging utils) **presents**
  the error: template lookup via `MessageManager`, with a reviewed passthrough allowlist
  for codes whose upstream message is safe to show. Per-backend or per-callsite user
  strings are forbidden — one policy for every backend, blocking and streaming paths
  alike.
- Security rules, enforced in the resolver: never show raw exception text or transport
  diagnostics to users; infrastructure error codes always resolve to the generic
  template; passthrough is only allowed for allowlisted codes whose message was scrubbed
  at the classification boundary.

**Cross-layer exceptions.** Exception types raised in outer layers but consumed
elsewhere are defined at the innermost layer they logically belong to
(`factory.core.exceptions` in factory) — never imported downward from an outer layer.

### Shared UI-primitive constants (ADR-009, archived)

`GENERIC_ERROR_REPLY` lives in the core messaging module, co-located with the `Response`
type it populates — it was moved out of the hub coordinator because an agent (a spoke)
was importing a UI string from the hub. The general rule: a shared constant lives in the
lowest-dependency module that owns the type it populates; spokes never import from the
coordinator for a UI primitive.

### Testing conventions

#### Negative-test rule (mandatory)

**Every guard must ship with a negative test** — a test that **fails** when the guarded
branch is removed, the filter is bypassed, or the protocol method is deleted.

What counts as a guard: `if`/`elif` branching; `else` branches encoding a distinct
error path; `None`-checks and early returns; filter expressions; Protocol/ABC method
implementations; exception catches that alter control flow.

A test that still passes when the guard is deleted is tautological and does **not**
satisfy the rule — e.g. asserting only the happy path, suppressing the very warning the
test asserts, or checking a re-export at import time only.

```python
def process(value: str | None) -> str:
    if value is None:          # <-- guard
        raise ValueError("value required")
    return value.upper()

def test_process_raises_on_none():   # fails if the guard is deleted
    with pytest.raises(ValueError, match="value required"):
        process(None)
```

Enforcement: missing negative tests are flagged during code review. This is a
**merge blocker**.

#### Mock boundaries & coverage

- Import and call real source functions — never mock the module under test.
- Patch at the import site of the *dependency*, never the module under test itself.
- Integration tests (real modules wired together) beat unit tests with heavy mocks.
- Verify coverage on the module under test; 0% coverage on it means the mocking is wrong.

#### Test taxonomy (trophy, priority order)

1. **Static** — type checker + linter (automatic, via quality gates).
2. **Unit** — pure functions, utilities, type guards.
3. **Integration** (largest layer) — real modules wired together.
4. **E2E** — critical journeys only.

Layer-to-test-type mapping:

| Layer | Test type | Constraints |
|-------|-----------|-------------|
| Kernel / Domain core | Unit, no mocks | Zero external dependencies; fast; full business-logic coverage |
| Adapters / Infrastructure | Integration, doubles at the port boundary | Patch dependencies at their import site |
| Full system | E2E with real services | Critical journeys only |

### CI quality gates

Quality gates are **declared in `.dev/stack.yml`** and **run by `scripts/qg`** — a
bash + yq runner with no generated hook wiring:

- The `quality_gates` map declares each gate: script, stages, an optional changed-files
  regex, binary requirements, per-gate env, and an optional CI-only script override.
- `qg.run_order` lists which gates run at each stage (pre-commit, pre-push, ci) and in
  what order; `qg.profiles` defines named subsets. **A gate absent from `run_order` does
  not run at that stage** — declaring it is not enough.
- Invocation: `scripts/qg run --stage ci`, `scripts/qg run --profile local`, or a single
  gate by name. Pre-commit/pre-push stages skip gates whose changed-files regex matches
  nothing; the ci stage runs everything.
- Gate implementations live in `tools/` (canonical source is the dev-core plugin;
  project copies are not edited directly); repo-specific domain scanners live in
  `scripts/`.
- Exit-code contract: 0 = ran clean, 1 = violations found, 2+ = the script itself broke.

Static checks (lint, typecheck), unit tests, and doc-drift gates are all stages of this
one runner — adding a check means declaring a gate in `stack.yml` and adding it to
`run_order`, not adding an ad hoc hook.

## Key invariants

- Dependencies point inward: Domain imports nothing from outer layers; Application
  imports Domain only; Infrastructure implements Domain ports; Adapters are the outer
  ring and are never imported by inner layers.
- Ports are domain-owned; adapters implement them. Lateral adapter-to-adapter imports
  are forbidden.
- Concretions are instantiated only in the Composition Root (`factory.bootstrap` in
  factory); no factory selecting implementations may live in Domain or Application.
- Layering is machine-enforced via `.importlinter`; new modules must be added to the
  contracts, not exempted.
- Key ADR invariants are covered by generalized contracts (see .importlinter comments for details and anti-patchwork consolidation):
  - ADR-059 (Hexagonal/Clean Architecture canonical model, Composition Root, ports): `clean-architecture-layers`, `core-ports-purity`, `composition-root-isolation`, `shared-modules-upper-boundary`, `per-part-stage-helpers-isolation`, `no-direct-*` rules, `application-no-direct-infra`.
  - ADR-073 (Stage as primary axis of decomposition): `stage-purity`, `per-part-stage-helpers-isolation`, generalized `shared-modules-upper-boundary` + `shared-modules-independence`, `no-direct-bot-store`.
  - Related absorbed: ADR-061 (independence/port imports), ADR-078 (store protocols via no-direct rules).
  - commands-no-infra + agents-no-bootstrap merged into `application-no-direct-infra` (broader application layer isolation from infra/CR).
- A user-visible error must have an explicit, typed catch site that replies via a
  message-template key — no silent drops, no raw exception text shown to users.
- A user-visible error must have an explicit, typed catch site that replies via a
  message-template key — no silent drops, no raw exception text shown to users.
- LLM backends classify failures into structured `WorkerError` codes; presentation
  happens once, in the domain resolver — never per backend or per callsite.
- Cross-layer exception types are defined at the innermost layer they belong to.
- Shared UI-primitive constants live in the lowest-dependency module that owns the type
  they populate, never in a coordinator module.
- Every guard ships with a negative test that fails when the guard is deleted — a merge
  blocker at code review.
- Never mock the module under test; patch dependencies at their import site.
- Quality gates run only if declared in `.dev/stack.yml` **and** listed in
  `qg.run_order` for the stage; local pre-push green does not imply CI green (some gates
  are ci-only).

## See also

- Storage & infrastructure store placement → `storage.md`
- Importlinter enforcement and workers tooling → `workers-tooling.md`
- Streaming pipeline (`StreamProcessor`, `RenderEvent` protocol) → `llm-streaming.md`
- Messaging & NATS invariants → `messaging.md`
- Deployment standards → `deployment.md`
- Generalized contracts + ADR-059 (Clean/Hex) / ADR-073 (stage axis) coverage → `.importlinter` (with comments) and AGENTS.md Core section. (See "nettoyage" consolidation for reduced patchwork.)

## ADR archive

| ADR | Title | Status |
|-----|-------|--------|
| 009 | `GENERIC_ERROR_REPLY` placement and agent→hub decoupling | Superseded — archived (consolidation v2, 2026-07-01); decision absorbed here |
| 048 | Infrastructure layer for persistence | Absorbed by ADR-059 |
| 058 | Typed error boundary and user-visible error contract | Superseded — archived 2026-07-02 (Option B never built; the surviving principle lives in § User-visible error contract) |
| 059 | Hexagonal / Clean architecture canonical model | Accepted — absorbs ADR-048, ADR-060 |
| 060 | CLI protocol circular import resolution | Absorbed by ADR-059 |
| 089 | Centralized LLM user-error resolution | Accepted |
