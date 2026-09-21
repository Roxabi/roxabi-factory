#!/usr/bin/env bash
# check_secrets_drift.sh — secrets set-membership consistency gate.
#
# OVERVIEW
# --------
# Three orthogonal checks that together guarantee the secret declarations in
# deploy/quadlet.toml, deploy/quadlet/*.container(.tmpl), the generated
# secrets-manifest.sh, and deploy/nats/acl-matrix.json all agree.
#
# CHECK (a) — Unit ↔ quadlet.toml
#   Every Secret= name in deploy/quadlet/*.container(.tmpl) that is owned by
#   the factory scope MUST have a matching entry in quadlet.toml
#   [component.<name>].required_secrets.  A unit secret with no declared entry
#   is a HARD FAIL.
#
# CHECK (b) — quadlet.toml ↔ secrets-manifest.sh
#   The set of unique secret names declared across all quadlet.toml
#   required_secrets arrays MUST equal the set of keys in the generated
#   deploy/generated/secrets-manifest.sh SECRET_SOURCES map.  A mismatch means
#   the manifest is stale and must be regenerated with:
#     python3 tools/emit_secrets_manifest.py
#
# CHECK (c) — NATS seeds ↔ acl-matrix identities (factory + container scope)
#   Bidirectional equality check (spec #9 "≠" intent):
#
#   Forward: every factory-nats-* secret in quadlet.toml required_secrets MUST
#     correspond to an acl-matrix identity where BOTH:
#       owner  == "factory"
#       deploy.type == "container"
#     (catches a quadlet secret with no backing identity)
#
#   Reverse: every acl-matrix identity with owner=="factory" &&
#     deploy.type=="container" whose deploy.secret is factory-nats-* MUST
#     appear in quadlet.toml required_secrets.
#     (catches a factory NATS identity whose secret is declared nowhere in a unit)
#
#   The owner=="factory" && deploy.type=="container" filter makes the REVERSE
#   direction load-bearing — it prevents false-positives from voicecli-nats-*
#   and other external identities present in the merged acl-matrix.
#   The auth bundle secret (factory-nats-auth) is excluded from both directions:
#   it is the NATS server auth.conf, not an NKey identity seed.
#
# CHECK (d) — quadlet.toml required_secrets ↔ secrets-policy.toml [secret.*]
#   Bidirectional: every required_secret must have a [secret.<name>] policy
#   entry (catches a required secret with no declared source), and every
#   policy entry must be referenced by some component (catches an orphan
#   policy entry with no consumer).
#
# OPTIONAL SECRETS
#   Secrets with policy=optional (factory-gh-pem, factory-claude-oauth) are
#   logged as SKIP and never cause a failure.  The optional set is derived by
#   parsing deploy/secrets-policy.toml — no hardcoding required.
#
# PARSE-RULE SOURCE OF TRUTH
#   Secret name = Secret= value split on the first comma, leading/trailing
#   whitespace stripped.  This mirrors tools/emit_secrets_manifest.py's
#   parse_unit_secret_names() contract:
#     name = value.split(",")[0].strip()
#   The {{bot_secrets}} template placeholder is excluded.
#   Do NOT change the parse logic here without a matching change in
#   tools/emit_secrets_manifest.py.
#
# EXIT-CODE CONTRACT (tools/AGENTS.md):
#   0 = ran cleanly, no violations
#   1 = one or more violations found (merge-blocking)
#   2 = script setup error (missing dep / missing required file)
#
# ENVIRONMENT OVERRIDES
#   QUADLET_TOML     — default: deploy/quadlet.toml
#   POLICY_TOML      — default: deploy/secrets-policy.toml
#   QUADLET_DIR      — default: deploy/quadlet
#   MANIFEST_SH      — default: deploy/generated/secrets-manifest.sh
#   ACL_MATRIX       — default: deploy/nats/acl-matrix.json
#
# RUN LOCALLY
#   bash tools/check_secrets_drift.sh
# RUN IN CI
#   check-secrets-drift step in .github/workflows/ci.yml

set -euo pipefail

# ── REPO ROOT ─────────────────────────────────────────────────────────────────
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || { echo "ERROR: not a git repository" >&2; exit 2; }
cd "$REPO_ROOT"

# ── PATHS ─────────────────────────────────────────────────────────────────────
QUADLET_TOML="${QUADLET_TOML:-deploy/quadlet.toml}"
POLICY_TOML="${POLICY_TOML:-deploy/secrets-policy.toml}"
QUADLET_DIR="${QUADLET_DIR:-deploy/quadlet}"
MANIFEST_SH="${MANIFEST_SH:-deploy/generated/secrets-manifest.sh}"
ACL_MATRIX="${ACL_MATRIX:-deploy/nats/acl-matrix.json}"

BOT_SECRETS_PLACEHOLDER="{{bot_secrets}}"

# ── GUARDS ────────────────────────────────────────────────────────────────────
for req in "$QUADLET_TOML" "$POLICY_TOML" "$MANIFEST_SH" "$ACL_MATRIX"; do
    if [[ ! -f "$req" ]]; then
        echo "ERROR: required file not found: $req" >&2
        exit 2
    fi
done
if [[ ! -d "$QUADLET_DIR" ]]; then
    echo "ERROR: quadlet directory not found: $QUADLET_DIR" >&2
    exit 2
fi
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found on PATH" >&2
    exit 2
fi
if ! python3 -c "import tomllib" 2>/dev/null; then
    echo "ERROR: python3 tomllib not available (requires Python 3.11+)" >&2
    exit 2
fi
if ! command -v jq &>/dev/null; then
    echo "ERROR: jq not found on PATH" >&2
    exit 2
fi

fail=0

# ── HELPERS ───────────────────────────────────────────────────────────────────

# parse_unit_secrets <unit_file>
# Emit one secret name per line using tools/emit_secrets_manifest.py's
# parse_unit_secret_names() — single parse rule, no PCRE, no bash reimplementation.
parse_unit_secrets() {
    local unit_file="$1"
    python3 "${REPO_ROOT}/tools/emit_secrets_manifest.py" --list-unit-secrets "${unit_file}"
}

# load_quadlet_required_secrets
# Emit the unique set of secret names from quadlet.toml required_secrets,
# one per line, sorted.
load_quadlet_required_secrets() {
    python3 - "$QUADLET_TOML" <<'PYEOF'
import sys, tomllib, pathlib
path = pathlib.Path(sys.argv[1])
with path.open("rb") as f:
    data = tomllib.load(f)
seen = set()
for comp in data.get("component", {}).values():
    for s in comp.get("required_secrets", []):
        seen.add(s)
for name in sorted(seen):
    print(name)
PYEOF
}

# load_optional_secrets
# Emit secret names whose policy == "optional" in secrets-policy.toml,
# one per line.  These are skipped in check (c) and logged, not failed.
load_optional_secrets() {
    python3 - "$POLICY_TOML" <<'PYEOF'
import sys, tomllib, pathlib
path = pathlib.Path(sys.argv[1])
with path.open("rb") as f:
    data = tomllib.load(f)
for name, attrs in data.get("secret", {}).items():
    if attrs.get("policy") == "optional":
        print(name)
PYEOF
}

# load_policy_secret_names
# Emit every secret name with a [secret.<name>] entry in secrets-policy.toml,
# one per line, sorted.
#
# NOTE: [secret-class.*] sections (e.g. [secret-class.bot-token]) are parsed
# by tomllib under data["secret-class"], a separate top-level key from
# data["secret"].  data.get("secret", {}) therefore naturally excludes all
# class declarations — they never enter POLICY_SET and are exempt from the
# check (d) bidirectional assertion by TOML structure, not by explicit filter.
load_policy_secret_names() {
    python3 - "$POLICY_TOML" <<'PYEOF'
import sys, tomllib, pathlib
path = pathlib.Path(sys.argv[1])
with path.open("rb") as f:
    data = tomllib.load(f)
for name in sorted(data.get("secret", {}).keys()):
    print(name)
PYEOF
}

# load_manifest_secret_names
# Emit secret names declared in secrets-manifest.sh (lines of the form
#     [name]="..."
# inside the SECRET_SOURCES declare -A block), one per line, sorted.
# Uses python3 to avoid PCRE / non-GNU-grep dependency.
load_manifest_secret_names() {
    python3 - "$MANIFEST_SH" <<'PYEOF'
import sys, re, pathlib
text = pathlib.Path(sys.argv[1]).read_text()
seen = set()
for m in re.finditer(r'^\s+\[([^\]]+)\]="[^"]*"', text, re.MULTILINE):
    seen.add(m.group(1))
for name in sorted(seen):
    print(name)
PYEOF
}

# load_acl_factory_container_secrets
# Emit the deploy.secret values from acl-matrix.json identities where:
#   .owner == "factory"  AND  .deploy.type == "container"
# This correctly excludes voicecli-nats-*, llm-worker, image-worker, etc.
load_acl_factory_container_secrets() {
    jq -r '
        .identities
        | to_entries[]
        | select(
            .value.owner == "factory"
            and .value.deploy.type == "container"
        )
        | .value.deploy.secret
    ' "$ACL_MATRIX" | sort -u
}

# ── BUILD LOOKUP SETS ─────────────────────────────────────────────────────────

# Quadlet required_secrets (sorted array)
mapfile -t QUADLET_SECRETS < <(load_quadlet_required_secrets)

# Optional secrets (associative set for fast lookup)
declare -A OPTIONAL_SET=()
while IFS= read -r name; do
    OPTIONAL_SET["$name"]=1
done < <(load_optional_secrets)

# Policy-declared secret names (associative set for fast lookup)
declare -A POLICY_SET=()
while IFS= read -r name; do
    POLICY_SET["$name"]=1
done < <(load_policy_secret_names)

# Manifest secret names (sorted array)
mapfile -t MANIFEST_SECRETS < <(load_manifest_secret_names)

# ACL factory+container secret names (sorted array)
mapfile -t ACL_SECRETS < <(load_acl_factory_container_secrets)

# ── CHECK (a) — Unit Secret= ⊆ quadlet.toml required_secrets ─────────────────
# Build a lookup map of quadlet.toml declared secrets.
declare -A QUADLET_SET=()
for s in "${QUADLET_SECRETS[@]}"; do
    QUADLET_SET["$s"]=1
done

unit_violations=()
for unit_file in "$QUADLET_DIR"/*.container "$QUADLET_DIR"/*.container.tmpl; do
    [[ -f "$unit_file" ]] || continue
    while IFS= read -r secret_name; do
        if [[ -z "${QUADLET_SET[$secret_name]+_}" ]]; then
            unit_violations+=("$(basename "$unit_file"): Secret=$secret_name — no required_secrets entry in quadlet.toml")
        fi
    done < <(parse_unit_secrets "$unit_file")
done

if [[ ${#unit_violations[@]} -gt 0 ]]; then
    echo "" >&2
    echo "FAIL (a): unit Secret= names not declared in quadlet.toml required_secrets:" >&2
    for v in "${unit_violations[@]}"; do
        echo "  $v" >&2
        filepath="${v%%:*}"
        echo "::error file=deploy/quadlet/${filepath}::secret in unit not declared in quadlet.toml required_secrets — add it or remove the Secret= line"
    done
    fail=1
else
    echo "check_secrets_drift (a): unit Secret= names all declared in quadlet.toml — OK"
fi

# ── CHECK (b) — quadlet.toml required_secrets == secrets-manifest.sh ──────────
# Set difference in both directions: missing from manifest, and extra in manifest.

declare -A MANIFEST_SET=()
for s in "${MANIFEST_SECRETS[@]}"; do
    MANIFEST_SET["$s"]=1
done

in_quadlet_not_manifest=()
for s in "${QUADLET_SECRETS[@]}"; do
    if [[ -z "${MANIFEST_SET[$s]+_}" ]]; then
        in_quadlet_not_manifest+=("$s")
    fi
done

in_manifest_not_quadlet=()
for s in "${MANIFEST_SECRETS[@]}"; do
    if [[ -z "${QUADLET_SET[$s]+_}" ]]; then
        in_manifest_not_quadlet+=("$s")
    fi
done

if [[ ${#in_quadlet_not_manifest[@]} -gt 0 || ${#in_manifest_not_quadlet[@]} -gt 0 ]]; then
    echo "" >&2
    echo "FAIL (b): quadlet.toml required_secrets / secrets-manifest.sh are out of sync:" >&2
    for s in "${in_quadlet_not_manifest[@]}"; do
        echo "  in quadlet.toml but not in manifest: $s" >&2
    done
    for s in "${in_manifest_not_quadlet[@]}"; do
        echo "  in manifest but not in quadlet.toml: $s" >&2
    done
    echo "  Regenerate: python3 tools/emit_secrets_manifest.py" >&2
    echo "::error file=deploy/generated/secrets-manifest.sh::stale secrets manifest — run python3 tools/emit_secrets_manifest.py and commit"
    fail=1
else
    echo "check_secrets_drift (b): quadlet.toml required_secrets == secrets-manifest.sh — OK"
fi

# ── CHECK (c) — factory-nats-* seeds ↔ acl-matrix factory+container ids ──────
# Bidirectional: BOTH directions must hold.
#
# Forward:  every factory-nats-* secret in quadlet.toml required_secrets must
#           exist as deploy.secret of an acl-matrix identity with
#           owner=="factory" && deploy.type=="container".
#           (catches a quadlet secret with no backing identity)
#
# Reverse:  every acl-matrix identity with owner=="factory" &&
#           deploy.type=="container" whose deploy.secret matches factory-nats-*
#           must appear in quadlet.toml required_secrets.
#           (catches a factory NATS identity whose secret is declared nowhere)
#
# The owner=="factory" && type=="container" filter makes the REVERSE direction
# load-bearing: it prevents false-positives from voicecli-nats-* / external
# identities that are also present in the merged acl-matrix.
#
# factory-nats-auth is excluded from both directions: it is the NATS server
# auth.conf bundle, not an NKey identity seed; the NATS server has no
# acl-matrix identity entry.
# Optional secrets → SKIP (logged, not failed).

declare -A ACL_SET=()
for s in "${ACL_SECRETS[@]}"; do
    ACL_SET["$s"]=1
done

nats_seed_violations=()
nats_reverse_violations=()
nats_skip_optional=()

# Forward direction: quadlet factory-nats-* ⊆ ACL_SET
for s in "${QUADLET_SECRETS[@]}"; do
    # Only inspect factory-nats-* seeds (not auth bundle, not non-NATS secrets)
    [[ "$s" == factory-nats-* ]] || continue
    [[ "$s" == "factory-nats-auth" ]] && continue  # auth.conf bundle, not an identity seed

    # Optional secrets are skipped (logged, not failed)
    if [[ -n "${OPTIONAL_SET[$s]+_}" ]]; then
        nats_skip_optional+=("$s")
        continue
    fi

    if [[ -z "${ACL_SET[$s]+_}" ]]; then
        nats_seed_violations+=("$s")
    fi
done

# Reverse direction: ACL factory+container factory-nats-* ⊆ QUADLET_SET
for s in "${ACL_SECRETS[@]}"; do
    # Only inspect factory-nats-* names (filter already ensures owner==factory && type==container)
    [[ "$s" == factory-nats-* ]] || continue
    [[ "$s" == "factory-nats-auth" ]] && continue  # excluded: auth.conf bundle, not a seed

    # Optional secrets are skipped
    if [[ -n "${OPTIONAL_SET[$s]+_}" ]]; then
        continue
    fi

    if [[ -z "${QUADLET_SET[$s]+_}" ]]; then
        nats_reverse_violations+=("$s")
    fi
done

for s in "${nats_skip_optional[@]}"; do
    echo "check_secrets_drift (c): SKIP optional secret: $s"
done

if [[ ${#nats_seed_violations[@]} -gt 0 || ${#nats_reverse_violations[@]} -gt 0 ]]; then
    echo "" >&2
    if [[ ${#nats_seed_violations[@]} -gt 0 ]]; then
        echo "FAIL (c) forward: factory-nats-* seeds in quadlet.toml have no matching acl-matrix identity (owner=factory, deploy.type=container):" >&2
        for s in "${nats_seed_violations[@]}"; do
            echo "  $s" >&2
            echo "::error file=deploy/nats/acl-matrix.json::NATS seed $s has no factory container identity in acl-matrix.json — add the identity or remove the secret"
        done
    fi
    if [[ ${#nats_reverse_violations[@]} -gt 0 ]]; then
        echo "FAIL (c) reverse: acl-matrix factory+container identities have no matching quadlet.toml required_secrets entry:" >&2
        for s in "${nats_reverse_violations[@]}"; do
            echo "  $s" >&2
            echo "::error file=deploy/quadlet.toml::acl-matrix factory container identity $s has no required_secrets entry in quadlet.toml — add it or remove the identity"
        done
    fi
    fail=1
else
    echo "check_secrets_drift (c): factory-nats-* seeds ↔ acl-matrix factory+container identities — OK (bidirectional)"
fi

# ── CHECK (d) — quadlet.toml required_secrets ↔ secrets-policy.toml [secret.*] ─
# Bidirectional set-membership:
#   Forward:  every required_secret in quadlet.toml MUST have a [secret.<name>]
#             entry in secrets-policy.toml (catches a required secret with no
#             declared policy/source — the litellm-key-lost class).
#   Reverse:  every [secret.<name>] in secrets-policy.toml MUST appear in some
#             component required_secrets (catches an orphan policy entry with
#             no consumer — the factory-nats-auth class).
#
# Bot-token class exemption is STRUCTURAL, not runtime: secrets matching a
# [secret-class.*] pattern in secrets-policy.toml are rendered dynamically at
# install by tools/render_quadlet.py and never appear in required_secrets, so
# they are absent from QUADLET_SECRETS (forward direction unaffected).  The
# class entry is parsed by tomllib under data["secret-class"] — a separate
# top-level key from data["secret"] — so it never enters POLICY_SET (reverse
# direction unaffected).  TOML namespacing therefore isolates [secret-class.*]
# from check (d) entirely; no per-secret exemption guard is required here.

in_quadlet_not_policy=()
for s in "${QUADLET_SECRETS[@]}"; do
    if [[ -z "${POLICY_SET[$s]+_}" ]]; then
        in_quadlet_not_policy+=("$s")
    fi
done

in_policy_not_quadlet=()
for s in "${!POLICY_SET[@]}"; do
    if [[ -z "${QUADLET_SET[$s]+_}" ]]; then
        in_policy_not_quadlet+=("$s")
    fi
done

if [[ ${#in_quadlet_not_policy[@]} -gt 0 || ${#in_policy_not_quadlet[@]} -gt 0 ]]; then
    echo "" >&2
    echo "FAIL (d): quadlet.toml required_secrets / secrets-policy.toml [secret.*] out of sync:" >&2
    for s in "${in_quadlet_not_policy[@]}"; do
        echo "  required_secret with no secrets-policy.toml entry: $s" >&2
        echo "::error file=deploy/secrets-policy.toml::required secret $s has no [secret.$s] policy entry — add it (policy + source)"
    done
    for s in "${in_policy_not_quadlet[@]}"; do
        echo "  orphan policy entry not referenced by any required_secrets: $s" >&2
        echo "::error file=deploy/secrets-policy.toml::policy entry $s is referenced by no component required_secrets — remove it or wire the secret"
    done
    fail=1
else
    echo "check_secrets_drift (d): required_secrets ↔ secrets-policy.toml [secret.*] — OK (bidirectional)"
fi

# ── SUMMARY ───────────────────────────────────────────────────────────────────
if [[ "$fail" -eq 0 ]]; then
    echo ""
    echo "check_secrets_drift: all checks passed — OK"
fi

exit "$fail"
