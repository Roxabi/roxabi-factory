# Dev Cycle — Close Checklist

Run from the worktree root **before** cleanup (while `HEAD` still points to the PR branch).

## Debt Retrospective

```bash
# Run from worktree root before cleanup (git diff is empty after merge + worktree removal)
git diff origin/staging..HEAD -- '*.py' | grep '^+' | grep -c 'DEBT:'
git diff origin/staging..HEAD -- '*.py' | grep '^+' | grep -c 'noqa:'
git diff origin/staging..HEAD -- '*.py' | grep '^+' | grep -c 'except Exception'
git diff origin/staging..HEAD -- '*.py' | grep '^+' | grep -c 'sleep('
```

| Metric | Threshold | Action |
|--------|-----------|--------|
| `DEBT:` added | > 3 | Split scope next cycle |
| `noqa:` added | > 3 | Review suppressions; each needs `— DEBT:<slug>` |
| `except Exception` added | > 2 | Narrow catch or add debt entry |
| `sleep()` added | > 1 | Justify or replace with async wait |

## Rules

- Every `noqa:` / `pyright: ignore` / `type: ignore` added → must carry `— DEBT:<slug>` suffix per `docs/debt-tracking.md`.
- DEBT: markers without a slug → `tools/audit_quality_debt.py` warns on stderr (exit 0, non-blocking).
- > 3 DEBT: added in one cycle → flag scope for splitting in next planning session.

## Reference

- Debt registry: `artifacts/debt/` — one `<slug>.md` per entry.
- Audit tool: `tools/audit_quality_debt.py` (run via `make quality-debt-report`).
- Full debt policy: `docs/debt-tracking.md`.

## Integration Status

This checklist is referenced from `AGENTS.md` TL;DR. Automated invocation from the `/dev` SKILL.md cleanup step (in `roxabi-plugins`) is **pending** — tracked as a follow-up cross-repo change. Until wired, the checklist must be run manually before worktree cleanup.
