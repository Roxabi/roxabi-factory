#!/usr/bin/env bash
# check_debt_expiry.sh — block stale DEBT: markers older than 6 months.
#
# A DEBT: marker is considered stale when ALL of the following are true:
#   1. The file that contains it was last modified more than 6 months ago
#      (per `git log --format=%ci -1 -- <file>`).
#   2. The marker comment does NOT contain a GitHub issue reference (#NNN).
#
# Rationale: an attached issue number (#NNN) indicates an active remediation
# path exists; file recency is the proxy for "still being worked on".
# Markers with an issue ref are never expired here — use the audit report for
# those.
#
# Exit-code contract (consistent with gate scripts in this repo — tools/AGENTS.md):
#   0 = no stale markers found
#   1 = stale markers detected (merge-blocking)
#   2 = script error (missing dep / corrupt git state)
#
# Environment overrides:
#   DEBT_EXPIRY_MONTHS  — grace period in months (default: 6)
#   DEBT_SCAN_ROOT      — directory to scan (default: src/)
#   DEBT_INCLUDE_EXT    — file extension to scan (default: py)
#
# Run locally:
#   bash tools/check_debt_expiry.sh
# Run in CI:
#   debt-expiry step in .github/workflows/ci.yml
#
# Note: git log date is file-level, not line-level. This is intentional:
#   - git blame per-line would be O(N) subprocess calls — too slow for CI.
#   - File-level last-modification is a conservative proxy: if the file was
#     touched recently the debt was presumably reviewed.
#   - False positives (file edited for unrelated reason) are low-cost: the
#     author simply adds a #NNN reference to pin the expiry.
set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve repo root and move there
# ---------------------------------------------------------------------------
cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not a git repository" >&2; exit 2; }

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MONTHS="${DEBT_EXPIRY_MONTHS:-6}"
SCAN_ROOT="${DEBT_SCAN_ROOT:-src/}"
EXT="${DEBT_INCLUDE_EXT:-py}"

# Cutoff timestamp: today minus MONTHS months (YYYY-MM-DD).
# date -d is GNU; macOS requires date -v.
if date --version >/dev/null 2>&1; then
    # GNU date
    CUTOFF=$(date -d "$MONTHS months ago" +%Y-%m-%d)
else
    # macOS / BSD date
    CUTOFF=$(date -v "-${MONTHS}m" +%Y-%m-%d)
fi

# ---------------------------------------------------------------------------
# Guard: scan root must exist
# ---------------------------------------------------------------------------
if [ ! -d "$SCAN_ROOT" ]; then
    echo "WARN: $SCAN_ROOT not found, skipping check_debt_expiry" >&2
    exit 0
fi

# ---------------------------------------------------------------------------
# Collect files containing DEBT: markers
# ---------------------------------------------------------------------------
mapfile -d '' FILES < <(
    grep -rl "DEBT:" "$SCAN_ROOT" \
        --include="*.$EXT" \
        -Z \
        2>/dev/null \
    || true
)

if [ ${#FILES[@]} -eq 0 ]; then
    echo "check_debt_expiry: no DEBT: markers found in ${SCAN_ROOT} — OK"
    exit 0
fi

# ---------------------------------------------------------------------------
# Gate loop
# ---------------------------------------------------------------------------
FAIL=0
STALE_COUNT=0

for file in "${FILES[@]}"; do
    # File-level last-commit date (ISO 8601 YYYY-MM-DD)
    LAST_DATE=$(git log --format="%ci" -1 -- "$file" 2>/dev/null | cut -d' ' -f1)
    if [ -z "$LAST_DATE" ]; then
        # Untracked / new file — not stale (no commit yet)
        continue
    fi

    # Skip files modified after the cutoff
    if [[ "$LAST_DATE" > "$CUTOFF" || "$LAST_DATE" == "$CUTOFF" ]]; then
        continue
    fi

    # File is older than the grace period — inspect each DEBT: line
    while IFS= read -r line; do
        lineno=$(echo "$line" | cut -d: -f1)
        content=$(echo "$line" | cut -d: -f2-)

        # Presence of a GitHub issue reference (#NNN) on the same line
        # means an active remediation path exists → skip expiry check.
        # Use ERE (-E) instead of -P for portability across GNU grep and macOS BSD grep.
        if echo "$content" | grep -qE '#[0-9]{1,6}([^0-9]|$)'; then
            continue
        fi

        # Stale marker: file old + no issue ref
        if [ "$STALE_COUNT" -eq 0 ]; then
            echo ""
            echo "FAIL: stale DEBT: markers (file last modified ${LAST_DATE}, cutoff ${CUTOFF}, no issue ref):" >&2
        fi
        echo "  ${file}:${lineno}: ${content}" >&2
        STALE_COUNT=$((STALE_COUNT + 1))
        FAIL=1
    done < <(grep -n "DEBT:" "$file" || true)
done

if [ "$FAIL" -eq 0 ]; then
    echo "check_debt_expiry: all DEBT: markers are within the ${MONTHS}-month grace period or have an issue ref — OK"
else
    echo "" >&2
    echo "Found ${STALE_COUNT} stale DEBT: marker(s)." >&2
    echo "Remediation options:" >&2
    echo "  1. Resolve the debt and remove the marker." >&2
    echo "  2. Open a GitHub issue and append '#<N>' to the DEBT: comment." >&2
    echo "     Example:  # noqa: BLE001 — DEBT:boundary-broad-catch #1234" >&2
    echo "  3. Touch the file with a meaningful commit (resets the 6-month clock)." >&2
fi

exit $FAIL
