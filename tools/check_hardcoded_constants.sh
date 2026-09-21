#!/usr/bin/env bash
# check_hardcoded_constants.sh — block NEW hardcoded numeric constants in src/factory/core/.
#
# DETECTION LOGIC
# ---------------
# A line is flagged when ALL of the following are true:
#   1. The file is under SCAN_ROOT (default: src/factory/core/).
#   2. The line contains a numeric literal >= 2 digits in an assignment (=N),
#      function-call argument (,N or (N), keyword-argument (key=N), or
#      comparison/condition context (>N, <N, >=N, <=N, ==N, !=N).
#      grep -E pattern: [=,(<>!][[:space:]]*[0-9]{2,6}([^0-9.]|$)
#      Rationale: 2-digit minimum avoids flagging 0, 1, and single-digit
#      loop indices; 6-digit ceiling avoids port numbers etc.
#   3. The line does NOT reference a Config or Protocol constant (contains
#      "Config." or "Protocol." — the sanctioned indirections).
#   4. The line does NOT carry an inline exemption marker "# const-ok:"
#      anywhere on the line.
#   5. The line is not a pure comment or blank.
#
# BASELINE-GRANDFATHER (optional, retired)
# ----------------------------------------
# Constants that existed when the gate was introduced (#1654) were once
# grandfathered in an optional baseline file (CONST_BASELINE_FILE).  The
# burn-down completed (#1698) and the file was deleted (#1706).  An ABSENT
# baseline is now treated as the EMPTY SET (no grandfathered entries) — every
# flagged line is a violation.  If a baseline file is present, a flagged line
# is a violation ONLY IF its signature is NOT in it.  Do NOT reintroduce a
# baseline file — there is nothing left to grandfather.
#
# SIGNATURE FORMAT
# ----------------
# Signature = "<relpath>:<normalized-line>"
# where:
#   relpath          = path of the file relative to the repo root
#                      (e.g. src/factory/core/hub/hub.py)
#   normalized-line  = code line with leading AND trailing whitespace stripped
#
# Leading/trailing whitespace is stripped so that reformatting (indentation
# changes) and minor code-moves do not produce false positives.  Line numbers
# are NOT part of the signature for the same reason.
#
# ESCAPE HATCH
# ------------
# Add "# const-ok: <reason>" as a trailing comment on the offending line to
# permanently exempt it.  Use sparingly and document the reason inline.
# Example:
#   NATS_MAX_PAYLOAD = 1024 * 1024  # const-ok: NATS protocol hard limit
#
# EXIT-CODE CONTRACT (consistent with all gate scripts — tools/AGENTS.md)
#   0 = clean (no new violations)
#   1 = violations found (merge-blocking)
#   2 = script error (missing dep / bad git state)
#
# ENVIRONMENT OVERRIDES
#   CONST_SCAN_ROOT      — directory to scan (default: src/factory/core/)
#   CONST_BASELINE_FILE  — path to an optional baseline (default:
#                          tools/hardcoded_constants_baseline.txt; absent = empty set)
#
# RUN LOCALLY
#   bash tools/check_hardcoded_constants.sh
# RUN IN CI
#   check-hardcoded-constants step in .github/workflows/ci.yml

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve repo root and cd there so all relative paths are stable
# ---------------------------------------------------------------------------
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || { echo "ERROR: not a git repository" >&2; exit 2; }
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCAN_ROOT="${CONST_SCAN_ROOT:-src/factory/core/}"
BASELINE_FILE="${CONST_BASELINE_FILE:-tools/hardcoded_constants_baseline.txt}"

# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
if [ ! -d "$SCAN_ROOT" ]; then
    echo "WARN: $SCAN_ROOT not found, skipping check_hardcoded_constants" >&2
    exit 0
fi

# ---------------------------------------------------------------------------
# scan_tree: emit signature lines for all flagged lines under SCAN_ROOT.
# Signature = "<relpath>:<normalized-line>"
# Normalization: strip leading + trailing whitespace from the source line.
#
# Uses grep for ERE matching (portable, handles {2,6}) then awk for
# normalization and relpath computation.
# ---------------------------------------------------------------------------
scan_tree() {
    local root="$1"
    # Find all .py files, grep for candidate lines, then normalize.
    # grep output format: <file>:<line-content>
    find "$root" -type f -name "*.py" -print0 \
        | sort -z \
        | xargs -0 grep -E --with-filename '[=,(<>!][[:space:]]*[0-9]{2,6}([^0-9.]|$)' \
                2>/dev/null || true
}

emit_signatures() {
    # Filter and normalize grep output:
    #   <filepath>:<line-content>
    # Excludes:
    #   - pure comment lines (first non-space char is #)
    #   - blank lines
    #   - lines containing Config. or Protocol. (sanctioned indirections)
    #   - lines containing # const-ok: (inline exemption)
    #   - f-string format specs: {expr:[<>^=]?N} or {expr:.Nf} etc. (width/precision literals
    #     stripped from content via gsub so co-located real literals are still caught)
    #   - regex quantifiers immediately preceded by a backslash escape class \d\D\w\W\s\S
    #     (stripped via gsub for the same reason)
    # Emits: <relpath>:<normalized-line>
    awk '
        {
            # Split on first ":" to get filepath + rest
            colon = index($0, ":")
            if (colon == 0) next
            filepath = substr($0, 1, colon - 1)
            content  = substr($0, colon + 1)

            # Strip leading whitespace from content to check for comment lines
            trimmed = content
            sub(/^[[:space:]]+/, "", trimmed)

            # Skip pure comment lines and blank lines
            if (trimmed == "" || substr(trimmed, 1, 1) == "#") next

            # Skip lines with sanctioned indirections (Config. / Protocol.)
            if (index(content, "Config.") > 0 || index(content, "Protocol.") > 0) next

            # Skip lines with inline exemption marker
            if (index(content, "# const-ok:") > 0) next

            # Strip f-string format specs: a {…} brace group whose content
            # contains a format-spec (:[<>^=]?[0-9]+(\.[0-9]+)?[dfeg]?).
            # Matches patterns like f"{x:<20}", f"{v:.2f}", f"{n:08d}".
            # gsub (not next) so a co-located real magic number on the same
            # line is still caught by the downstream numeric-literal check.
            gsub(/\{[^}]*:[<>^=]?[0-9]+(\.[0-9]+)?[dfeg]?[^}]*\}/, "", content)

            # Strip regex quantifiers that are immediately preceded by a
            # backslash escape class (\d \D \w \W \s \S).
            # Matches: \d{8,12}, \w{3}, \s{1,4}, etc.
            # Does NOT match plain set/dict literals like {10, 20} (no preceding \escape).
            # gsub (not next) so a co-located real magic number is still caught.
            gsub(/\\[dDwWsS]\{[0-9]+(,[0-9]*)?\}/, "", content)

            # After stripping exempt tokens, re-check that the residual content
            # still contains a flagged numeric literal (mirrors the grep pre-filter
            # pattern).  If the gsubs cleared all flagged tokens, skip the line.
            if (!match(content, /[=,(<>!][[:space:]]*[0-9][0-9]/)) next

            # Normalize: strip leading + trailing whitespace from content
            normalized = content
            sub(/^[[:space:]]+/, "", normalized)
            sub(/[[:space:]]+$/, "", normalized)

            print filepath ":" normalized
        }
    '
}

# ---------------------------------------------------------------------------
# Baseline generation mode
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--generate-baseline" ]; then
    {
        echo "# DIAGNOSTIC LISTING — current hardcoded numeric constants in src/factory/core/"
        echo "#"
        echo "# The grandfather baseline was retired (#1698 burn-down → #1706 deletion). This"
        echo "# mode is now a read-only diagnostic: it prints every signature the gate would"
        echo "# flag at HEAD. Do NOT commit this output as a baseline — an absent baseline is"
        echo "# treated as the empty set, and reintroducing one would re-grandfather debt."
        echo "#"
        echo "# SIGNATURE FORMAT: <relpath>:<normalized-line>"
        echo "#   relpath         = file path relative to repo root"
        echo "#   normalized-line = line with leading + trailing whitespace stripped"
        echo "# Line numbers are omitted so reformatting does not cause false positives."
    }
    scan_tree "$SCAN_ROOT" | emit_signatures | sort -u
    exit 0
fi

# ---------------------------------------------------------------------------
# Scan current tree and emit all signatures
# ---------------------------------------------------------------------------
ALL_SORTED="$(scan_tree "$SCAN_ROOT" | emit_signatures | sort -u)"

# If nothing flagged at all, clean
if [ -z "$ALL_SORTED" ]; then
    echo "check_hardcoded_constants: no hardcoded numeric constants found in ${SCAN_ROOT} — OK"
    exit 0
fi

# ---------------------------------------------------------------------------
# Load baseline (sorted, comments stripped). An ABSENT baseline file is the
# empty set (no grandfathered entries) — every flagged signature is a violation.
# ---------------------------------------------------------------------------
if [ -f "$BASELINE_FILE" ]; then
    BASELINE_SORTED="$(grep -v '^[[:space:]]*#\|^[[:space:]]*$' "$BASELINE_FILE" | sort -u)"
else
    BASELINE_SORTED=""
fi

# ---------------------------------------------------------------------------
# Set-difference: violations = flagged signatures NOT in baseline
# comm -23 on two sorted inputs = lines only in the first (violations)
# ---------------------------------------------------------------------------
VIOLATIONS="$(comm -23 \
    <(printf '%s\n' "$ALL_SORTED") \
    <(printf '%s\n' "$BASELINE_SORTED") \
    || true)"

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
if [ -z "$VIOLATIONS" ]; then
    echo "check_hardcoded_constants: no new hardcoded numeric constants found in ${SCAN_ROOT} — OK"
    exit 0
fi

echo "" >&2
echo "FAIL: hardcoded numeric constants found in ${SCAN_ROOT}:" >&2
while IFS= read -r v; do
    [ -n "$v" ] || continue
    filepath="${v%%:*}"
    echo "  ${v}" >&2
    echo "::error file=${filepath}::hardcoded numeric constant — use a named Config constant or add '# const-ok: <reason>' if intentional"
done <<< "$VIOLATIONS"
cnt="$(printf '%s\n' "$VIOLATIONS" | grep -c .)"
echo "" >&2
echo "Found ${cnt} new hardcoded constant(s) in ${SCAN_ROOT}." >&2
echo "Remediation options:" >&2
echo "  1. Replace the literal with a named constant in a Config class." >&2
echo "  2. Add '# const-ok: <reason>' on the line if the literal is genuinely un-configurable." >&2
echo "  Do NOT reintroduce a grandfather baseline — it was retired in #1706." >&2
exit 1
