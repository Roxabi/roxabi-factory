# AGENTS.md — deploy/

## Scope

Container deployment artifacts for factory production (Podman Quadlet reference implementation).
Subdirs: `quadlet/` | `nats/` | `scripts/` | `lib/` | `factory-gh/` | `systemd/` | `omp/` (omp runtime config → factory LiteLLM, #1811 — see `omp/README.md`) | `omp-base/` (omp binary carrier image → `ghcr.io/roxabi/factory-omp-base`, #1810 — see `omp-base/README.md`)

¬docker, ¬docker-compose for prod. Runtime stack: **Podman 5.x (native on Ubuntu 26.04 LTS)
+ Quadlet generators + systemd user units**.

factory is the **reference implementation** for the Roxabi Quadlet pattern.
Cross-repo adoption checklist → `docs/runbooks/container-publishing.md § Cross-repo adoption`.

---

## Unit naming convention

Authoritative unit manifest: `deploy/quadlet.toml`.
Pattern: `factory-<component>.container` → `ContainerName=factory-<component>` → `factory-<component>.service`.
Telegram and discord units are rendered from `.container.tmpl` at deploy time (bot-token injection).
Network: all units attach to `roxabi.network` (defined in `quadlet/roxabi.network`).

---

## NATS server

`factory-nats.container` is part of the deploy bundle — the sole NATS server on the production hub host.
The host `nats.service` is retired (big-bang consolidation). Hub and adapters declare
`After=factory-nats.service` / `Requires=factory-nats.service` so systemd boots NATS first.

NATS config: `nats/nats-container.conf` (bind-mounted read-only).
Auth credentials: `auth.conf` delivered as an **inline bind mount** (`Volume=%h/.roxabi/factory/nkeys/auth.conf:/etc/nats/nkeys/auth.conf:ro,z`) — NOT a Podman secret (→ `docs/architecture/deployment.md`). Private NKey seeds remain `type=mount` Podman secrets (→ `docs/architecture/deployment.md` § Key invariants).
NATS version: pinned by digest in `factory-nats.container` — ¬autoupdate, bump manually.

---

## Image lifecycle (CI → GHCR → Quadlet)

Full pattern → `docs/runbooks/container-publishing.md`

| Phase | Image tag | `AutoUpdate=` |
|---|---|---|
| Staging validation | `ghcr.io/roxabi/factory:staging` | `registry` |
| Post-release prod pin | `ghcr.io/roxabi/factory:X.Y.Z` | none (edit manually) |

CI: push to `staging` → `publish.yml` → `ghcr.io/roxabi/factory:staging`.
Prod pull: `podman-auto-update.timer` fires every 5 min, checks digest, restarts on change.
Rollback: edit `Image=` to previous semver tag → `systemctl --user daemon-reload` → restart.

**Schema-floor bumps** (wire-protocol change): stop auto-update timer, restart hub + telegram
+ discord atomically, re-enable timer. `factory-clipool` excluded (not a RenderEvent receiver).

---

## llmCLI cloud gateway

The M₁ always-on cloud LLM gateway — **`factory-litellm`** (LiteLLM proxy :18091), **`llmcli-xai-forwarder`** (:18645, xAI/Grok OAuth relay), **`llmcli-fw-forwarder`** (:18646, Fireworks) — is deployed from this repo (`deploy/quadlet/{factory-litellm,llmcli-xai-forwarder,llmcli-fw-forwarder}.container` + `[component.litellm-proxy|xai-forwarder|fw-forwarder]`, `host_roles=["factory-hub"]`). The proxy container was renamed `llmcli`→`factory-litellm` (factory-`<component>` convention) to avoid a filename collision with llmCLI's own M₂ `llmcli.container` — `cluster_plan` matches by filename, so a shared name leaked llmCLI's M₂ proxy into M₁'s install-plan. Rationale: HA requires M₁ to answer LLM 24/7 (LiteLLM→cloud), so the always-on gateway belongs with the always-on hub.

**Boundary** — factory owns *deployment* (host placement, converge lifecycle, autoupdate wiring, secret wiring); Roxabi/llmCLI owns *code + image* (`ghcr.io/roxabi/llmcli`, built + published by its CI) and the **M₂ local GPU worker** (`llmcli-nats-worker` + engines, `host_roles=["llm-worker"]`, unchanged — deferred). The units are byte-vendored from llmCLI; M₁ gateway images track `:staging` via `io.containers.autoupdate=registry`.

**Carve-outs from § Hardening invariants** (intentional — do not "normalize"):
- `UserNS=keep-id:uid=1502,gid=1502` — the llmCLI image runs as uid 1502 (distinct from factory 1500 / voicecli 1501). The `NoNewPrivileges`/`ReadOnly`/`DropCapability=all` trio is present. <!-- drift-ignore -->
- The proxy master key is read from `~/.roxabi/llmcli/env/proxy.env` (grandfathered llmCLI data dir, `EnvironmentFile=`), **not** a `type=mount` secret — hence `required_secrets=[]`. Its value must equal `factory-litellm-key` (the bearer `factory-omp` sends); they are the same token today. Hardening to a `type=mount` `LLMCLI_API_KEY_FILE` secret needs an image change (follow-up).
- xAI OAuth credentials live at `~/.roxabi/llmcli/credentials/` (rw bind-mount, per-host grant family — never a Podman secret, never Syncthing-synced, never copied between hosts).

**Consumers** (ports are the contract; **forwarder** container-names too — the proxy reaches them by DNS): `factory-omp` → :18091/v1; Claude Code aliases + xai-research skill → host loopback :18091 / :18645. The proxy is consumed only via PublishPort/tailnet :18091, so its own container name is free to be `factory-litellm`; the forwarder names (`llmcli-xai-forwarder`/`llmcli-fw-forwarder`) must NOT change.

### llmCLI cloud-gateway image

`ghcr.io/roxabi/llmcli:staging` with `Label=io.containers.autoupdate=registry` in all three
`deploy/quadlet/{factory-litellm,llmcli-xai-forwarder,llmcli-fw-forwarder}.container` files.
`podman-auto-update.timer` pulls new staging builds and restarts the units (same as voiceCLI).
Manual catch-up: `podman auto-update && systemctl --user restart factory-litellm llmcli-xai-forwarder llmcli-fw-forwarder`.

---

## Provisioning

`provision.sh` — production host post-install script. Run once per machine, or after a full wipe.

```bash
curl -fsSL https://raw.githubusercontent.com/Roxabi/roxabi-factory/staging/deploy/provision.sh | bash
```

Who runs it: operator — ¬automated, ¬CI. Idempotent for most steps.
`quadlet-install-verify.sh` — smoke-check that all Quadlet units loaded cleanly after
`systemctl --user daemon-reload`.

Secrets bootstrap: `make quadlet-secrets-install` (installs Podman secrets from host key files).
BotStore bootstrap: `make quadlet-install` runs `factory bot init` as its first step (seeds
`~/.roxabi/factory/config.db` from `config.toml` — idempotent, skip-existing). Required since #1416.
Operator scripts: `scripts/rotate-claude-oauth.sh`, `scripts/rotate-gh-key.sh` — run manually
on rotation events.

---

## Atomic deploy — `make converge`

`make converge` (→ `deploy/converge.sh`) is the **atomic, idempotent, change-gated** local
deploy verb on the production host. It reconciles the running system with the desired state declared in
`staging` (factory + optionally voiceCLI) without operator intervention.

### Properties

| Property | Mechanism |
|---|---|
| **Change-gated** | Computes a 6-field convergence fingerprint (see below). If current state matches the last recorded stamp (`~/.roxabi/factory/.converge-stamp`), `make converge` exits immediately with `Already converged — nothing to do.` |
| **Idempotent** | Running `make converge` twice on an unchanged tree is a no-op. Individual steps (git pull, `make quadlet-install`, `factory-acl genkeys`, secret install, restarts) are each idempotent or guarded. |
| **Atomic** | A `flock` file lock (`/run/user/<uid>/factory-deploy.lock`) prevents concurrent converges. If the lock is held, the second invocation exits 0 silently. The full sequence (pull → install → regen auth → secrets → restart NATS → restart clients) is executed as a single critical section. |

### Convergence fingerprint

Implemented in `deploy/lib/deploy-common.sh` (`compute_convergence_state`, `_classify_drift`). Written to `~/.roxabi/factory/.converge-stamp` at the end of every successful converge.

**Format** — six colon-separated fields (image digest fields store **bare hex**, no `sha256:` prefix — the prefix would break colon parsing):

```
<git-head>:<units-sha256>:<authconf-sha256>:<voicecli-head>:<staging-svc-hex>:<staging-hex>
```

| Field | Source | Drift kind |
|---|---|---|
| 0 `git-head` | `git rev-parse HEAD` in `~/projects/roxabi-factory` | code-only (structural if another field also differs) — see Drift classification below |
| 1 `units-sha256` | `sha256sum` of sorted `~/.config/containers/systemd/factory*` unit files | structural |
| 2 `authconf-sha256` | `sha256sum` of `~/.roxabi/factory/nkeys/auth.conf` | auth |
| 3 `voicecli-head` | `git rev-parse HEAD` in `~/projects/voiceCLI` (or `none`) | structural |
| 4 `staging-svc-hex` | `factory_canonical_image_digest` for `ghcr.io/roxabi/factory:staging-svc` | structural |
| 5 `staging-hex` | `factory_canonical_image_digest` for `ghcr.io/roxabi/factory:staging` | structural |

**Tracked images** — single source of truth: `FACTORY_TRACKED_IMAGES` in `deploy-common.sh`. `factory-post-autoupdate.sh` iterates the same array (fields 4–5 of the stamp).

**Drift classification** (`_classify_drift`):

| Output | Meaning | Converge behavior |
|---|---|---|
| `none` | Stamp matches current state | Exit 0 immediately |
| `auth` | Only field 2 differs | Restart `factory-nats` only; clients reconnect via `allow_reconnect` |
| `code-only` | **Only** field 0 (`git-head`) differs — every artifact (units / auth / voice / image digests) is unchanged | `converge.sh` resolves it with a `git diff --name-only` over the commit range, in two tiers: **(1) inert** (`_code_change_is_inert`) — every changed path is non-runtime (`docs/`, `tests/`, `artifacts/`, `.github/`, `*.md`, …) → record the new stamp and **skip** (no restart); **(2) image-carried** (`_code_change_is_image_carried`) — every changed path is inert ∪ `src/`/`packages/`/`apps/dashboard/`/`brand/` (code that reaches the fleet only inside `factory:staging-svc`/`factory:staging`) → record the stamp and **skip the pre-image restart**; the restart is deferred to `factory-post-autoupdate`'s digest converge (fields 4–5) once publish.yml ships the image, killing the "2 full-fleet restarts per code merge" double-bounce. Requires post-autoupdate's **unconditional change-gated converge** (see timer table): when `podman-auto-update` wins the digest race, only the stale stamp re-arms the host steps (unit render, auth regen). Any host-carried path (`deploy/`, `tools/`, lockfiles, …), or an undecidable diff (missing commit) → fall through to **structural**. Fail-safe by construction: a mis-classification can only over-restart (the old behaviour), never silently under-restart. Requires content-addressed images (provenance/revision removed, lot 4a) so a docs-only commit does not move fields 4–5. |
| `structural` | Any of fields 1, 3, 4, 5 differ, **or** field 0 differs alongside another field, **or** no prior stamp | Full converge: NATS + all factory clients + voiceCLI |

Legacy 4-field stamps (pre-image-digest schema) are normalized to `:none:none` on fields 4–5 before comparison — one structural converge migrates them.

**Image digest detection** — single helper, two call sites:

- **`factory_canonical_image_digest`** (`deploy-common.sh`) — SSOT for fields 4–5 and post-autoupdate drift. Prefers the skopeo **index** digest when it appears in local `RepoDigests` (#1749); falls back to the first `RepoDigests` entry when skopeo is unavailable. <!-- drift-ignore -->
- **`factory-post-autoupdate.sh`** — compares `factory_remote_index_digest` (3 retries) to `factory_canonical_image_digest`. On drift: `podman pull`, then `make converge` (does **not** delete the stamp).

### Convergence sequence

1. **Change-gate** — skip if already converged.
2. **Pull** — `git pull origin staging` in `~/projects/roxabi-factory` (and `~/projects/voiceCLI` if present).
3. **Install cluster Quadlets** — `bash ~/projects/deploy.sh --prune` (role-aware SSOT: installs every host-matched unit across all managed repos from `hosts.toml` × `*/deploy/quadlet.toml`, generates host-override drop-ins, prunes orphan `.container` files). Runs under `converge.sh`'s `set -e` — **must succeed before step 4**. Template-only components (telegram/discord) are emitted as RENDER actions and rendered in step 4, not copied here.
4. **Render factory units** — `make quadlet-install NO_RESTART=1` (renders the telegram/discord `.container.tmpl` templates, copies to `~/.config/containers/systemd`, seeds BotStore). `NO_RESTART=1` **skips** `daemon-reload` — the reload lives in `deploy/quadlet-install-verify.sh`, run only in the non-`NO_RESTART` else-branch (see `docs/runbooks/quadlet-install.md § Deploy-time verification`).
4b. **Reload systemd** — `converge.sh` then runs `systemctl --user daemon-reload` explicitly + unconditionally, so systemd regenerates `.service` units from the Quadlet content installed in steps 3-4 before the restarts fire (else restarts relaunch from the stale generated unit).
5. **Regenerate auth.conf** — `factory-acl genkeys --regen-authconf` (renders `nkeys/` → `auth.conf`).
6. **Install secrets** — `make quadlet-secrets-install` (recreates Podman secrets from host key files; `factory-nats-auth` is no longer a secret — `auth.conf` is now an inline bind mount per `docs/architecture/deployment.md`).
7. **Restart NATS** — operator-path choices for targeted operations:
   - **Pure identity add** (`make nats-add-identity`): atomic write to `auth.conf` on host → `systemctl --user reload factory-nats` (fires `ExecReload=` → `podman kill --signal=HUP factory-nats`). Zero client restarts, zero dropped connections.
   - **ACL permission change** (`make nats-regen-authconf`): atomic write to `auth.conf` on host → `systemctl --user restart factory-nats` (required per #1390 — stale-subject-auth risk on ACL changes). Waits for `is-active`.
   Converge always **restarts** factory-nats on any drift (auth → factory-nats only; structural → factory-nats + clients) — it never reloads, because it cannot prove a change is a pure identity-add (#1390). Clients reconnect automatically via `allow_reconnect`.
8. **Restart factory clients** — the client set is **derived from `deploy/quadlet.toml` at runtime** (`converge.sh`: `quadlet_containers` minus `factory-nats`, floored by `quadlet_enabled_component_count` so a partial/empty manifest parse fails fast) — not a hand-maintained list, so it auto-tracks added components. Unconditional `systemctl restart` — also starts units that were inactive; any failure aborts the converge. Restarted only on **structural** drift. On **auth-only** drift, converge restarts factory-nats alone; clients reconnect via `allow_reconnect` without explicit restart.
9. **Restart voiceCLI** — `voicecli-tts`, `voicecli-stt` (if voiceCLI directory exists).
10. **Record stamp** — writes the new convergence fingerprint to `~/.roxabi/factory/.converge-stamp`.

### Trigger wiring

Three systemd user timers drive convergence **automatically**:

| Timer | Period | Service | Role |
|---|---|---|---|
| `podman-auto-update.timer` | `*:4/5` (5 min, offset +4 min — #1989) | `podman-auto-update.service` | Host-static apt unit (installed by `provision.sh`/`install.sh`); drop-in sets `OnCalendar=*:4/5`. Polls GHCR digests for containers labelled `io.containers.autoupdate=registry`; pulls and restarts on new digest. **Staggered off `*:0/5` (#1989):** podman-auto-update restarts containers directly, OUTSIDE `converge.sh`'s flock; at `*:0/5` it collided in-phase with `factory-quadlet-sync` and double-bounced each container ~1s apart (omp SIGKILL, hub WAL crash, telegram teardown). At `*:4/5` it trails `factory-post-autoupdate` (`*:2/5`), which has already converged the new image, so it usually no-ops and serves as a catch-up net for timer-miss runs (post-autoupdate skipped / flock held) rather than a primary restarter. |
| `factory-quadlet-sync.timer` | `*:0/5` (5 min) | `factory-quadlet-sync.service` | Pulls `origin/staging` for roxabi-factory; **if HEAD advanced, runs `make converge`** (`deploy/factory-quadlet-sync.sh`: fetch → if `HEAD == origin/staging` exit → `git pull --ff-only` → `make converge`). Converge's change-gate then classifies the drift: a `deploy/nats/acl-matrix.json`-only change is still structural (regen auth.conf + restart, #1848), but a **pure docs/tests/CI/`*.md` commit is `code-only`** and is **skipped** (stamp recorded, no restart) via `_code_change_is_inert`'s paths git-diff (lot 4b), and a **pure `src/`/`packages/`/`apps/dashboard/`/`brand/` commit** is likewise skipped via `_code_change_is_image_carried` — its restart arrives via `factory-post-autoupdate`'s digest converge once the new image is published (no pre-image bounce on the old image). Any host-carried runtime path, or an undecidable diff, falls through to structural (fail-safe). Cheap when there's no real drift: a no-op converge short-circuits on the `.converge-stamp` fingerprint. |
| `factory-post-autoupdate.timer` | `*:2/5` (5 min, offset +2 min — #1751) | `factory-post-autoupdate.service` | Polls GHCR index digests for `FACTORY_TRACKED_IMAGES` (`staging-svc`, `staging`). On drift: `podman pull`, then `make converge` (stamp fields 4–5 detect image drift — no stamp deletion). **Always ends with a change-gated `make converge` even when remote==local** — if `podman-auto-update` (`*:4/5`) observed a fresh digest first, it pulled and restarted containers WITHOUT converge's host steps (unit render, auth regen, stamp), and a drift-gated converge would be suppressed until the next commit; the stale stamp fields 4–5 make this tick self-heal (no-op `drift=none` <1s otherwise). Load-bearing for the image-carried skip. Steady-state cost (accepted trade-off, review #2144): the no-op converge re-queries the remote digests (~2 extra skopeo round-trips per tick on top of main()'s own poll) + one `converge_skip drift=none` op_log line per tick (~288/day) — do NOT "optimize" by passing main()'s digests into converge: the gate exists precisely to catch what main() did NOT see (podman pulled behind its back). Fires 2 min after `factory-quadlet-sync` so converge short-circuits when quadlet-sync already converged the same HEAD. |

`podman-auto-update` handles **image pulls** (CI-driven, registry-labelled containers); fires at `*:4/5` — staggered off the `*:0/5` converge slot (#1989) so its direct restart never collides with `factory-quadlet-sync`.
`factory-quadlet-sync` handles **any staging HEAD change** (code, units, ACL, docs — anything merged), running the full `make converge`; fires at `*:0/5`.
`factory-post-autoupdate` handles **GHCR image-digest drift** for the two factory runtime tags; fires at `*:2/5` (2 min later) so converge short-circuits when `factory-quadlet-sync` already converged the same git HEAD. Trigger sources differ — quadlet-sync on git-HEAD change, post-autoupdate on image-digest change — but both invoke `make converge` and share the same 6-field stamp.
All three fire on a staggered `*:0/5 → *:2/5 → *:4/5` cadence (converge → post-pull converge → image-pull/rollback net) and are required for fully hands-off deploys.

### Failure notification path

`factory-quadlet-sync.service` and `factory-post-autoupdate.service` both declare:

```ini
OnFailure=factory-deploy-failure.service
```

`factory-deploy-failure.service` is a `Type=oneshot` unit that logs a structured error message
to the systemd journal via `systemd-cat` (tag `factory-deploy-failure`, priority `err`).
Monitor: `journalctl --user -t factory-deploy-failure -f`

### Operator audit (shell actions)

Deploy scripts record imperative operator actions separately from container stdout:

| Channel | Path | Contents |
|---|---|---|
| Operator JSONL | `~/.local/state/factory/logs/operator.log` | `install.sh`, `make converge` (via `deploy/lib/operator-log.sh`) |
| Rotation narrative | `~/.roxabi/factory/rotation-log.md` | Voluntary credential changes (`--force-regen-blobstore`, routine nkey rotations via `_mode_add_identity` or `_mode_full_provision`) |
| Container runtime | journald `--user` | Quadlet stdout/stderr — unchanged |

**Incident triage:**

| Question | Where |
|---|---|
| Container crash / 401 / NATS wire errors | `journalctl --user -u factory-<unit>` |
| Timer deploy ran? | `journalctl --user -u factory-quadlet-sync` |
| Converge skip vs run? | `grep converge_ ~/.local/state/factory/logs/operator.log` |
| Who rotated blobstore? | `rotation-log.md` + `grep blobstore ~/.local/state/factory/logs/operator.log` |

Full query recipes → `docs/runbooks/operator-log.md`. Design → `docs/architecture/observability.md`.

`operator.log` retention: `factory-operator-logrotate.timer` (weekly, 12 rotations, 10M maxsize) via `make quadlet-sync-install`.

Syncthing: `deploy/install.sh` maintains `~/.roxabi/factory/.stignore` (excludes `blobstore.tok`, `blobstore/`, `nats/jetstream/`).

`install.sh` flags: `--force-secrets` (Podman `--replace` only) · `--force-regen-blobstore` (regenerates `blobstore.tok`, logged) · `--force` deprecated alias for `--force-secrets`. `factory secrets reset` uses `--force-secrets` so NATS wipe does not rotate blobstore.

### Manual usage

```bash
# Full atomic converge (operator-initiated)
make converge

# Check convergence state without changing anything (prints none|auth|code-only|structural)
bash -c 'source deploy/lib/deploy-common.sh; _classify_drift "$(read_convergence_state)" "$(compute_convergence_state)"'
```

---

## Network exposure tiers

Host `PublishPort` bind = the access boundary. Every service exposed beyond localhost <!-- drift-ignore -->
carries its own auth — bind tier and auth mechanism are chosen **together**:

| Service | Bind | Reachable | Auth boundary |
|---|---|---|---|
| `factory-nats` 4222 | `0.0.0.0` | LAN + Tailnet | NKey (mandatory, per-identity) |
| `factory-nats` 8222 (monitoring) | `127.0.0.1` | host only | — |
| `factory-blobstore` 8449 | `${TAILSCALE_IPV4}` | Tailnet only | bearer token (#1330) |
| `factory-dashboard` 8765 | `${TAILSCALE_IPV4}` | Tailnet only | control-plane session cookie / API key (`require_principal` on protected BFF); Tailnet bind = defense-in-depth → `docs/architecture/security-routing.md` § control-plane (#2129) |
| `factory-hub` 8443 | `127.0.0.1` | host only | — |
| `factory-loki` 3100 | `127.0.0.1` | host only (logcli / #1760) | — |
| `factory-langfuse-web` 3000 | `127.0.0.1` | host only (trace UI / #1760) | Langfuse login |
| `factory-otel` 4317 (gRPC) / 8450 (HTTP) | `roxabi.network` | pod network (workers); health/query on 8450 | bearer token (query API) |
| `factory-otel-collector` 4317/4318 | — | **disabled** (legacy rollback) | — |
| `factory-langfuse-*` deps | — | `roxabi.network` only | — |

Rules:
- **`0.0.0.0` (LAN + Tailnet) requires strong per-request auth** — only `factory-nats` (NKey) qualifies today. UFW base policy (`deploy/provision.sh`) denies inbound by default; the 4222 LAN-subnet rule is a manual host step (the old `deploy/nats/setup.sh` automation was removed as dead — #2041).
- **No / weak app auth → bind `${TAILSCALE_IPV4}`** (Tailnet-only) + the fail-closed `ExecStartPre` guard; never `0.0.0.0`. Tailnet-IP bind needs no UFW rule (only the tailscale0 address accepts). <!-- drift-ignore -->
- **Internal-only surfaces → `127.0.0.1`.**
- New exposed unit → pick a row, pair it with an auth boundary, document it here.

## Hardening invariants (∀ `.container` file)

`NoNewPrivileges=true` | `ReadOnly=true` | `DropCapability=all`
**Carve-out (Langfuse deps):** `factory-langfuse-{postgres,redis,clickhouse,minio}` omit the trio — upstream entrypoints `setpriv`/chmod data dirs before dropping to service users (→ `docs/architecture/observability.md`).
`UserNS=keep-id:uid=1500,gid=1500` for factory units (container UID 1500)
Secrets via `type=mount` (tmpfs) — ¬env vars, ¬volume wrappers for credentials.
Operational consequence: `type=mount` secrets are bound at container init — `--replace` updates the store but the in-container tmpfs file is stale. ACL permission changes require container restart (not HUP) to refresh (#1390). See [`docs/runbooks/nats-authconf-update.md`](../docs/runbooks/nats-authconf-update.md).
**Carve-out (auth.conf):** `auth.conf` (the public ACL bundle — `U…` nkeys + permission blocks, no private seeds) is delivered as an **inline bind mount**, not a `type=mount` secret. This allows live SIGHUP reload for pure identity-add operations without client restarts. Private NKey seed files (e.g. `factory-nats-hub.seed`) remain `type=mount` (→ `docs/architecture/deployment.md`).
¬inline `#` comments after `Volume=` values — Quadlet passes them to Podman as mount options.

### Secret naming convention

NATS-related secrets use hyphens (`factory-nats-<role>`) — this predates the underscore
convention and is preserved for NATS NKey compatibility. Non-NATS secrets (bearer tokens,
API keys) use underscores (`factory_<service>_<purpose>`, e.g. `factory_blobstore_token`).
Mixing styles is intentional and tracked; do not "normalize" without coordinating
with the operator (Mickael).

Bot per-platform secrets follow the hyphen convention:

| Secret name | In-container target | Mode | Notes |
|---|---|---|---|
| `factory-bot-<platform>-<bot_id>` | `bot_token-<bot_id>` | 0400 | Bot token; always emitted per bot |
| `factory-bot-<platform>-<bot_id>-webhook` | `bot_webhook-<bot_id>` | 0400 | Telegram webhook secret; only emitted when `[[auth.<platform>_bots]].webhook_enabled = true` |

### Known residual risk — blobstore PublishPort Tailscale fallback (#1330)

`factory-blobstore.container` binds PublishPort to `${TAILSCALE_IPV4}:8449:8449` (resolved at
provision time via `tailscale ip -4 | head -1`). If `TAILSCALE_IPV4` is unset or `tailscale0`
is absent at container start, Podman falls back to `0.0.0.0:8449` (LAN-exposed). The bearer
token (`factory_blobstore_token`) is then the **sole** auth boundary. Accepted for V8; Phase 2
(network policy / per-identity tokens) will address this systematically.

The `ExecStartPre=` guard strips all whitespace before the `-n` test (POSIX `tr -d`) so
empty, unset, **and whitespace-only** values are all rejected at the systemd layer (#1368).
Prior to #1368, `[ -n "   " ]` was TRUE in POSIX sh — a whitespace-only value passed the
guard and Podman's downstream parse error provided fail-closed behaviour by accident, not
by design. The guard is now the authoritative rejection point.

### factory-dashboard auth boundary (supersedes “no auth” wording)

`factory-dashboard.container` binds PublishPort to `${TAILSCALE_IPV4}:8765:8765` (same pattern +
fail-closed `ExecStartPre` guard as blobstore above). **Application auth is live:** protected <!-- drift-ignore -->
BFF routes (including `/api/bff/jobs*`, sessions, agents, admin, pipeline) require
`require_principal` (session cookie and/or Bearer API key / shared operator token). Hub RPC
rehydrates principal from proof fields (session_token / api_key) — wire roles alone are not
enough. Jobs list/steer/cancel are further scoped by ownership/org (#2129).

Tailnet-only bind remains **defense-in-depth** (never `0.0.0.0`) — not the sole boundary.
SSE stream tokens (`stream_token` query param) are an additional gate for EventSource; mint
endpoints sit behind principal auth; multi-slot registry avoids peer clobber (#2316).

**Session list API (`/api/bff/sessions*`)** — hub-backed (no `turns.db` mount on the dashboard
container); same principal gate as other protected BFF routes.
→ `docs/runbooks/dashboard-auth-bootstrap.md` · `docs/architecture/security-routing.md` § control-plane
Legacy `FACTORY_DASHBOARD_AUTH_REQUIRED` stub was removed. Remaining #1992 items (if any) are
phase-2 GA hardening beyond this baseline, not “auth absent”.

### Known residual risk — clipool `core.hooksPath` override (tracked #1245)

The clipool unit sets `core.hooksPath = /opt/factory-gh/hooks` via `GIT_CONFIG_GLOBAL`
so the image-baked `prepare-commit-msg` hook fires on every commit. The workspace
volume is mounted RW; a malicious subprocess (uid 1500) could write a per-repo
`.git/config` containing its own `[core] hooksPath = …` that **overrides** the
global setting at the per-repo layer. Within the single-tenant container threat
model — the subprocess is already trusted to execute arbitrary code under
`DropCapability=all` + `ReadOnly=true` — this is **accepted residual risk**.
The follow-up (#1245) tracks switching to `GIT_CONFIG_SYSTEM` (or `GIT_CONFIG_COUNT`)
so the hooksPath becomes process-immutable.

---

## Cross-references

- `docs/runbooks/container-publishing.md` — full CI → GHCR → Quadlet pattern + auto-update
- `docs/ARCHITECTURE.md` — hub-spoke topology
- `docs/architecture/deployment.md` — UserNS + secret delivery decisions
