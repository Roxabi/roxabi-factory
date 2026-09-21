# Quality Gates — Index

Operator reference for automated checks. **Single source of truth:** `.dev/stack.yml` (`quality_gates` + `qg.run_order`).

**Runner:** `scripts/qg` (bash + [yq](https://github.com/mikefarah/yq)) — reads stack.yml at execution time (no generated wiring, no drift).

**Layout:** gate implementations live in `tools/`; `scripts/` holds the runner and repo-specific scanners. See `CONTRIBUTING.md` § Language & layout.

For script behaviour and exit codes, see [`tools/AGENTS.md`](../../tools/AGENTS.md).

---

## Architecture (4 files)

| File | Role |
|------|------|
| `.dev/stack.yml` | Declares gates (`quality_gates`), stages, scripts, file filters, execution order (`qg.run_order`) |
| `scripts/qg` | Executes gates for a stage, profile, or single gate name |
| `.pre-commit-config.yaml` | Stable shell: upstream hooks + `qg run --stage pre-commit` / `pre-push` |
| `.github/workflows/ci.yml` | CI bootstrap (uv, bun, yq) + `qg run --stage ci` + meta-tests, coverage, e2e |

**CI job layout (#2177):** the former monolithic `ci` job is split 3-way — `gates`
(runs `qg run --stage ci`), `tests`, and `package-coverage` run in parallel
(~4m50 serial → ~2m45 wall). An aggregate job keeps the id `ci` so the required-check
context on `staging` is unchanged.

`tools/qg.conf` remains generated runtime config for file-length scripts (drift-gated by `scripts/check-qg-conf-drift.sh`).

**Wiring rule:** a gate runs on a stage only when it appears in **both** `quality_gates.<name>.stages` **and** `qg.run_order.<stage>`. `tests/scripts/test_qg.sh` enforces this.

---

## Run locally

```bash
make dev-setup                              # after clone
scripts/qg run --stage pre-commit           # commit hooks parity
scripts/qg run --stage pre-push             # push hooks parity (incl. deploy gates)
scripts/qg run --stage ci                   # CI gate bundle
scripts/qg run lint_js                      # single gate
make pre-pr                                 # pre-PR ritual (see below)
make qg                                     # profile local + extra factory tests
pre-commit run --all-files                  # upstream + pre-commit stage
pre-commit run --hook-stage pre-push --all-files
```

### Pre-PR ritual (`make pre-pr`)

Before applying the `reviewed` label on a feature PR:

1. **`scripts/qg run --stage pre-push`** — typecheck, smoke pytest, deploy gates, doc drift.
2. **`scripts/qg plan --stage ci`** — diff-scoped gate/job plan (same contract CI logs on PRs).
   Default diff for `make pre-pr`: `origin/staging...HEAD` (run `git fetch origin staging` first if
   the ref is missing). Override with `QG_DIFF_RANGE=...`.
3. **`tools/check_pytest_partition.py`** — collect-only check that CI pytest partitions stay
   disjoint and that `ci.yml` still calls `scripts/ci-pytest.sh` for each runtime partition.

Partition expressions live in **`tools/pytest_partitions.py`** (SSoT). CI invokes
`scripts/ci-pytest.sh <partition>`; do not duplicate `-m` / `--ignore` strings in `ci.yml`.

---

## Stages

| Stage | When | stack.yml key |
|-------|------|---------------|
| `pre-commit` | `git commit` | `qg.run_order.pre-commit` |
| `pre-push` | `git push` | `qg.run_order.pre-push` |
| `ci` | GitHub Actions `ci` job | `qg.run_order.ci` |

Gates list target stages in `quality_gates.<name>.stages`.

Path-filtered gates (`files:` regex) skip when no changed file matches. This is
scoped to a diff range (`QG_DIFF_RANGE`):

- **commit/push hooks** — the local staged/pushed diff.
- **CI on a pull request (#2176)** — the merge-base diff (`origin/<base>...HEAD`),
  so a gate whose inputs the PR does not touch is skipped.
- **CI on push to `staging`/`main` and merge-queue entries** — `QG_DIFF_RANGE` is
  empty, so `scripts/qg` **fails open** and runs **all** gates: protected-branch and
  queue runs validate the full merge result, never a diff-scoped subset.

---

## Deploy gates

Deploy integrity checks (`secrets_drift`, `quadlet_manifest_install`, `volumes_table`, `secrets_source`) run on **pre-push**, **CI**, and **`make qg`** (local profile).

| Gate | pre-push | ci | `make qg` | Notes |
|------|:--------:|:--:|:---------:|-------|
| `secrets_drift` | yes | yes | yes | |
| `quadlet_manifest_install` | yes | yes | yes | |
| `volumes_table` | yes | yes | yes | |
| `secrets_source` | yes | yes | yes | Skips (exit 0) when `~/.roxabi/factory` absent — typical on CI runners |

`quadlet_component_source` is **ci-only** (not pre-push).

Before a **deploy PR**, `scripts/qg run --stage pre-push` or `make qg` is sufficient for deploy gates. For full CI parity: `scripts/qg run --stage ci`.

`make quadlet-lint` for path-scoped Quadlet checks (see `quadlet-lint.yml` workflow).

---

## CI extras (not in `qg run --stage ci`)

Explicit steps in `.github/workflows/ci.yml` after the QG bundle:

- `uv lock --check` in the `gates` job (pyproject.toml ↔ `uv.lock` desync tripwire; #2173)
- Gate self-tests (`tests/tools/test_check_*.sh`, `tests/scripts/test_qg.sh`)
- Dashboard Playwright e2e, package coverage thresholds
- Pytest jobs via `scripts/ci-pytest.sh` (partitions from `tools/pytest_partitions.py`)
- Directory↔marker layout (`tools/check_pytest_dir_markers.py` — markers only under
  `MARKER_DIR_ALLOWLIST` prefixes in `tools/pytest_partitions.py`; #2287 / #2288
  docker tier = `tests/nats/integration/`)
- Jobs `integration`, `docker-build` (`docker-build` is a required check on `staging`)

ACL scanners (`acl_matrix_retired`, `request_reply_flows`, `acl_grants`, `inbox_prefix`, `subject_literals`) are declared in `stack.yml` and run inside `qg run --stage ci`.

---

## Adding a gate

1. Add `quality_gates.<name>` in `.dev/stack.yml` (`script`, `stages`, optional `files`, `env`, `requires`).
2. Add `<name>` to `qg.run_order.<stage>` for **each** stage in `stages`.
3. Regenerate `tools/qg.conf` via `/release-setup --force` if the gate uses file-length/folder shared config.
4. Document non-obvious behaviour in `tools/AGENTS.md`; add `tests/tools/` when logic is non-trivial.

No pre-commit or ci.yml edit required for standard gates.
