#!/usr/bin/env bash
# #1420 guardrail — TOML bot sections are seed-only; no runtime roster reads allowed.
#
# Three checks (SC#7a, SC#7b, SC#9):
#   (SC#7a) No raw dict-index reads of [[telegram.bots]] / [[discord.bots]] at runtime
#           Pattern: raw["telegram"]["bots"], raw['telegram']['bots'],
#                    raw["discord"]["bots"], raw['discord']['bots'],
#                    auth_block.get("telegram_bots", auth_block.get("discord_bots"
#   (SC#7b) No raw dict-index reads of [[auth.telegram_bots]] / [[auth.discord_bots]]
#           Pattern: raw_config["auth"]["telegram_bots"], raw_config['auth']['telegram_bots'],
#                    raw_config["auth"]["discord_bots"], raw_config['auth']['discord_bots']
#   (SC#9)  load_multibot_config() must not be called from boot paths
#           (auth_seeding.py, factory/agent_factory.py) — replaced by BotStore.get_all()
#
# Exclusions:
#   src/factory/agent_cmd/bots/init.py    — sanctioned seed consumer (factory bot init)
#   src/factory/config.py                 — parser internals (sections are still parsed for compat)
#
# Sanctioned load_multibot_config() caller NOT in SC#9 target list:
#   src/factory/cli.py                    — `lyra config validate` CLI introspection only (¬boot path)
#
# Run locally: bash tools/check_no_runtime_toml_bots.sh
# Run in CI:   quality gate (no_runtime_toml_bots in .dev/stack.yml)

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

fail=0

# ── SC#7a: no runtime roster reads of [[telegram.bots]] / [[discord.bots]] ───
# Guards against re-introducing raw["telegram"]["bots"] / raw["discord"]["bots"]
# or .get("telegram_bots") / .get("discord_bots") outside the seed-only consumer.
violations_7a=$(grep -rn \
    'raw\[["'"'"']\(telegram\|discord\)["'"'"']\]\[["'"'"']bots["'"'"']\]\|auth_block\.get("telegram_bots"\|auth_block\.get("discord_bots"' \
    src/factory/ \
    --include="*.py" \
    | grep -v 'src/factory/agent_cmd/bots/init\.py:' \
    | grep -v 'src/factory/config\.py:' \
    || true)
if [ -n "$violations_7a" ]; then
    echo "FAIL (SC#7a): runtime TOML bot roster read detected — [[telegram.bots]] / [[discord.bots]] are seed-only (#1420)." >&2
    echo "  Use BotStore.get_all() at runtime instead." >&2
    printf '%s\n' "$violations_7a" | sed 's/^/  /' >&2
    fail=1
fi

# ── SC#7b: no runtime roster reads of [[auth.telegram_bots]] / [[auth.discord_bots]] ──
# Guards against raw_config["auth"]["telegram_bots"] / raw_config["auth"]["discord_bots"]
# outside the seed-only consumer.
violations_7b=$(grep -rn \
    'raw_config\[["'"'"']auth["'"'"']\]\[["'"'"']\(telegram_bots\|discord_bots\)["'"'"']\]' \
    src/factory/ \
    --include="*.py" \
    | grep -v 'src/factory/agent_cmd/bots/init\.py:' \
    | grep -v 'src/factory/config\.py:' \
    || true)
if [ -n "$violations_7b" ]; then
    echo "FAIL (SC#7b): runtime TOML bot roster read detected — [[auth.telegram_bots]] / [[auth.discord_bots]] are seed-only (#1420)." >&2
    echo "  Use BotStore.get_all() at runtime instead." >&2
    printf '%s\n' "$violations_7b" | sed 's/^/  /' >&2
    fail=1
fi

# ── SC#9: load_multibot_config() must not be called from boot paths ───────────
# Hub standalone (auth_seeding.py) and unified (factory/agent_factory.py, which owns
# _init_bot_auths_and_agents after #1283 Phase 6) must source the bot roster from
# BotStore.get_all(), not load_multibot_config(raw_config).
violations_9=$(grep -n 'load_multibot_config(' \
    src/factory/bootstrap/auth_seeding.py \
    src/factory/bootstrap/factory/agent_factory.py \
    2>/dev/null || true)
if [ -n "$violations_9" ]; then
    echo "FAIL (SC#9): load_multibot_config() called in boot path — boot paths must use BotStore.get_all() (#1420)." >&2
    echo "  Files: src/factory/bootstrap/auth_seeding.py, src/factory/bootstrap/factory/agent_factory.py" >&2
    printf '%s\n' "$violations_9" | sed 's/^/  /' >&2
    fail=1
fi

exit $fail
