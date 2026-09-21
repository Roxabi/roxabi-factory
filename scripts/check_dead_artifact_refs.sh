#!/usr/bin/env bash
# check_dead_artifact_refs.sh — flag artifacts/... path references that no
# longer resolve to a file in the repo tree (issue #2221, analysis Appendix B
# "SC1"). Manual prover — NOT wired into `scripts/qg` / .dev/stack.yml;
# run by hand when auditing ADR/artifact coherence.
#
# Scope: docs/ deploy/ scripts/ src/ packages/ tools/ tests/ .github/ README.md
#   - artifacts/ itself is excluded (refs *inside* artifacts/ are not doc rot,
#     they're artifact-to-artifact cross-references — out of scope for SC1).
#   - docs/architecture/CURRENT.generated.md is excluded (generated inventory,
#     not a hand-authored doc — see check_architecture_snapshot.sh).
#   - tests/tools/ is excluded: test_archive_artifacts_wave.py and
#     test_audit_quality_debt.py deliberately reference synthetic/fictional
#     artifacts/ paths as fixtures for testing the archive/audit tooling
#     itself. Confirmed empirically (#2221 T1) these are the only tests/tools/
#     files with artifacts/ hits and none are genuine doc references.
#
# Resolution: an extracted path is considered LIVE if it exists as-is, or
# with one of the fallback extensions appended (.md/.mdx/.claude.md/.json),
# tried first repo-root-relative, then relative to the referring file's own
# directory (covers `../artifacts/...`-style relative refs).
#
# Exclusions (a match is NOT reported even if unresolved):
#   - the bare directory token "artifacts" (not a path into it)
#   - `YYYY`-style date placeholders (e.g. artifacts/archive/YYYY-MM/ in
#     tools/AGENTS.md's prose describing the archival *pattern*, not a ref)
#   - tombstones: `git show <sha-or-tag>:<path>` on the same source line —
#     the convention for citing a path that was deliberately archived out of
#     the working tree (e.g. `git show artifacts-archive/2026-06:artifacts/...`)
#
# Output: one `DEAD <file>:<line> -> <target>` line per dead reference.
# Exit 0 iff no output, exit 1 otherwise. (This script is a manual prover,
# not a tools/ gate — it does not follow the tools/AGENTS.md "exit 0 = ran
# clean" contract; a non-empty finding is the actionable signal here.)
#
# Run:
#   bash scripts/check_dead_artifact_refs.sh
set -euo pipefail

cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not a git repository" >&2; exit 2; }

SCAN_DIRS=(docs deploy scripts src packages tools tests .github README.md)
# This script's own header comments illustrate the ref grammar it scans for
# (e.g. "artifacts/..." , "../artifacts/...", "artifacts/foo") — self-exclude
# so those illustrative examples don't self-report as dead refs.
SELF_PATH="scripts/check_dead_artifact_refs.sh"
EXCLUDE_FILE_EXACT="docs/architecture/CURRENT.generated.md"
EXCLUDE_DIR_PREFIX="tests/tools/"
# image-request.py's docstring shows an example run whose stdout redirect
# target lives under artifacts/evidence/ — an output path the command creates
# at runtime, not a doc reference (#2221 analysis, Liens morts row 25).
EXCLUDE_OUTPUT_PATH="scripts/smoke/image-request.py"

# A missing scan root would silently shrink the scan (grep skips it, the loop
# just sees fewer lines) and turn a maintenance rename into a false-clean
# exit 0 — fail loudly instead: a prover's exit 0 must mean "actually scanned".
for root in "${SCAN_DIRS[@]}"; do
    [ -e "$root" ] || { echo "ERROR: scan root missing: $root" >&2; exit 2; }
done

DEAD=""

while IFS= read -r hit; do
    file="${hit%%:*}"
    rest="${hit#*:}"
    line="${rest%%:*}"
    ref="${rest#*:}"

    [ "$file" = "$EXCLUDE_FILE_EXACT" ] && continue
    [ "$file" = "$SELF_PATH" ] && continue
    [ "$file" = "$EXCLUDE_OUTPUT_PATH" ] && continue
    case "$file" in
        "$EXCLUDE_DIR_PREFIX"*) continue ;;
    esac

    # Normalize the captured path. The grep character class already excludes
    # ) ` , : so this strip is defensive; the trailing-slash and
    # trailing-period strips ARE load-bearing (prose like "...spec.mdx." or
    # "see artifacts/foo/" would otherwise falsely report a dead ref).
    clean="${ref%%[)\`,:]*}"
    clean="${clean%/}"
    clean="${clean%.}"

    [ "$clean" = "artifacts" ] && continue
    case "$clean" in
        *YYYY*) continue ;;
    esac

    # -e (not -f): several refs are bare directory literals (`artifacts/debt`,
    # `artifacts/plans`, `artifacts/archive/...`) — a directory that exists is
    # not a dead ref.
    resolved=""
    for base in "$clean" "$(dirname "$file")/$clean"; do
        for ext in "" ".md" ".mdx" ".claude.md" ".json"; do
            if [ -e "${base}${ext}" ]; then
                resolved="${base}${ext}"
                break 2
            fi
        done
    done

    [ -n "$resolved" ] && continue

    # Tombstone convention: `git show <sha-or-tag>:<path>` cites a path that
    # was deliberately archived out of the working tree. Any occurrence of
    # `git show` on the same source line is treated as a tombstone context —
    # broader than matching the exact ref after the colon, to stay robust to
    # minor formatting variance in how the tombstone is written.
    # sed failure => same_line empty => tombstone check misses => ref reported
    # DEAD (fails toward noise, never toward hiding a real dead ref).
    same_line="$(sed -n "${line}p" "$file" 2>/dev/null || true)"
    case "$same_line" in
        *"git show"*) continue ;;
    esac

    DEAD="${DEAD}DEAD ${file}:${line} -> ${clean}
"
done < <(
    grep -rnoIE '(\.\./)*artifacts/[A-Za-z0-9_./-]+' \
        "${SCAN_DIRS[@]}"
)

if [ -n "$DEAD" ]; then
    printf '%s' "$DEAD"
    exit 1
fi

echo "check_dead_artifact_refs: no dead artifacts/ references found — OK"
exit 0
