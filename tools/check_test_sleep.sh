#!/usr/bin/env bash
# check_test_sleep.sh — block raw sleep() calls in tests without a sync comment.
#
# A sleep() call is flagged when ALL of the following are true:
#   1. The line contains asyncio.sleep( or time.sleep( (an actual invocation).
#   2. The same line does NOT contain "# event-based" or "# NATS delivery window".
#   3. The line is not a mock/patch assignment (does not contain "patch",
#      "mock", "Mock", "_RL_SLEEP", or "asyncio.sleep = ").
#
# Rationale: timing-based sleeps in tests are flaky under CI load. Every sleep
# must document why it cannot be replaced with event-based synchronisation:
#   - "# event-based" — a genuine event loop yield, cancellation park, or
#     filesystem-timing wait that is structurally unavoidable.
#   - "# NATS delivery window" — waiting for NATS message propagation where
#     an event-based alternative would require infrastructure changes.
#
# Exit-code contract (consistent with gate scripts in this repo — tools/AGENTS.md):
#   0 = no violations found
#   1 = violations detected (merge-blocking)
#   2 = script error (missing dep / corrupt git state)
#
# Environment overrides:
#   SLEEP_SCAN_ROOT  — directory to scan (default: tests/)
#
# Run locally:
#   bash tools/check_test_sleep.sh
# Run in CI:
#   check-test-sleep step in .github/workflows/ci.yml
set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve repo root and move there
# ---------------------------------------------------------------------------
cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not a git repository" >&2; exit 2; }

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCAN_ROOT="${SLEEP_SCAN_ROOT:-tests/}"

# ---------------------------------------------------------------------------
# Guard: scan root must exist
# ---------------------------------------------------------------------------
if [ ! -d "$SCAN_ROOT" ]; then
    echo "WARN: $SCAN_ROOT not found, skipping check_test_sleep" >&2
    exit 0
fi

# ---------------------------------------------------------------------------
# Scan: find sleep() invocations — MULTI-LINE AWARE.
# The sync comment may sit on ANY line of the call, e.g. on the closing ')' of a
# multi-line  `await asyncio.sleep(\n    0.1\n)  # NATS delivery window`.
# We walk each statement from `.sleep(` until its parentheses balance, then check
# the whole span for the comment (and for mock/patch markers → skip).
# ---------------------------------------------------------------------------
VIOLATIONS="$(
    find "$SCAN_ROOT" -type f -name '*.py' -print0 2>/dev/null \
    | xargs -0 -r awk '
        function flush() {
            if (!instmt) return
            if (buf !~ /# event-based/ && buf !~ /# NATS delivery window/ \
                && buf !~ /patch|[Mm]ock|_RL_SLEEP/)
                printf "%s:%d\n", sfile, sline
            instmt = 0; depth = 0; buf = ""
        }
        BEGIN { SQ = sprintf("%c", 39) }   # apostrophe char, for docstring detection
        FNR == 1 { flush() }
        {
            if (!instmt) {
                if ($0 ~ /(asyncio\.sleep|time\.sleep)[ \t]*\(/) {
                    t = $0; sub(/^[ \t]+/, "", t)
                    c1 = substr(t, 1, 1)
                    if (c1 == "#" || c1 == "\"" || c1 == SQ) next   # comment-only / docstring line
                    if ($0 ~ /asyncio\.sleep[ \t]*=/) next   # reassignment, not a call
                    seg = $0; sub(/.*(asyncio\.sleep|time\.sleep)[ \t]*/, "", seg)
                    op = seg; cl = seg
                    depth = gsub(/\(/, "", op) - gsub(/\)/, "", cl)
                    instmt = 1; sfile = FILENAME; sline = FNR; buf = $0
                    if (depth <= 0) flush()                  # single-line call
                }
                next
            }
            buf = buf "\n" $0
            op = $0; cl = $0
            depth += gsub(/\(/, "", op) - gsub(/\)/, "", cl)
            if (depth <= 0) flush()                          # statement closed
        }
        END { flush() }
    '
)"

if [ -n "$VIOLATIONS" ]; then
    echo "" >&2
    echo "FAIL: raw sleep() calls in tests without sync comment:" >&2
    while IFS= read -r v; do
        [ -n "$v" ] || continue
        echo "  ${v}" >&2
        echo "::error file=${v%:*},line=${v##*:}::raw sleep() — add '# event-based' or '# NATS delivery window' comment"
    done <<< "$VIOLATIONS"
    cnt="$(printf '%s\n' "$VIOLATIONS" | grep -c .)"
    echo "" >&2
    echo "Found ${cnt} raw sleep() call(s) without sync comment." >&2
    echo "Add '# event-based' or '# NATS delivery window' on any line of the call." >&2
    exit 1
fi

echo "check_test_sleep: no raw sleep() calls found in ${SCAN_ROOT} — OK"
exit 0
