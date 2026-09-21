# Builder python MUST satisfy pyproject requires-python (>=3.12,<3.13) — a
# 3.13+/3.14 base makes `uv sync` unable to build /app/.venv (staging red
# 2026-07-03, dependabot bump #2160). Dependabot: patch-only via ignore rule.
FROM python:3.12.13-slim AS builder

# Install system deps (git needed for GitHub-sourced Python deps)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.11.1 /uv /uvx /usr/local/bin/

WORKDIR /app

# Install dependencies. src/ must be present before `uv sync` — factory is
# installed editable, and uv only links src files that exist at sync time.
# Copying src/ after would leave the editable install pointing at an empty
# dist-info (ImportError: No module named 'factory' at runtime).
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY src/ src/
COPY deploy/quadlet.toml deploy/quadlet.toml
COPY deploy/quadlet/ deploy/quadlet/
RUN uv sync --frozen --no-dev

# ── Dashboard SPA builder (#1771) ───────────────────────────────────────────
FROM oven/bun:latest AS dashboard-builder
WORKDIR /app
COPY package.json bun.lock biome.json ./
COPY apps/dashboard-v2/package.json apps/dashboard-v2/
COPY packages/shared/package.json packages/shared/
COPY brand/ brand/
COPY packages/shared/ packages/shared/
COPY apps/dashboard-v2/ apps/dashboard-v2/
RUN bun install --frozen-lockfile
RUN bun run build:dashboard

# ── Slim service runtime (hub, telegram, discord) ───────────────────────────
# Pinned to linux/amd64 manifest digest of base-svc:latest (2026-06-28).
# Bump together with roxabi-container base-svc release and update this comment.
# NOTE: this is the amd64 child digest (valid — docker-bake.hcl is single-platform/amd64).
# Before adding a `platforms` entry to docker-bake.hcl, re-pin to the multi-arch INDEX digest:
#   docker buildx imagetools inspect --format '{{.Manifest.Digest}}' ghcr.io/roxabi/base-svc:latest
FROM ghcr.io/roxabi/base-svc@sha256:42b1d64e6e4a98d0840539aee73f3c3a43ab46683c9d6fe777b5cc725f5629c7 AS svc-runtime

USER root

# UID 1500 pinned per ADR-053 (Quadlet container UID stability)
RUN useradd -u 1500 -m factory

COPY --from=builder --chown=factory:factory /app /app
COPY --from=dashboard-builder --chown=factory:factory /app/apps/dashboard-v2/dist /app/apps/dashboard-v2/dist

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH"

USER factory

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD factory config validate || exit 1

# ── Agent runtime (clipool — full gh_token tooling) ───────────────────────────
# Pinned to linux/amd64 manifest digest of base:latest (2026-06-28).
# Bump together with roxabi-container base release and update this comment.
# NOTE: amd64 child digest (valid — single-platform build). Re-pin to the multi-arch INDEX
# digest before adding `platforms` to docker-bake.hcl (see base-svc note above).
FROM ghcr.io/roxabi/base@sha256:dab0e1477f5e6cea6d8090e0f15421cfb2cbcd237dace12717afba5d9be14bbc AS agent-runtime

USER root

# socat — required by the gh_token shim scripts (git-credential-factory-gh + factory-gh)
# to dial the dispenser Unix socket. Smallest dep that handles UNIX-CONNECT cleanly;
# BSD nc -U fallback in the shims is for hosts where socat is unavailable.
# git — two consumers: (1) runtime — the omp ownership probe shells out to
# `git rev-parse HEAD` (git_ownership_probe.py); (2) build — the omp_rpc `git+URL`
# install below. Do NOT drop git while either consumer exists.
RUN apt-get update \
 && apt-get install -y --no-install-recommends socat git \
 && rm -rf /var/lib/apt/lists/*

# UID 1500 pinned per ADR-053 (Quadlet container UID stability)
RUN useradd -u 1500 -m factory \
 && mkdir -p /home/factory/projects \
              /home/factory/.claude/projects \
              /home/factory/.claude/plugins \
              /home/factory/.claude/skills \
              /home/factory/.claude/shared \
              /home/factory/.claude/.git

# ── omp binary (#1812 / #1867) ─────────────────────────────────────────────────
# Pulled from the pinned scratch carrier (deploy/omp-base/ — tag = omp version,
# immutable; the carrier never runs directly). _rpc_bridge.py re-verifies the
# sha256 of /opt/omp/omp at startup, so a carrier bump must land together with
# the _PINNED_SHA256 update. Placed before the builder COPY so code-only builds
# keep this pinned layer cached.
COPY --from=ghcr.io/roxabi/factory-omp-base:17.2.12 /opt/omp/omp /opt/omp/omp

COPY --from=builder --chown=factory:factory /app /app

# ── factory-gh helper user (#1078) ─────────────────────────────────────────────
# uid 1501 ≠ 1500 (factory's uid) so the token cache file
# /run/factory-gh-token/token.json (mode 0600 owned by 1501) is unreadable from
# inside the Claude subprocess. Both users share group factory-tokenuser
# (gid 1502) so the credential helper + factory-gh shim can connect to the
# dispenser socket (mode 0660 group rw).
RUN groupadd -g 1502 factory-tokenuser \
 && useradd -u 1501 -M -d /nonexistent -s /usr/sbin/nologin -G factory-tokenuser factory-gh \
 && usermod -aG factory-tokenuser factory

# ── gh_token tools (#1078) ───────────────────────────────────────────────────
# Copy helper + dispenser Python modules, and shell shims when T6/T7 land.
# The conditional chmod/ln blocks are no-ops before those tasks ship.
RUN mkdir -p /opt/factory-gh /etc/factory
COPY --chown=root:root src/factory/tools/gh_token/ /opt/factory-gh/
RUN chmod 0755 /opt/factory-gh/*.py 2>/dev/null || true \
 && { [ -f /opt/factory-gh/git-credential-factory-gh ] && chmod 0755 /opt/factory-gh/git-credential-factory-gh || true; } \
 && { [ -f /opt/factory-gh/factory-gh ] && chmod 0755 /opt/factory-gh/factory-gh && ln -sf /opt/factory-gh/factory-gh /usr/local/bin/factory-gh && ln -sf /opt/factory-gh/factory-gh /usr/local/bin/gh || true; }
COPY --chown=root:root deploy/factory-gh/git.config.tmpl /etc/factory/git.config.tmpl
COPY --chown=root:root deploy/factory-gh/hooks/ /opt/factory-gh/hooks/
RUN chmod 0755 /opt/factory-gh/hooks/prepare-commit-msg

# Take `gh` off PATH (AC#5 from #1078): the base image ships /usr/bin/gh which
# would let any process — including the Claude subprocess — invoke gh directly
# and inherit the token if one ever leaked into env. Move it to a non-PATH
# location and point FACTORY_GH_BIN at it so the factory-gh shim still finds it
# without anyone else's `command -v gh` succeeding. /usr/local/bin/gh is a
# shim alias (→ factory-gh) so callers that hardcode `gh` also route through the
# dispenser; the shim's FACTORY_GH_BIN-first resolution guards against recursion.
RUN test -x /usr/bin/gh \
 && mv /usr/bin/gh /opt/factory-gh/gh \
 && chmod 0755 /opt/factory-gh/gh \
 || true
ENV FACTORY_GH_BIN=/opt/factory-gh/gh

# ── omp_rpc Python package (#1871) ─────────────────────────────────────────────
# omp_rpc is deliberately kept OUT of uv.lock (alpha lib, pin-by-SHA pattern —
# #1807/#1810). Install at image build time from the pinned commit that matches
# OMP_VERSION=v17.2.12 / factory-omp-base:17.2.12. The commit SHA is locked here;
# a version bump must update deploy/omp-base/Containerfile OMP_VERSION+OMP_SHA256,
# src/factory/adapters/omp/_rpc_digest.py _PINNED_SHA256, AND this pin — in lockstep.
# uv is not present in agent-runtime (only in builder); bring the static binary from
# the official astral-sh image so we can pip-install into /app/.venv without touching
# the project lockfile.
COPY --from=ghcr.io/astral-sh/uv:0.11.1 /uv /usr/local/bin/uv
RUN uv pip install --python /app/.venv/bin/python \
      "git+https://github.com/can1357/oh-my-pi@45e12e5bb758198a920c6070e7e64cb33b21beac#subdirectory=python/omp-rpc"
# Build-time import smoke — fails the image build if omp_rpc is mis-installed or
# the subdirectory path changes upstream. Closes the "declared but never importable" gap.
RUN /app/.venv/bin/python -c "import omp_rpc"

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH"

USER factory

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD factory config validate || exit 1
