# AGENTS.md Registry

Instruction content lives in `AGENTS.md` — one per subsystem, read by every harness. Update here on add/rename/delete.

| AGENTS.md | Scope |
|---|---|
| `AGENTS.md` | project root |
| `src/factory/core/AGENTS.md` | hub, stores, pool |
| `src/factory/adapters/AGENTS.md` | Telegram, Discord, CLI, NATS |
| `src/factory/adapters/omp/AGENTS.md` | OmpWorker NATS adapter — digest gate, `tool_input` suppression, `_result_sent` guard, SanitizedError discipline |
| `src/factory/inbound/AGENTS.md` | stage-axis inbound pipeline (parser, router, session, dispatcher) |
| `src/factory/agents/AGENTS.md` | agent impls |
| `src/factory/blobstore/AGENTS.md` | HTTP-fronted BlobStore service (peer-of-adapters, #1330 V8) |
| `src/factory/bootstrap/AGENTS.md` | process bootstrap (standalone, wiring, lifecycle, factory, infra) |
| `src/factory/commands/AGENTS.md` | plugin commands |
| `src/factory/infrastructure/AGENTS.md` | store implementations (engineering-standards layering) |
| `src/factory/integrations/AGENTS.md` | external boundary layer (supervisor, systemctl, vault-cli, web-intel) |
| `src/factory/agent_cmd/AGENTS.md` | agent + bot CLI commands — applicative layer above core |
| `src/factory/llm/AGENTS.md` | LLM drivers |
| `src/factory/monitoring/AGENTS.md` | standalone health-check subsystem (`python -m factory.monitoring`) |
| `src/factory/obs/AGENTS.md` | observability — live OTel tracing (`hub_tracer`/`otel_wiring`/`otlp_export`, wired into runtime, #2069) + forward-facing `ObservabilityProvider` Langfuse abstraction (`base`/`noop`, #1235) |
| `src/factory/outbound/AGENTS.md` | outbound stage composition (formatter/throttle/error_handler/emitter, #1279) |
| `src/factory/streaming/AGENTS.md` | stage-axis streaming primitives (parser Protocol, state_machine, event_emitter) — composed by CliStreamingParser + StreamProcessor (#1282) |
| `src/factory/transport/AGENTS.md` | NATS transport + WorkerPoolClient (3-layer primitives, #1278) |
| `src/factory/infrastructure/turn_writer/AGENTS.md` | JetStream subscriber-writer for turns.db (#1331) — sole writer (storage.md) |
| `src/factory/infrastructure/outbound_audio/AGENTS.md` | JetStream stream + consumer + KV provisioning for durable outbound-audio path (#1482) |
| `src/factory/infrastructure/jobs/AGENTS.md` | FACTORY_JOBS WorkQueue stream + DLQ router provisioning (messaging.md, #1203) |
| `src/factory/nats/AGENTS.md` | in-tree NATS integration (subjects, codec, domain clients) |
| `src/factory/tools/AGENTS.md` | GitHub token dispenser (gh_token helper) |
| `src/factory/dashboard/AGENTS.md` | control-plane BFF axis (observability.md) — session-ID footgun |
| `packages/roxabi-nats/AGENTS.md` | NATS transport SDK (contracts.md) |
| `packages/roxabi-contracts/AGENTS.md` | NATS contract schemas (contracts.md) |
| `packages/roxabi-blobs/AGENTS.md` | BlobStore client SDK (consumed by hub + adapters) |
| `packages/roxabi-otel/AGENTS.md` | OTel impl of MessageLifecycleHooks (keeps OTel out of roxabi-nats) |
| `packages/roxabi-obs/AGENTS.md` | fleet plane ③ reporter — periodic ContainerReport publish |
| `packages/roxabi-satellite/AGENTS.md` | shared NATS satellite plumbing for GPU worker CLIs |
| `plugins/factory-ops/AGENTS.md` | ops plugin (debug, remote inspection) |
| `plugins/refine-agent/AGENTS.md` | agent-profile refine plugin |
| `tools/AGENTS.md` | quality gates + analysis scripts |
| `scripts/AGENTS.md` | platform orchestration (bash) + domain operational tooling — scripts/ vs tools/ boundary |
| `deploy/AGENTS.md` | Podman + Quadlet prod deploy (reference impl) |
