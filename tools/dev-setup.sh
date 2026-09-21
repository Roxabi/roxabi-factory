#!/usr/bin/env bash
# Bootstrap local dev after clone — SSOT command: .dev/stack.yml → commands.dev_setup
set -euo pipefail

YQ_VERSION="${YQ_VERSION:-4.44.6}"

install_yq() {
  if command -v yq >/dev/null 2>&1; then
    return 0
  fi
  local arch dest
  arch="$(uname -m)"
  case "$arch" in
    x86_64) arch=amd64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *)
      echo "yq: unsupported arch ${arch} — install manually: https://github.com/mikefarah/yq#install" >&2
      return 1
      ;;
  esac
  dest="${HOME}/.local/bin"
  mkdir -p "$dest"
  curl -fsSL \
    "https://github.com/mikefarah/yq/releases/download/v${YQ_VERSION}/yq_linux_${arch}" \
    -o "${dest}/yq"
  chmod +x "${dest}/yq"
  if ! command -v yq >/dev/null 2>&1; then
    echo "Add ${dest} to PATH, then re-run dev-setup (required for scripts/qg)" >&2
    return 1
  fi
}

echo "==> yq (stack.yml parser for scripts/qg)"
install_yq

echo "==> Python deps (uv sync)"
uv sync

echo "==> JS deps (bun install)"
if ! command -v bun >/dev/null 2>&1; then
  echo "bun not installed — https://bun.sh (see package.json packageManager)" >&2
  exit 2
fi
bun install --frozen-lockfile

echo "==> Git hooks (pre-commit + pre-push)"
# NOT `pre-commit install`: it refuses whenever core.hooksPath is set at any
# scope, which silently left this repo with zero installed hooks.
bash tools/install-hooks.sh

echo "✓ dev setup complete — run 'uv run pytest' to verify"
