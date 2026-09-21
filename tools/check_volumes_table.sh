#!/usr/bin/env bash
# check_volumes_table.sh — verify deployment.md Volumes table matches quadlet Volume= lines.
#
# Parses the Volumes table in docs/architecture/deployment.md and compares
# the Container(s) column against Volume= directives in deploy/quadlet/*.container*
# (both .container and .container.tmpl files).
#
# A mismatch is flagged when:
#   - A Volume= line in a quadlet unit references a path that does not appear in
#     the Volumes table, OR
#   - The Volumes table claims a per-file bind for adapters but the quadlets mount
#     the full factory-data.volume (the known drift this gate was written to catch).
#
# Exit-code contract (consistent with gate scripts in this repo — tools/AGENTS.md):
#   0 = no drift detected
#   1 = drift detected (merge-blocking)
#   2 = script error (missing file / parse failure)
#
# Usage:
#   bash tools/check_volumes_table.sh
set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve repo root
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DEPLOYMENT_DOC="${REPO_ROOT}/docs/architecture/deployment.md"
QUADLET_DIR="${REPO_ROOT}/deploy/quadlet"

# ---------------------------------------------------------------------------
# Guard: required paths must exist
# ---------------------------------------------------------------------------
if [ ! -f "${DEPLOYMENT_DOC}" ]; then
    echo "ERROR: ${DEPLOYMENT_DOC} not found" >&2
    exit 2
fi
if [ ! -d "${QUADLET_DIR}" ]; then
    echo "ERROR: ${QUADLET_DIR} not found" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Extract the ## Volumes section from deployment.md.
#
# All checks (prose-sentinel and named-volume cross-check) are scoped to this
# section only — scanning the whole file would cause false positives if a future
# historical/ADR/migration section quotes the old stale claim (#11).
# ---------------------------------------------------------------------------
IN_VOLUMES_SECTION=0
VOLUMES_TABLE_CONTENT=""
while IFS= read -r line; do
    if echo "${line}" | grep -qE '^## Volumes'; then
        IN_VOLUMES_SECTION=1
        continue
    fi
    if [ "${IN_VOLUMES_SECTION}" -eq 1 ]; then
        if echo "${line}" | grep -qE '^(---$|## )'; then
            IN_VOLUMES_SECTION=0
            break
        fi
        VOLUMES_TABLE_CONTENT="${VOLUMES_TABLE_CONTENT}
${line}"
    fi
done < "${DEPLOYMENT_DOC}"

# ---------------------------------------------------------------------------
# Prose-sentinel check — scoped to ## Volumes section only (#11).
#
# Detects the stale claim "Adapter mounts are per-file inline binds (not the
# full factory-data.volume)" if it reappears inside the Volumes section.
# A future historical/ADR note elsewhere in the file does NOT trigger this.
# ---------------------------------------------------------------------------
ADAPTER_CLAIM_LINE=""
while IFS= read -r line; do
    if echo "${line}" | grep -qiE "adapter.*(per-file|per file).*bind.*(not.*factory-data\.volume|factory-data\.volume.*not)"; then
        ADAPTER_CLAIM_LINE="${line}"
        break
    fi
    if echo "${line}" | grep -qiE "(not.*factory-data\.volume|factory-data\.volume.*not).*adapter"; then
        ADAPTER_CLAIM_LINE="${line}"
        break
    fi
done <<< "${VOLUMES_TABLE_CONTENT}"

DRIFT_FOUND=0

if [ -n "${ADAPTER_CLAIM_LINE}" ]; then
    echo "" >&2
    echo "FAIL: deployment.md ## Volumes section claims adapters do NOT mount factory-data.volume, but quadlet templates show they do." >&2
    echo "" >&2
    echo "  Doc claim: ${ADAPTER_CLAIM_LINE}" >&2
    echo "" >&2
    echo "  Quadlet reality:" >&2
    for tmpl in "${QUADLET_DIR}"/factory-discord.container.tmpl "${QUADLET_DIR}"/factory-telegram.container.tmpl; do
        if [ -f "${tmpl}" ]; then
            vol_line="$(grep -E '^Volume=factory-data\.volume' "${tmpl}" || true)"
            if [ -n "${vol_line}" ]; then
                echo "    $(basename "${tmpl}"): ${vol_line}" >&2
            fi
        fi
    done
    DRIFT_FOUND=1
fi

# ---------------------------------------------------------------------------
# Cross-check: every Volume=<named-volume>: line in adapter templates must
# reference a named volume that appears in the Volumes table of deployment.md.
#
# We extract named-volume mounts (Volume=<name>.volume:...) from the adapter
# quadlet files and verify the volume name appears in the Volumes table.
# ---------------------------------------------------------------------------

# Collect named volumes used in adapter quadlet files
ADAPTER_TEMPLATES=()
for f in "${QUADLET_DIR}"/factory-discord.container.tmpl \
          "${QUADLET_DIR}"/factory-telegram.container.tmpl \
          "${QUADLET_DIR}"/factory-discord.container \
          "${QUADLET_DIR}"/factory-telegram.container; do
    [ -f "${f}" ] && ADAPTER_TEMPLATES+=("${f}")
done

# #10: missing adapter templates = script/config error, not a clean pass.
# If all four candidate files are absent the gate cannot verify anything —
# treat as exit 2 (script error) so CI does not silently go green.
if [ ${#ADAPTER_TEMPLATES[@]} -eq 0 ]; then
    echo "ERROR: no adapter quadlet files found in ${QUADLET_DIR}" >&2
    echo "  Expected at least one of: factory-discord.container{,.tmpl} factory-telegram.container{,.tmpl}" >&2
    echo "  If the files were renamed, update ADAPTER_TEMPLATES in tools/check_volumes_table.sh." >&2
    exit 2
fi

for tmpl in "${ADAPTER_TEMPLATES[@]}"; do
    while IFS= read -r vline; do
        # Named volumes look like: Volume=<name>.volume:<path>:...
        # (as opposed to inline binds: Volume=%h/... or Volume=/abs/path)
        if echo "${vline}" | grep -qE '^Volume=[^%/].*\.volume:'; then
            vol_name="$(echo "${vline}" | sed -E 's/^Volume=([^:]+\.volume):.*/\1/')"
            # Check if this volume name appears anywhere in the Volumes table
            if ! echo "${VOLUMES_TABLE_CONTENT}" | grep -qF "${vol_name}"; then
                echo "" >&2
                echo "FAIL: $(basename "${tmpl}") mounts '${vol_name}' but '${vol_name}' is not referenced in the Volumes table of deployment.md." >&2
                echo "  Quadlet line: ${vline}" >&2
                echo "  Fix: add a row for '${vol_name}' to the Volumes table in docs/architecture/deployment.md." >&2
                DRIFT_FOUND=1
            fi
        fi
    done < "${tmpl}"
done

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
if [ "${DRIFT_FOUND}" -ne 0 ]; then
    echo "" >&2
    echo "check_volumes_table: FAILED — deployment.md Volumes table does not match quadlet reality." >&2
    echo "Fix docs/architecture/deployment.md so the table and prose reflect actual Volume= directives." >&2
    exit 1
fi

echo "check_volumes_table: OK — Volumes table matches quadlet Volume= directives."
exit 0
