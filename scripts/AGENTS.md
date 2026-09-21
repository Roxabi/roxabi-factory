# AGENTS.md — scripts/

## Role

**Platform orchestration** (bash) and **domain operational tooling** for this repo.

| Kind | Language | Examples |
|------|----------|----------|
| Orchestration | **bash only** | `qg`, `check-*-drift.sh` |
| Domain ops entrypoint | **bash** → Python | `check_inbox_prefix.sh` → `.py` |
| Domain ops implementation | **Python** | `gen_nkeys.py`, `render_acl_*.py`, `check_grants.py` |
| Bootstrap | **bash** in `tools/` | `dev-setup.sh` (installs yq, uv sync, hooks) |

## vs `tools/`

| | `scripts/` | `tools/` |
|---|------------|----------|
| **What** | Run the factory (ACL, CI scanners, qg runner) | Quality gates invoked **by** `stack.yml` / `qg` |
| **Who calls** | `scripts/qg run` (all `stack.yml` gates); Makefile / `factory-acl` for direct ops | Gate implementations in `tools/` (deploy, lint, size) |
| **dev-core** | Repo-specific | Canonical pattern from dev-core plugin |

Rule: new **quality gate** → implementation in `tools/`, declaration in `stack.yml`.
New **ACL/deploy scanner** → `scripts/` (bash entry + Python if needed).

See `docs/runbooks/quality-gates.md` and `CONTRIBUTING.md` § Language & layout.

---

## Patterns (not an inventory)

The authoritative gate list is `.dev/stack.yml` `quality_gates` (each `check-*`
scanner here is referenced by a gate entry); `ls scripts/` is the file list. Rather
than duplicate either, recognise the kinds:

- **Orchestration (bash)** — `qg` (reads `.dev/stack.yml` via yq) and the
  `check-*-drift.sh` guards (generated-artifact drift: qg.conf, ACL spec/authconf,
  theme build, astryx doctor). Pure bash, no parsing logic.
- **Domain-ops scanners** — anything CI or the Makefile invokes directly gets a
  `.sh` wrapper (`check_<x>.sh`) whose parsing lives in a sibling `check_<x>.py`;
  Python-only tools that CI reaches via a `factory-*` console entry (`check_grants.py`,
  `check_acl_matrix_retired.py`, `check_request_reply_flows.py`) have no wrapper. ACL /
  nkey provisioning (`render_acl_*.py`, `gen_nkeys.py`) runs from deploy / `make nats-*`.
- **Shared `_`-prefixed Python** (`_loader.py`, `_renderer.py`, `_nk.py`, …) supports
  the ACL/nkeys tooling — not standalone entrypoints.
- **One-offs / evidence** — `goal-*`, `backfill_*.py`, `blob-reconcile.py`: run
  manually with intent, never wired as gates.

Rule: new persistent scanner → `.sh` wrapper **iff** CI/Makefile invokes it directly,
parsing in `.py`; declare the gate in `.dev/stack.yml`, not here.
