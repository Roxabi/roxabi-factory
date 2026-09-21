#!/usr/bin/env bash
# check_secrets_source.sh — secret-source ↔ on-disk integrity gate (host-dependent).
#
# For every secret in deploy/secrets-policy.toml with a file-based source,
# verify the source file exists under the factory data dir. Catches the class
# of incident where a Podman secret survives but its source file is lost
# (e.g. litellm-key.tok during the 2026-06-11/12 overhaul) — leaving the secret
# unrecoverable on the next rotation (#1901).
#
# HOST-DEPENDENT — SKIP when data dir absent:
#   The factory data dir (~/.roxabi/factory/) is not present on CI runners or
#   dev machines without a deploy. When it is absent this gate SKIPS (exit 0).
#   Listed in stack.yml ci/pre-push for wiring parity; enforcement is on
#   operator hosts with a real factory data dir.
#
# Policy semantics (deploy/secrets-policy.toml `policy =`):
#   nats-seed / required  → source file MUST exist (HARD FAIL if missing)
#   optional              → WARN if missing (unrecoverable after rotation), no fail
#   generated             → source "n/a", skipped (generated at install time)
#
# ENVIRONMENT OVERRIDES
#   POLICY_TOML        — default: deploy/secrets-policy.toml
#   FACTORY_DATA_DIR   — default: $HOME/.roxabi/factory
#
# EXIT-CODE CONTRACT (tools/AGENTS.md): 0 = clean, 1 = violations, 2 = setup error.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || { echo "ERROR: not a git repository" >&2; exit 2; }
cd "$REPO_ROOT"

POLICY_TOML="${POLICY_TOML:-deploy/secrets-policy.toml}"
DATA_DIR="${FACTORY_DATA_DIR:-$HOME/.roxabi/factory}"

if [[ ! -f "$POLICY_TOML" ]]; then
    echo "ERROR: policy file not found: $POLICY_TOML" >&2
    exit 2
fi
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import tomllib' >/dev/null 2>&1; then
    echo "ERROR: python3 with tomllib (Python 3.11+) required" >&2
    exit 2
fi

# Skip gracefully when the operator data dir is absent (CI runner / dev box).
if [[ ! -d "$DATA_DIR" ]]; then
    echo "check_secrets_source: SKIP — factory data dir ($DATA_DIR) not present (CI/dev runner); source-existence not checked"
    exit 0
fi

fail=0

# Emit one "<name>\t<policy>\t<source>" line per secret with a file-based source.
while IFS=$'\t' read -r name policy source; do
    if [[ -z "$name" ]]; then
        continue
    fi
    src_path="$DATA_DIR/$source"
    if [[ -f "$src_path" ]]; then
        continue
    fi
    case "$policy" in
        nats-seed|required)
            echo "FAIL: $name (policy=$policy) source missing: $src_path" >&2
            echo "::error file=deploy/secrets-policy.toml::secret $name source file missing on host: $src_path"
            fail=1
            ;;
        optional)
            echo "WARN: optional secret $name source missing: $src_path — unrecoverable after next rotation" >&2
            ;;
        *)
            echo "WARN: $name has unrecognized policy '$policy' — source-missing treated as non-fatal" >&2
            ;;
    esac
done < <(python3 - "$POLICY_TOML" <<'PYEOF'
import sys
import tomllib

with open(sys.argv[1], "rb") as f:
    data = tomllib.load(f)

for name, attrs in data.get("secret", {}).items():
    source = attrs.get("source", "n/a")
    if source == "n/a":
        continue
    print(f"{name}\t{attrs.get('policy', '')}\t{source}")
PYEOF
)

if [[ "$fail" -ne 0 ]]; then
    echo "" >&2
    echo "check_secrets_source: required secret source files missing on host (see above)." >&2
    echo "Restore the source file under $DATA_DIR, or flip the secret to policy=optional if non-vital." >&2
    exit 1
fi

echo "check_secrets_source: all required secret sources present — OK"
exit 0
