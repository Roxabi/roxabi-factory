#!/usr/bin/env bash
# check_file_exemptions.sh — enforce expiry dates on file-length exemptions.
#
# Every non-comment entry in tools/file_exemptions.txt MUST carry a well-formed
# expires=YYYY-MM-DD token. The gate fails if:
#   - An active entry is missing an expires= token.
#   - An active entry carries an expires= date that is strictly before today.
#
# Rationale: exemptions represent acknowledged technical debt. Without an expiry
# date they accumulate silently and become stale. The 6-month horizon forces a
# periodic review (either resolve the debt or push the date with a new rationale).
#
# Exit-code contract (consistent with all gate scripts in this repo — tools/AGENTS.md):
#   0 = clean (all entries have a valid, non-expired date)
#   1 = violations found (missing or expired date — merge-blocking)
#   2 = script error (missing file, bad environment, etc.)
#
# Environment overrides:
#   FILE_EXEMPTIONS_PATH   — path to the exemptions file (default: tools/file_exemptions.txt)
#   FILE_EXEMPTIONS_TODAY  — override today's date for testing (YYYY-MM-DD)
#
# Run locally:
#   bash tools/check_file_exemptions.sh
# Run in CI:
#   file-exemptions step in .github/workflows/ci.yml
set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve repo root and move there
# ---------------------------------------------------------------------------
cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not a git repository" >&2; exit 2; }

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
EXEMPT_FILE="${FILE_EXEMPTIONS_PATH:-tools/file_exemptions.txt}"
TODAY="${FILE_EXEMPTIONS_TODAY:-$(date +%F)}"

# ---------------------------------------------------------------------------
# Guard: exemptions file must exist
# ---------------------------------------------------------------------------
if [ ! -f "$EXEMPT_FILE" ]; then
    echo "WARN: $EXEMPT_FILE not found, skipping check_file_exemptions" >&2
    exit 0
fi

# ---------------------------------------------------------------------------
# Validate TODAY format
# ---------------------------------------------------------------------------
if ! echo "$TODAY" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
    echo "ERROR: TODAY='$TODAY' is not a valid YYYY-MM-DD date" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Gate loop — inspect each non-comment, non-blank line
# ---------------------------------------------------------------------------
FAIL=0
MISSING_COUNT=0
EXPIRED_COUNT=0

while IFS= read -r line; do
    # Skip blank lines and comment lines (lines whose first non-space char is #)
    stripped="${line#"${line%%[![:space:]]*}"}"  # ltrim
    [ -z "$stripped" ] && continue
    [[ "$stripped" == \#* ]] && continue

    # Extract the path (first whitespace-delimited field)
    path=$(echo "$line" | awk '{ print $1 }')

    # Look for expires=YYYY-MM-DD anywhere on the line
    expires=""
    if echo "$line" | grep -qE 'expires=[0-9]{4}-[0-9]{2}-[0-9]{2}'; then
        expires=$(echo "$line" | grep -oE 'expires=[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1 | cut -d= -f2)
    fi

    if [ -z "$expires" ]; then
        echo "MISSING expires= : $path" >&2
        MISSING_COUNT=$((MISSING_COUNT + 1))
        FAIL=1
        continue
    fi

    # Validate date format
    if ! echo "$expires" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
        echo "MALFORMED expires=$expires : $path" >&2
        MISSING_COUNT=$((MISSING_COUNT + 1))
        FAIL=1
        continue
    fi

    # Compare dates lexicographically (YYYY-MM-DD sorts correctly as strings)
    if [[ "$expires" < "$TODAY" ]]; then
        echo "EXPIRED expires=$expires (today=$TODAY): $path" >&2
        EXPIRED_COUNT=$((EXPIRED_COUNT + 1))
        FAIL=1
    fi
done < "$EXEMPT_FILE"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
if [ "$FAIL" -eq 0 ]; then
    echo "check_file_exemptions: all entries have a valid, non-expired expiry date — OK"
else
    echo "" >&2
    TOTAL=$((MISSING_COUNT + EXPIRED_COUNT))
    [ "$MISSING_COUNT" -gt 0 ] && echo "  $MISSING_COUNT entry(ies) missing expires=YYYY-MM-DD" >&2
    [ "$EXPIRED_COUNT" -gt 0 ] && echo "  $EXPIRED_COUNT entry(ies) with an expired date (before $TODAY)" >&2
    echo "" >&2
    echo "Found $TOTAL violation(s) in $EXEMPT_FILE." >&2
    echo "Remediation:" >&2
    echo "  - Add   expires=YYYY-MM-DD  to entries that lack it." >&2
    echo "  - Resolve the debt (remove the exemption) if it is past its expiry." >&2
    echo "  - Push the expiry date forward with an updated rationale + issue ref." >&2
    echo "  Example: src/factory/foo.py  # 350 lines — DEBT:refactor — #1234 rationale expires=2027-01-01" >&2
fi

exit $FAIL
