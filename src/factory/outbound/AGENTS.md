# src/factory/outbound/ — Outbound Stage Composition
# HELPERS per part + stage purity enforced via generalized .importlinter contracts (per-part-stage-helpers-isolation, stage-purity).
# See root AGENTS.md Core + .importlinter (anti-patchwork: no new narrow contracts; consolidated from prior low-consensus proposals).

## Purpose

Per-platform-agnostic stage composition for outbound message rendering. Consumed by
`OutboundAdapterBase.send_streaming` via `_make_emitter` on platform adapters (Telegram,
Discord). The per-target outbound files (`telegram_outbound.py`, `discord_outbound.py`)
delegate to `OutboundEmitter` (composition); state helpers live in `_streaming_state.py`.

## Layer contract

```
OutboundEmitter (generic; composes stages)
     ↓ composes
[OutboundFormatter Protocol, ThrottleCapability Protocol, OutboundErrorHandler]
     ↓ used by
Per-platform impls (telegram_formatter.py, discord_formatter.py, *TypingIndicator)
```

`OutboundEmitter` is the only assembly point — it owns the placeholder→edits→delivery
lifecycle. Platform adapters construct it in `_make_emitter`; they never call
formatter/throttle/error_handler methods directly.

## Key invariants

- **No second circuit-breaker layer here.** CB lives in `OutboundDispatcher` (hub-side). Outbound stages MUST NOT add a second CB.
- **SanitizedError only on bus-bound paths.** All exceptions caught by
  `OutboundErrorHandler.guard` are converted to
  `SanitizedError(message=type(exc).__name__, …)` — never `str(exc)`. The discipline
  kills the `str(exc)` leak class (#1212 etc.).
- **OutboundAdapterBase has NO `__init__`.** Discord MRO constraint:
  `DiscordAdapter.__init__` flows to `discord.Client(intents=intents)` via
  `super().__init__(intents=intents)`. Do NOT add `__init__` to any class in the
  `OutboundAdapterBase` inheritance chain.
- **Single broad-catch site in the emitter.** `OutboundErrorHandler.guard` is the single semantic broad-catch site in `factory.outbound/`. Two additional terminal sites in `OutboundEmitter.run` and `_run_event_loop` capture stream errors with broad-catch — these are intentional (terminal stream-error path).

## State + recap helpers

`IntermediateTextState`, `StreamState` → `factory.outbound._streaming_state`.
`ToolRecapAccumulator`, `format_recap_lines` → `factory.outbound._tool_recap`.
No deferred-import block exists.

## ToolDisplayConfig wiring

`ToolDisplayConfig` (from `factory.core.messaging`) is injected via
`OutboundAdapterBase.send_streaming` — the **single WRITE site** for
`emitter.tool_display_config` (→ `docs/architecture/adapters.md` § Outbound stage composition). Concrete `_make_emitter` overrides MUST NOT
assign this attribute; doing so re-introduces the target-axis-trap Phase B (#1336) removed.
Thresholds (`bash_max_len`, `group_threshold`, `names_threshold`) are config-driven inside
`ToolRecapAccumulator`; `_route()` gates via `config.show` for visibility control.

## Audio delivery path (durable — #1482)

Outbound audio uses a separate durable JetStream path, NOT the text-chunk Core path:

- Subject: `factory.outbound.audio.<platform>.<bot_id>` (5 tokens — distinct from 4-token text path)
- Stream: `FACTORY_OUTBOUND_AUDIO` (Limits retention, `MaxAge=24h`)
- Consumer: durable pull `outbound-audio-{platform}-{bot_id}` (one per bot, e.g. `outbound-audio-telegram-main`, `outbound-audio-discord-main`)
- Dedup: KV bucket `factory_outbound_audio_sent` keyed on `stream_id`

Hub publishes and returns immediately (stateless, Model A). Adapter owns the ACK after
platform API confirms. Text path (`factory.outbound.<platform>.<bot_id>`, Core) is unchanged.

ACL and stream provisioning: T7 (ACL grants) and T14 (stream/consumer/KV bootstrap) — both
provisioned by hub sole-provisioner (→ `docs/architecture/messaging.md` Key invariants).

## Enforcement: bus-bound str(exc) gate

`tools/check_str_exc_bus_bound.sh` (quality gate, `stages: [ci]` per `.dev/stack.yml`)
covers `src/factory/outbound/` as one of its six bus-bound scan roots (`SCAN_ROOTS`). It blocks `str(exc)` / `f"{exc}"` / `repr(exc)` from
flowing into `SanitizedError` construction or NATS-bus-bound message fields. Escape hatch:
`# str-exc-ok: <reason>` on the offending line. Debt from #1279 retired by #1835.
