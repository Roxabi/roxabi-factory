# Post-mortem: NATS ACL inbox case mismatch (2026-04-27)

## Summary

All four bot channels (Telegram lyra/aryl, Discord lyra/aryl) were fully
unavailable for ~3h15m (15:34 deploy → 19:30 restored). Every user message
returned a silent failure (bot received the message, no reply was ever sent).
LLM-streamed responses were the only affected path; no commands, history reads,
or other features were tested for availability.

Root cause: hub's subscribe ACL was missing the lowercase _inbox.hub.> variant,
so clipool replies never reached the hub after a new image was deployed. Symptom
was `_stream_gen timeout on factory.jobs.claude` in hub logs and repeated
permissions violation for publish to "_inbox.hub.*" in clipool logs.
Detection lag: **2h44m** from first failure to mitigation start. No alert fired;
discovered via manual log inspection.

---

## Timeline

| Time (CEST) | Event |
|---|---|
| ~11:17 | Anthropic API 401 errors — unrelated (API key issue, self-resolved by ~12:23) |
| ~13:17–14:23 | CLI invocations succeed (hub PID 1585354, no ACL issue yet) |
| 15:34 | Hub restarted → new PID 1680947, new image pulled |
| 16:11 | First permissions violation for publish to "_inbox.hub.*" in clipool |
| 17:15, 18:03 | Repeated violations — every user message produces ❌ |
| 18:55 | Hub restarted as part of investigation + NATS secret updated |
| 19:30 | NATS restarted with new auth.conf (Podman secret); all units reconnect |
| 19:30+ | No further permission violations |

---

## Root cause

### What the code does

`hub_standalone.py` connects with inbox_prefix="_INBOX.hub":

```python
nc = await nats_connect(nats_url, inbox_prefix="_INBOX.hub")
```

`NatsDriverBase._stream_gen` creates ephemeral inboxes via `nc.new_inbox()`:

```python
inbox = self._nc.new_inbox()   # → "_INBOX.hub.<NUID>"
await self._nc.publish(subject, payload, reply=inbox)
```

This is uppercase. Confirmed by exec into the running hub container:

```
_inbox_prefix bytes: b'_INBOX.hub'
new_inbox():         _INBOX.hub.tjt1ZZrZCAPdpKe6JhWAdT
```

### What NATS server does

NATS server 2.10.29 (Go) lowercases subject names in `-ERR` permission violation
messages. The actual wire subject is _INBOX.hub.<NUID> (uppercase), but the
server reports:

```
nats: permissions violation for publish to "_inbox.hub.3bimc4lrihwrpg4kx8k2ox"
```

This misled the investigation into thinking nats-py was generating lowercase
inboxes. The subjects on the wire were uppercase throughout.

### The actual gap

Hub's subscribe ACL before the fix:

```
subscribe: { allow: ["_INBOX.hub.>"] }   # uppercase only
```

The `allow_responses: true` in clipool's ACL grants publish permission to the
exact reply-to subject from the received message. Because the ACL evaluation by
NATS server appears to match case-insensitively for `allow_responses` grants but
applies the subscription filter case-sensitively, replies published by clipool
to _INBOX.hub.<NUID> did not match the hub's subscription to _INBOX.hub.>.

> **Note:** `allow_responses` case-sensitivity is observed behaviour, not a NATS
> spec guarantee. A future NATS release could change this; Fix 2 (explicit
> reply-path ACLs) is the only durable defence.

Exact failure chain:
1. Hub publishes to `factory.jobs.claude` with `reply="_INBOX.hub.TOKEN"`
2. Clipool receives message, attempts to publish streaming chunks to _INBOX.hub.TOKEN
3. NATS reports permission violation (displayed as lowercase in error)
4. Hub subscription _INBOX.hub.> receives nothing → `_stream_gen` times out
5. Users get ❌

### Why it worked before

Hub PID 1585354 (running from 08:40) was on an older image. The new image
deployed at 15:34 contained changes from commit `fd3250c4` (adding
inbox_prefix="_INBOX.hub") and accompanying ACL narrowing. The combination of
a fresh uppercase prefix and the narrowed clipool publish ACL (removing the old
_INBOX.> wildcard, added in #949) exposed the gap.

---

## Fix applied

Added _inbox.hub.> to hub's subscribe ACL in `acl-matrix.json` and the live
Podman secret:

```json
"hub": {
  "subscribe": [
    "...",
    "_INBOX.hub.>",
    "_inbox.hub.>"    // ← added
  ]
}
```

Same dual-case pattern already used by tts-adapter, stt-adapter, voice-tts,
voice-stt for nats-py compatibility. NATS secret recreated and NATS container
restarted to apply.

---

## Long-term solutions

### Fix 1 — Standardize on lowercase _inbox prefix (eliminates dual-variant)

The dual-case pattern (_INBOX.X.> + _inbox.X.>) is a permanent maintenance
burden. Every new identity, every ACL audit, every `gen-nkeys.sh --regenerate`
must carry both variants or the next case-mismatch bug ships silently.

**Action:** Change all bootstrap connects to lowercase:

```python
# hub_standalone.py, adapter_standalone.py, clipool_standalone.py, voicecli
nc = await nats_connect(nats_url, identity_name="hub")   # already in a832f5c8
# OR explicit:
nc = await nats_connect(nats_url, inbox_prefix="_inbox.hub")
```

Update `nats_connect()` in roxabi-nats: `identity_name` → f"_inbox.{identity_name}".
Update all `acl-matrix.json` subscribe entries to lowercase. Drop the uppercase
_INBOX.X.> entries.

Result: one entry per identity, matches what NATS server reports in errors,
no divergence possible.

**Cross-repo coordination required:** voicecli (`voice-tts`, `voice-stt`) is a
separate repo with its own release cycle. Fix 1 must be coordinated across lyra
+ voicecli in a single release, or the ACL must carry dual-case entries during a
grace period until all services confirm lowercase in staging. Deploying Fix 1 in
lyra only while voicecli still uses uppercase is an identical silent breakage.

**Safe deploy order:**
1. `acl-matrix.json` update (add lowercase, keep uppercase during transition)
2. `gen-nkeys.sh --regen-authconf` → `make quadlet-secrets-install`
3. `make factory-nats reload`
4. Redeploy containers one at a time, verify each reconnects
5. Once all services confirmed on lowercase: drop uppercase entries, repeat 2–4

### Fix 2 — Make reply-paths explicit in `acl-matrix.json`

`allow_responses: true` is invisible in the ACL matrix. The fact that
clipool-worker needs to reach _inbox.hub.* is undocumented and only
inferable from the request-reply pattern at runtime.

**Action:** Add the requester's inbox to each responder's publish ACL:

```json
"clipool-worker": {
  "publish": ["factory.clipool.heartbeat", "factory.system.ready", "_inbox.hub.>"]
},
"voice-tts": {
  "publish": ["factory.voice.tts.heartbeat", "factory.system.ready", "_inbox.hub.>"]
},
"voice-stt": {
  "publish": ["factory.voice.stt.heartbeat", "factory.system.ready", "_inbox.hub.>"]
},
"image-worker": {
  "publish": ["...", "_inbox.hub.>"]   // same pattern — also uses allow_responses
}
```

This makes the dependency graph fully auditable in `acl-matrix.json` and
removes reliance on dynamic `allow_responses` grants for correctness.
`allow_responses: true` can be kept as defence-in-depth.

**Coupling trade-off:** responders now encode _inbox.hub.>, so a hub identity
rename or a new requester identity requires ACL updates across all responder
entries. Acceptable for current hub-spoke topology; does not auto-scale to
multi-requester topologies.

### Fix 3 — ACL integration test in CI

A wrong `auth.conf` currently ships silently; production breakage is the first
signal.

**Action:** Add `make test-acl` that:
1. Generates real ephemeral nkeys via `nk -gen user` (one per identity) — do
   **not** use `--template-only` output directly; the dummy pubkeys it produces
   are rejected by a real NATS server
2. Renders `auth.conf` from `acl-matrix.json` using the ephemeral pubkeys
3. Spins up NATS on `-p 0` (random port, avoids CI conflicts), waits for
   readiness probe (poll stdout for `Server is ready` — no `sleep` hardcode)
4. Connects as each identity using its nkey seed
5. Asserts each identity can subscribe/publish on all ACL-allowed subjects
6. Asserts cross-identity round-trips: hub→clipool→hub, hub→voice→hub
7. Asserts each identity is denied on subjects outside its ACL
8. Includes a fixture where `allow_responses` is removed — verifies explicit
   ACL grants alone are sufficient (documents Fix 2 as load-bearing)

Run in `pre-push` hook (alongside `import_layers`) so any `acl-matrix.json`
change is gated before it reaches staging.

**Limitation:** this validates ACL config, not client code. A service connecting
with a wrong `inbox_prefix` will still pass — the test only proves the ACL is
internally consistent.

**Prerequisites:** `nats-server` binary must be available in CI and dev
environments; document or add as a CI setup step.

---

## Recommended order

| Priority | Action | Effort | Blocker |
|---|---|---|---|
| P0 — now | Fix 2 — explicit reply-path publish in `acl-matrix.json` | 10-line JSON + regenerate secret | none |
| P0 — unblocked | Fix 1 — lowercase _inbox prefix + drop dual-variant (hub/adapters/image-worker/clipool-worker) | `nats_connect()` identity_name path + `acl-matrix.json` + redeploy | voicecli blocker resolved; voice-{tts,stt} already done |
| P0 — alongside Fix 1 | Fix 3 — ACL integration test in CI | new test script + Makefile target | Fix 1 must land first to test correct case |

Fix 2 is independent and low-risk — deploy immediately regardless of Fix 1
scheduling. It uses lowercase _inbox.hub.> (correct after Fix 1); during the
transition window before Fix 1 lands, also carry _INBOX.hub.> in each
responder's publish ACL.

Fix 3 is P0, not Medium: it is the only fix that would have prevented this
incident from shipping. Gate it in `pre-push` alongside `import_layers`.

> **Fix 1 voicecli blocker is resolved.** voicecli already connects with
> lowercase _inbox.voice-tts / _inbox.voice-stt (verified 2026-04-28).
> voice-{tts,stt} ACL entries in `acl-matrix.json` are already normalized to
> lowercase-only (commit `4f3a02d6`). Remaining Fix 1 scope: change
> `nats_connect()` `identity_name` path from _INBOX.{name} → _inbox.{name}
> and update ACL entries for hub, adapters, image-worker, clipool-worker.

Fixes 1+2 together eliminate the entire class of case-mismatch and
invisible-grant bugs. Fix 3 is the guardrail that catches the next one.

---

## Deploy guardrails (missing, would have limited blast radius)

- **No canary/staged rollout:** the new image at 15:34 went to all 4 channels
  simultaneously. A single-channel canary (e.g., aryl-telegram for 15 min)
  would have limited impact to 1 of 4 channels and cut detection to ~15m.
- **No post-deploy smoke test:** 1h37m elapsed between bad deploy (15:34) and
  first logged violation (16:11). An automatic hub→clipool→hub round-trip at
  deploy time would have caught this before any real user hit it. Smoke test
  must validate NATS round-trip correctness, not just process liveness.
- **No rollback procedure:** the fix was a forward-deploy. No documented path
  to roll back to a previous image. Given that a deploy caused this incident,
  rollback is a required runbook entry.

---

## Incident response gaps

- No user notification was sent (no Telegram/Discord status message, no status
  page). Users experienced silent failure for 3h15m with no acknowledgment.
- No defined owner or process for user communication during an incident.
- Severity: P0 (all users, all channels, full LLM response path, 3h15m). This
  event should be formally recorded against any SLO/SLA in place.

---

## Action items

| Item | Owner | Status |
|---|---|---|
| Fix 2 — explicit reply-path ACLs | — | done |
| Fix 1 — lowercase normalization (all identities) | — | done — `nats_connect()` identity_name path → _inbox.{name}; all uppercase _INBOX.X.> ACL entries dropped |
| Fix 3 — `make test-acl` in pre-push | — | skipped |
| Phase 3 — `request_reply_flows` schema + derived inbox grants | — | Done — #992 (request_reply_flows array added; manual _inbox.hub.> entries removed from all responder publish ACLs) |
| Alert: `permissions violation` in NATS logs | — | done |
| Alert: sustained `_stream_gen timeout` in hub logs | — | done |
| Synthetic round-trip health probe | — | skipped |
| Fix `gen-nkeys.sh` missing `clipool-worker` in key-gen block | — | done |
| Remove retired identities from `acl-matrix.json` | — | done |
| Write NATS secret rotation runbook | — | done — `docs/runbooks/nats-authconf-update.md` |
| Write post-deploy smoke test + canary rollout procedure | — | done — `docs/runbooks/deploy-smoke-canary.md` |
| Post-incident verification: zero `permissions violation` for 24h | — | open |

---

## Latent bugs identified (independent of this incident)

- **`gen-nkeys.sh` hardcoded key-generation block skips `clipool-worker`**
  (lines 597–620): `clipool-worker.seed` is never created on a fresh run without
  `--regenerate`. The identity is covered by `render_auth_conf` via `IDENTITIES[]`
  but not by the explicit `generate_nkey` calls. Verify and fix before next
  fresh deploy.

- **Retired identities** (`tts-adapter`, `stt-adapter`) remain in
  `acl-matrix.json` with active ACL entries and no documented removal process.
  They generate seeds and auth entries on every regeneration. Schedule removal.

---

## Monitoring gaps

The 2h44m detection lag is unacceptable. No fix above addresses runtime alerting:

- **Add alert on `permissions violation` log lines** in the NATS container —
  would have flagged this at 16:11, ~3h earlier
- **Add alert on sustained `_stream_gen timeout`** in hub logs
- **Add synthetic round-trip health probe** (hub → clipool → hub) to the
  existing health endpoint; current `/health` returns hub liveness but not
  NATS round-trip correctness

---

## Missing runbook: NATS secret rotation

No documented procedure for the full sequence. Until written:

1. `gen-nkeys.sh --regenerate` (has auto-restore on failure)
2. `make quadlet-secrets-install`
3. `make factory-nats reload`
4. Verify each service reconnects: `make factory-hub logs`, `make factory-clipool logs`, etc.

---

## Related

- `deploy/nats/acl-matrix.json` — SSoT for all NATS ACLs
- `deploy/nats/gen-nkeys.sh` — generates `auth.conf` from the matrix
- ADR-062 — NATS ACL inbox case normalization and explicit reply-paths
- ADR-051 — per-identity inbox prefix invariant
- ADR-045 — roxabi-nats SDK
- Issue #949 — clipool publish ACL narrowing (removed _INBOX.> wildcard)

---

## 5-Why Deep-Dive (4-domain analysis)

Four independent analyses were run after the initial postmortem: architect, product, devops, and security. Each traced at least 5 causal levels. This section documents the chains and the consolidated root causes.

---

### Domain 1 — Architecture

#### Why-chain 1: `allow_responses` created an invisible, load-bearing dependency

1. After #949 removed the _INBOX.> wildcard from clipool's publish ACL, no explicit _inbox.hub.> entry replaced it. `allow_responses: true` became the **sole** authorization path for all reply traffic — not a supplement.
2. The assumption that `allow_responses` was a complete replacement was never challenged because its semantics (and case-sensitivity behavior) are not documented in `acl-matrix.json`, ADR-046, or ADR-051 with enough precision to make the gap visible.
3. `allow_responses: true` is hardcoded into `emit_user()` in `gen-nkeys.sh` for every identity. It does not appear in the ACL matrix at all — an operator reading `acl-matrix.json` has no signal that it exists, let alone that its case-sensitivity behavior diverges between publish-grant and subscribe-filter evaluation.
4. Pre-#949, _INBOX.> wildcards covered the full reply path. `allow_responses: true` was a secondary mechanism. When #949 narrowed publish ACLs it silently promoted `allow_responses` to load-bearing status — a transition that was not documented.
5. **Root:** `acl-matrix.json` is a per-identity permission list, not a communication topology graph. Cross-identity request-reply flows span two identities and cannot be expressed in the schema. Any change to one side of a request-reply pair requires updating the other side by convention only — there is no machine-enforced linkage.

#### Why-chain 2: Dual-case existed on satellites but not hub

1. Hub received inbox_prefix="_INBOX.hub" via `fd3250c4` as the first ADR-051 implementation for the hub identity.
2. Satellite workers already had dual-case ACL entries (_INBOX.X.> + _inbox.X.>) added reactively when nats-py lowercase behavior was first observed — treated as a satellite-specific quirk, not generalized to a rule.
3. When ADR-051 was implemented for hub, the precedent set by the satellite dual-case entries was not applied. ADR-051 documented the concern in "Negative consequences" but deferred it, and hub was the first identity to implement ADR-051 without inheriting the defensive dual-case pattern.
4. The deferred dual-case rule was not encoded as a lint check or schema constraint. ADR-051 stated the rule in prose; there was no automated enforcement.
5. **Root:** A temporary compatibility measure was treated as self-expiring and therefore not codified as a lint-enforced invariant. The transition was expected to be self-resolving, so no permanent guardrail was warranted. It was never resolved; new identities were added without the defensive pattern.

#### Why-chain 3: No invariant enforces inbox_prefix ↔ ACL subscribe parity

1. inbox_prefix="_INBOX.hub" is set in `hub_standalone.py`; _INBOX.hub.> is an entry in `acl-matrix.json`. These two artifacts live in different files, different layers (application code vs. deploy config), with no machine-checked relationship.
2. ADR-051 established `inbox_prefix` as a "required-by-convention parameter" but made no provision for verifying that the value used at connect time matches the ACL subscribe entry.
3. `gen-nkeys.sh --validate-supervisor` verifies credential wiring but does not validate that the identity's runtime inbox prefix matches any ACL entry.
4. Extracting the `inbox_prefix` value from Python source and cross-referencing it against JSON would require multi-language static analysis not present in any quality gate.
5. **Root:** The architectural decision to separate connect-time configuration (Python) from ACL configuration (JSON) is correct for separation of concerns, but no compensating control was designed to enforce consistency between the two. The invariant exists only in ADR prose.

#### Why-chain 4: `acl-matrix.json` is not the operational SSoT

1. The sequence to apply an ACL change to production is: edit JSON → `gen-nkeys.sh --regen-authconf` → `make quadlet-secrets-install` → `make factory-nats reload` — four distinct manual actions.
2. Podman secrets are immutable once created; updating requires delete + recreate, a separate command from reloading NATS. A `make factory-nats reload` executed before `make quadlet-secrets-install` silently reloads NATS against the old `auth.conf`.
3. The four steps are separate Makefile targets with independent dependencies. No atomic wrapper exists; no dependency chain prevents partial execution.
4. No post-apply verification step exists at any stage. There is no automatic check that NATS is now enforcing the new ACL, that each service reconnected, or that no permission violations appear in the first 30 seconds after reload.
5. **Root:** `acl-matrix.json` was designed as the SSoT for one-time provisioning (ADR-046). As ACL changes became frequent operational work (#949, ADR-051 rollout, etc.), the multi-step manual pipeline became a routine hazard rather than a rare provisioning activity. Every ACL change is an opportunity to leave the system silently inconsistent.

#### Why-chain 5: Cross-repo coupling and latent gen-nkeys.sh bug

**5A — Hub-spoke coupling (Fix 2):** Fix 2 requires each responder to name _inbox.hub.> in its publish ACL. This encodes a cross-identity dependency that looks structurally identical to a namespace grant — no schema field identifies it as a topology dependency. A hub identity rename or second requester requires manual updates to all responder ACLs with no tooling to find them.

**5B — voicecli coordination (Fix 1):** `voice-tts` and `voice-stt` identities live in `acl-matrix.json` (roxabi-factory repo) but their connect-site configuration lives in voicecli (separate repo, separate release cycle). Deploying Fix 1 in lyra while voicecli still uses uppercase produces an identical outage for voice-tts and voice-stt. The lyra `acl-matrix.json` can be updated and `auth.conf` regenerated with no CI gate preventing partial rollout.

**5C — gen-nkeys.sh identity list desync:** The hardcoded key-generation block (lines 597–620) does not include `clipool-worker`. The script maintains two independent identity lists — the hardcoded shell block and `IDENTITIES[]` from the JSON — with no cross-validation. On a fresh provision without `--regenerate`, `clipool-worker.seed` is never created; `render_auth_conf` then fails hard (missing pubkey). This is a disaster-recovery time-bomb, not a silent bug, but there is no CI check preventing the two lists from diverging further.

#### Architectural root causes (consolidated)

| Gap | Next variant that ships undetected |
|---|---|
| No topology model in ACL schema | Any new request-reply flow where the responder's publish ACL is not manually updated to name the requester's inbox |
| No invariant enforcement for inbox_prefix ↔ ACL subscribe parity | Any new identity whose connect-site prefix doesn't match its ACL entry |
| `allow_responses` semantically invisible | Any ACL narrowing that removes an explicit publish grant while leaving `allow_responses` as the sole mechanism |
| 4-step manual pipeline with no atomicity | Any ACL change where an operator stops after step 2 |
| Cross-repo deploy coupling with no gate | Any Fix 1 partial rollout; any future cross-satellite ACL change |
| Dual hardcoded identity list in gen-nkeys.sh | Any new identity added to JSON but not the shell block, on fresh provisioning |
| No runtime detection of ACL violations | Any future ACL gap where broken communication is not surfaced by user-visible symptoms for hours |

---

### Domain 2 — Product / Process

#### Why-chain 1: Users got silent failure instead of graceful degradation

1. The `_stream_gen` timeout path emitted a `❌` internally but no fallback handler sent a user-facing "service temporarily unavailable" message.
2. No graceful degradation contract exists in the LLM response path. The failure branch was never specified.
3. Graceful degradation was never a product requirement. No acceptance criterion covered "what happens when the responder is unreachable."
4. The system was designed assuming NATS round-trips succeed; failure modes were implicitly invisible to the operator who was also the user.
5. **Root:** No failure-mode product requirement was ever written. The implicit assumption was that infrastructure failures are self-evident to users. They are not.

#### Why-chain 2: No user notification for 3h15m

1. Nobody sent one.
2. No defined owner for user communication during an incident.
3. No incident response process — no runbook, no on-call trigger criteria, no communication obligation.
4. No definition of what constitutes a user-visible incident vs. an internal ops issue. Without a definition there is no trigger.
5. **Root:** factory's user base has been treated as a side effect of development rather than a stakeholder population with expectations. No SLO or SLA exists, which means there is no obligation to communicate failures.

#### Why-chain 3: No canary deployment

1. All four channels received the new image simultaneously.
2. No canary or staged rollout procedure was documented or automated.
3. No deployment strategy beyond "push image, restart units" was ever specified.
4. Staged rollout was implicitly deferred as "something to add when we have more users."
5. **Root:** Deployment risk is not modeled. Every deploy is treated identically regardless of what changed (config, ACLs, connection behavior, core logic). Blast radius scales with channel count, never with risk.

#### Why-chain 4: No post-deploy smoke test

1. 1h37m elapsed between bad deploy and first logged violation with no automated detection.
2. No post-deploy validation step exists. `/health` validates liveness, not user-path correctness.
3. The smoke test was not built because no deploy runbook entry for "verify deployment success" exists.
4. "Deployed" implicitly meant "container restarted without error," not "end-to-end user path validated."
5. **Root:** Deployment success was never defined from the user's perspective. It was defined from the operator's perspective (container up, no crash on start). These are different claims.

#### Why-chain 5: No defined incident response process

1. Detection was manual, response was ad hoc, communication did not happen.
2. No incident response playbook — no criteria for declaring an incident, no owner, no comms template, no post-incident review obligation.
3. Incident response was always "something to add later once the system is more mature."
4. "More mature" was never defined. No milestone, no metric, no user count threshold with an exit condition.
5. **Root:** No explicit service maturity graduation. The project is operated as a development system while functioning as a production service. The gap between those two operational modes was never named.

**User trust impact:** Silent failure is specifically damaging because it removes the user's ability to make decisions (wait, retry, use something else). An acknowledged outage with a status message preserves trust by demonstrating operational awareness. A silent outage damages trust twice: once during the failure, and again when the user realizes no one communicated with them. The compounding effect is severe at any scale because the behaviors are structural, not circumstantial.

---

### Domain 3 — DevOps / CI-CD

#### Why-chain 1: Bad ACL config shipped without a CI gate

1. No CI job validates NATS ACL correctness before merge.
2. `acl-matrix.json` changes are treated as infrastructure config, not code, and the quality gate model has not been extended to cover authorization config.
3. No tool computes the effective permission set across all identities and cross-checks subscribe/publish symmetry — the only feedback loop for ACL correctness is a production deployment.
4. NATS ACL semantics (including `allow_responses`, dual-case inbox patterns, wildcard precedence) are not encoded anywhere in the repo as executable logic. The authoritative source is the NATS server runtime.
5. **Root:** The deployment pipeline has no boundary between "config that is validated" and "config that is trusted on commit." ACL changes enter production on the same path as source code, but without the test coverage that source code requires.

#### Why-chain 2: 2h44m detection lag

1. No alert fired on NATS `permissions violation` log events or sustained `_stream_gen timeout` in hub logs.
2. NATS server logs are not wired to an alerting backend. Log collection exists for human inspection only.
3. Alert thresholds were never defined for NATS-layer errors because NATS was introduced as a transport without a corresponding observability spec.
4. The pre-NATS failure model was: transport errors surfaced immediately as adapter disconnects. With NATS, "connected but ACL-blocked" is a new failure class — adapters stay connected, messages silently fail server-side, the symptom is silence.
5. **Root:** Observability investment tracked the old architecture's failure modes. The NATS hub-spoke migration introduced a new class of silent failures (ACL blocks, inbox mismatches) that produce no adapter-visible errors and evade every existing alert.

#### Why-chain 3: NATS secret rotation is 4-step manual with no runbook

1. The rotation procedure was never written down because NATS secret rotation was not considered during initial deployment design.
2. NKeys are long-lived by default; rotation was assumed infrequent with no scheduling forcing function.
3. The 4-step process was discovered ad hoc during the incident; there is no post-incident gate mandating runbook creation.
4. No post-incident action-item tracking requires runbook creation as a resolution step — incident response ends at "service restored."
5. **Root:** No operational readiness requirement before a component ships to production. NATS shipped without answering: how do we rotate credentials? How do we recover from a misconfiguration?

#### Why-chain 4: gen-nkeys.sh skips clipool-worker

1. The script has a hardcoded identity list that was not updated when `clipool-worker` was introduced.
2. No single source of truth drives both `acl-matrix.json` entries and `gen-nkeys.sh` identity enumeration — they are maintained independently.
3. The script was written early when the identity set was small and stable; maintainability (driven from SSoT) was deferred for shipping speed.
4. No CI check diffs the identities in `acl-matrix.json` against the identities in `gen-nkeys.sh`.
5. **Root:** Two artifacts with a required 1:1 relationship between their identity sets are maintained independently. The burden of consistency is entirely on the developer.

#### Why-chain 5: `/health` does not validate NATS round-trip correctness

1. `/health` is a liveness probe (process up, event loop running) — the minimal bar for container orchestration.
2. Readiness (can serve traffic) was conflated with liveness (process is alive). In a hub-spoke architecture, hub process alive and NATS subscription broken are decoupled states, as this incident demonstrated.
3. NATS round-trip validation requires an async publish+subscribe cycle, which is more complex than a flag check and was not scoped when the endpoint was first built.
4. The health endpoint was designed before NATS became the critical path and was not revisited.
5. **Root:** Health check design is not coupled to the system's dependency graph. As dependencies are promoted to critical path, the health check is not updated. The result is false confidence — green health, full outage.

---

### Domain 4 — Security

#### Why-chain 1: ACL model relies on undocumented `allow_responses` case behavior

1. `allow_responses: true` is emitted unconditionally for every identity in `emit_user()`, including identities that never participate in request-reply. After #949, it became the sole authorization path for reply traffic.
2. NATS documentation does not specify case-folding behavior for `allow_responses`. The team observed it working and treated observation as correctness. No test verified the dynamic grant held under all subject-casing variants.
3. ACL narrowing in #949 was treated as authorization tightening (good security hygiene), not as a change that altered the trust model for reply-path authorization. The implicit promotion of `allow_responses` to sole guard was not documented.
4. The ACL authoring process has no rule that "every identity with a reply-path must have an explicit publish ACL entry for that path." The schema allows implicit grants by design.
5. **Root:** The ACL design conflates "safe default" (`allow_responses` as convenience) with "authorization control" (explicit publish grants). When the wildcard was removed, the dynamic grant silently became the sole authorization path, and its case-sensitivity was an untested assumption about a Go runtime behavior not covered by the protocol spec.

#### Why-chain 2: Retired identities retain active ACL entries and credential generation

1. `tts-adapter` and `sst-adapter` remain in `acl-matrix.json` with full ACL entries. `gen-nkeys.sh` calls `generate_nkey` for both on every `--regenerate` run.
2. The `acl-matrix.json` schema has no `status` or `active` field. `render_auth_conf` iterates all keys in the JSON — all become live `users[]` blocks in `auth.conf`. "Archived identity with inactive credentials" is not expressible in the tooling.
3. The schema was designed for active identities and never extended to accommodate lifecycle transitions.
4. The `#690` cutover removed the running services but left ACL and credential management untouched, reasoning that "the seeds are harmless if never deployed." This is correct only if the seeds never escape the host.
5. **Root:** Identity lifecycle (add, retire, remove) is not a defined process. The ACL matrix grows monotonically and becomes progressively less trustworthy as a source of truth. Retired identities are a real attack surface: their seeds exist and their ACL grants are live.

**Threat surface for retired identities:** A leaked `tts-adapter.seed` or `sst-adapter.seed` allows an attacker to subscribe to all voice request payloads, publish false heartbeats (masquerade as the worker), and via `allow_responses`, publish to any inbox from which a message is received — including hub's inbox.

#### Why-chain 3: No audit trail or rotation policy for NATS credentials

1. No documented rotation schedule or policy. No SLA on when rotation must run, no triggering conditions.
2. Seed files have filesystem mtime but that is not an integrity-protected audit record. No credential database, no rotation log.
3. Security audit logging was not a requirement when `gen-nkeys.sh` was written.
4. The NATS audit stream (`factory.audit.>`) covers application-level message events, not infrastructure credential operations.
5. **Root:** The audit stream was designed top-down from application requirements, not bottom-up from security requirements. Credential management is a shell script invoked by a human operator, outside any audit framework.

#### Why-chain 4: Misconfigured ACL can ship without authorization correctness verification in CI

1. No CI job validates ACL changes before merge. The `quality_gates` in `.dev/stack.yml` cover import layers, file length, and test uniqueness — not authorization config.
2. `gen-nkeys.sh` runs `nats-server -t` for syntax validation only when the binary is present, which is not enforced in CI and validates syntax, not semantics.
3. The first end-to-end test of ACL authorization correctness is when real services connect in production.
4. Building a semantic verifier (spin up NATS, connect as each identity, assert allowed/denied subjects) was identified as Fix 3 and rated P0 in the postmortem. It does not yet exist.
5. **Root:** Authorization correctness depends on the joint behavior of three systems: the Python connect-site parameter, the `acl-matrix.json` subscribe entry, and the NATS server's runtime `allow_responses` semantics. No single document or review surface shows all three together; no CI job tests the combination.

#### Why-chain 5: Authorization failures not exposed to ops before user impact

1. 2h44m detection lag. NATS logged `permissions violation` at 16:11 — 37 min after the bad deploy — but no alert was configured.
2. NATS HTTP monitoring is **explicitly disabled** in `nats.conf` ("Disabled until Slice A3 is implemented"). Without the monitoring port, log-line alerting is the only viable mechanism — and it was not implemented.
3. The monitoring endpoint was disabled while waiting for Slice A3, creating a sequencing dependency that left no machine-observable signal for authorization events in the interim.
4. Hub `/health` conflates liveness with readiness. A NATS connection can be established and authenticated while publish/subscribe operations fail with permissions violations — the health endpoint reports "healthy."
5. **Root:** An attacker with a compromised identity publishing false heartbeats or subscribing to message streams would produce no alert under the current observability model. Availability failures and active exploitation are equally invisible.

#### Blast radius of NATS credential compromise

`allow_responses: true` on every identity is a universal amplifier. Any worker identity that receives a message with a reply-to field can subsequently publish to that reply-to subject. Hub always sends requests with reply-to inboxes. Therefore, any compromised worker seed grants implicit write access to hub's inbox, enabling response injection into the hub's message flow regardless of the worker's explicit publish ACL.

| Compromised identity | Impact |
|---|---|
| `hub` | Full message bus control. Subscribe to all inbound messages (full user PII). Inject arbitrary outbound messages on all channels. Suppress or inject audit entries. |
| `clipool-worker` | Intercept all LLM dispatch commands (full prompt + context). Inject arbitrary LLM responses into hub's reply stream — affecting all 4 channels simultaneously. |
| `voice-tts` / `voice-stt` | Intercept all voice requests. Inject false TTS/STT responses. Publish false heartbeats (suppress worker availability). |
| `image-worker` | Intercept all image generation requests (prompt content). Inject false image responses. |
| `telegram-adapter` | Read all Telegram inbound messages. Publish to `factory.inbound.telegram.>` — inject messages into hub as if from Telegram. |
| `tts-adapter` (retired) | Same capability as `voice-tts` — but identity should have no running service and has no monitoring. Compromise is maximally stealthy. |
| `llm-worker` | Intercept all LLM requests (full user prompt + context). Inject false LLM responses. |

Highest-risk scenario: `clipool-worker` seed compromise → intercept all user prompts + inject LLM responses across all channels. Under current observability: service stays "alive," liveness probe stays green, time-to-detection is indeterminate (2h44m is the observed baseline for an availability failure; content injection with the service running would be slower to detect).

---

### Consolidated root causes (3 structural)

| # | Root cause | Explains |
|---|---|---|
| **1** | ACL matrix is a permission list, not a topology graph | Every new request-reply flow is a silent breakage waiting to happen — no machine can find the missing link between a responder's publish ACL and a requester's subscribe entry |
| **2** | No CI gate on authorization config correctness | First end-to-end test of ACL semantics is always a production deploy. `allow_responses` case-sensitivity, inbox_prefix ↔ ACL parity, and cross-identity round-trips are all untested until production |
| **3** | Service operated with development-project posture | Silent failures, no comms owner, no SLO, no canary, no smoke test, no credential lifecycle — structural, not circumstantial. Every specific gap is a consequence of the project never formally graduating from personal tool to production service |

---

### The single structural error behind root cause 1

**Authorization was modeled at the implementation layer instead of the semantic layer.**

`acl-matrix.json` encodes: *"what subjects can identity X pub/sub to?"* — strings, cases, wildcards.
The invariant that actually needs to be maintained is: *"hub uses request-reply to clipool"* — a communication topology fact about pairs of actors.

These two representations describe the same truth, but they are not equivalent under change:
- Topology → per-identity ACLs can be **derived mechanically**
- Per-identity ACLs → topology **cannot be reconstructed** without knowing which subjects correspond to which roles at runtime

Because the wrong level was chosen as the SSoT, every implementation detail — inbox case (_INBOX vs _inbox), wildcard narrowing, `allow_responses` semantics — became a load-bearing configuration surface with no machine-enforced relationship to the semantic invariant it was supposed to implement.

Every specific failure in this incident traces back to this:

| Specific failure | Trace to root |
|---|---|
| `allow_responses` became invisible load-bearing | It bridges the gap between the subject-centric model and the actual topology — magic required because the schema cannot express "clipool replies to hub" |
| Dual-case maintenance burden | Case is an implementation artifact; the topology (`clipool→hub flow`) is case-agnostic. The wrong abstraction level forces case tracking |
| `inbox_prefix` ↔ ACL subscribe parity unenforced | _INBOX.hub in Python and _INBOX.hub.> in JSON are two representations of the same semantic fact — but the schema has no concept of "this is the hub's inbox", so there is nothing to enforce parity against |
| #949 narrowing silently broke clipool→hub | Changing one identity's publish ACL had no machine-detectable effect on the cross-identity flow it was part of — because cross-identity flows do not exist as a concept in the schema |
| Fix 2 introduces responder-encodes-requester coupling | Correct fix, but it encodes topology into per-identity subject lists — still the wrong layer, just explicit now instead of implicit |

#### The durable fix: topology as a first-class schema concept

Add a section to `acl-matrix.json` that names flows explicitly:

```json
{
  "request_reply_flows": [
    { "requester": "hub", "responder": "clipool-worker", "subject": "factory.jobs.claude" },
    { "requester": "hub", "responder": "voice-tts",      "subject": "factory.voice.tts.cmd" }
  ],

  "identities": {
    "hub":            { "subscribe": ["factory.system.>"], "publish": ["factory.jobs.claude"] },
    "clipool-worker": { "subscribe": ["factory.jobs.claude"], "publish": ["factory.clipool.heartbeat"] }
  }
}
```

`gen-nkeys.sh` (or a replacement `gen-acl.py`) **derives** the inbox entries automatically from the flow declarations:

```
for each flow (requester=hub, responder=clipool-worker):
    hub.subscribe     += ["_inbox.hub.>"]   ← requester always needs this
    clipool.publish   += ["_inbox.hub.>"]   ← responder needs to reach requester's inbox
    allow_responses    = false              ← no longer needed; explicit grant covers it
```

ACL entries become **generated output**, not manually maintained input.

What this buys:

| Old world | New world |
|---|---|
| Rename hub's inbox prefix → manually hunt every responder | Rename it in one place → generator updates all responder publish entries |
| Add a new flow → remember to update both sides | Declare the flow → both sides generated |
| `allow_responses` is invisible load-bearing magic | Explicit _inbox.hub.> entry; `allow_responses` is defence-in-depth or removed |
| CI cannot verify cross-identity correctness | Generator has the topology → can emit a test matrix asserting hub→clipool→hub round-trip |
| Case mismatch is possible | Generator controls the case → single source, always consistent |

The inbox case-mismatch bug that caused this incident is **structurally impossible** in this model: there is no second place to write the string incorrectly. The generator formats _inbox.{requester}.> once and writes it into both the requester's subscribe ACL and every responder's publish ACL. The Python connect call reads from the same source:

```python
# generator uses:   f"_inbox.{identity_name}.>"
# connect call uses: nats_connect(nats_url, identity_name="hub")
# → inbox_prefix derived from identity_name by nats_connect, same string, structural parity
```

This is the correct long-term target for `acl-matrix.json`. Fix 2 (explicit _inbox.hub.> in responder publish ACLs) is the right immediate step and moves in this direction — it makes the topology visible. The topology-first schema makes it machine-enforced.

---

### Prioritized action map (from 5-why analysis)

Three phases: immediate mitigation → normalization (coordinated) → topology-first migration (durable fix for root cause 1).

---

#### Phase 1 — Immediate (P0, unblocked)

| Action | Rationale |
|---|---|
| Fix 2: explicit _inbox.hub.> in all responder publish ACLs in `acl-matrix.json` | Removes reliance on `allow_responses` as sole auth path; seeds Phase 3 topology migration |
| Remove `allow_responses: true` from identities that never do request-reply | Shrinks blast radius; makes which identities participate in request-reply visible |
| Alert on `permissions violation` in NATS container logs | Would have caught this incident at 16:11 instead of 18:55 |
| Alert on sustained `_stream_gen timeout` in hub logs | Hub-side signal for any future ACL or transport break |
| Synthetic round-trip health probe (hub → clipool → hub) post-deploy | Catches a broken reply-path before real user traffic hits it |
| Retire `tts-adapter`/`sst-adapter`: remove from `acl-matrix.json`, revoke seeds, remove `generate_nkey` calls | Eliminates active attack surface for credentials with no running service |

---

#### Phase 2 — Coordinated (P0, blocked on voicecli)

| Action | Rationale |
|---|---|
| Fix 1: lowercase _inbox prefix — all identities | `nats_connect()` identity_name path normalized; all uppercase _INBOX.X.> ACL entries dropped; voicecli confirmed lowercase (2026-04-28) |
| Fix 3: `make test-acl` in pre-push — real NATS, real round-trips, denied-subject assertions, `allow_responses`-removed fixture | CI gate that would have blocked this incident from shipping; run alongside `import_layers` |

---

#### Phase 3 — Topology-first migration (P1, unblocked after Phase 1)

Root cause 1 fix: `acl-matrix.json` currently models authorization at the subject level. Topology must become a first-class schema concept so the generator enforces cross-identity consistency — making the case-mismatch and missing-grant classes of bug structurally impossible.

| Action | Status | Rationale |
|---|---|---|
| Add `request_reply_flows` section to `acl-matrix.json`; declare all existing hub→responder flows | Done — #992 | Makes topology visible and machine-readable; prerequisite for generator derivation |
| Remove manual _inbox.hub.> entries from all responder publish ACLs (derived from flows) | Done — #992 | Inbox grants are now declared via request_reply_flows, not per-identity manual entries |
| Build generator that derives _inbox.{requester}.> subscribe + publish entries from flow declarations | open | Inbox ACLs become generated output; no second place to write the string incorrectly |
| Update `nats_connect()` in roxabi-nats: `identity_name` → f"_inbox.{identity_name}.>" so connect-site and generator use the same string | open | Closes the Python ↔ JSON parity gap that caused this incident |
| Drop `allow_responses: true` from all identities once explicit flow grants cover all reply paths | open | Removes invisible magic; may be kept as defence-in-depth only |
| Drive `gen-nkeys.sh` key-generation from `IDENTITIES[]` iteration — kill hardcoded identity list | open | Single identity SSoT; prevents clipool-worker-class desync on fresh provisioning |
| Extend Fix 3 test suite: assert that removing a flow declaration removes the derived ACL grant | open | Documents and tests topology derivation as load-bearing |
| `acl-matrix.json` schema: add `status: active\|retired`, CI rejects any retired entry without expiry | open | Identity lifecycle becomes a defined process; prevents monotonic ACL growth |

---

#### Other P1 / P2

| Priority | Action |
|---|---|
| P1 | `/health/ready` split: liveness vs. NATS round-trip readiness; container healthcheck → `/health/ready` |
| P1 | Incident response process: owner, trigger criteria, user notification template, post-incident gate (runbook must exist before incident is closed) |
| P1 | Canary rollout procedure: 1 channel before full blast; post-canary smoke test before promoting |
| P1 | `make nats-rotate-secrets` atomic target wrapping all 4 steps with pre/post validation |
| P1 | NATS secret rotation runbook |
| P2 | Enable NATS HTTP monitoring on `127.0.0.1`; alert on error counters |
| P2 | Credential rotation policy (90-day max age + event-triggered) + append-only rotation log |
| P2 | Graceful degradation spec for LLM response path: user-facing message on `_stream_gen` timeout |
