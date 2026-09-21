"""E2E parity test: boots nats-server with rendered auth.conf.

Verifies identity authorization end-to-end. Matrix-driven live ACL round-trip
coverage (#2247) lives here too — see tests/scripts/test_acl_canonical_llm.py
and tests/scripts/test_acl_gh_helper.py for the static (no live server)
per-identity string-membership layer this file complements.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Generator, NamedTuple

import pytest


class NatsServerEndpoints(NamedTuple):
    client_url: str
    monitor_url: str


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

NATS_AVAILABLE = shutil.which("nats-server") is not None
NK_AVAILABLE = shutil.which("nk") is not None

_nats_py_available = False
try:
    import nats as _nats_module  # noqa: F401  # pyright: ignore[reportUnusedImport]

    _nats_py_available = True
except ImportError:
    pass

NATS_PY_AVAILABLE: bool = _nats_py_available


# ── CI hard-fail guard (#2247, N6) ──────────────────────────────────────────
# Inside GitHub Actions, a missing binary/import here means a broken CI
# environment, not an intentionally minimal dev machine — fail collection
# outright instead of letting the per-test `skipif(not NATS_PY_AVAILABLE)`
# decorators below quietly report "N skipped". `nats-py` is included because
# it is a hard transitive dependency (arrives via packages/roxabi-nats, see
# pyproject.toml), not an optional one.
#
# Keyed on GITHUB_ACTIONS rather than the generic CI precedent used by
# tests/integration/test_voice_routing.py: .dev/stack.yml's pytest_smoke
# pre-push gate imports this module locally (to inspect markers) even though
# none of its tests carry the `smoke` marker, so this guard also executes on
# every local pre-push run. A dev shell that happens to export a generic
# CI=true but lacks nats-server/nk locally would otherwise hard-fail
# unexpectedly. GITHUB_ACTIONS=true is set only by actual GitHub runners.
#
# The decision itself (not just the missing-deps list) is factored into a
# pure function so it can be unit-tested directly below
# (test_ci_hardfail_guard_*). Enforcement lives in tests/scripts/conftest.py
# (pytest_runtest_setup on the first live_acl test) — the tests job excludes
# live_acl (no nats-server/nk), so import must not hard-fail there (#2251, N6).
def _ci_hardfail_missing_deps(
    *,
    github_actions: bool,
    nats_available: bool,
    nk_available: bool,
    nats_py_available: bool,
) -> list[str]:
    """Names missing when the guard should hard-fail; `[]` means don't fail.

    Returns `[]` whenever `github_actions` is False, regardless of which
    deps are available — collection-time skip behavior is preserved outside
    GitHub Actions (see Judgment Call above).
    """
    if not github_actions:
        return []
    return [
        name
        for name, available in (
            ("nats-server", nats_available),
            ("nk", nk_available),
            ("nats-py", nats_py_available),
        )
        if not available
    ]


# ── Matrix-driven parametrization (#2247) ───────────────────────────────────
# Sourced directly from the SSoT (deploy/nats/acl-matrix.json), not from the
# v3-current.json snapshot the live nats-server below boots from — see Design
# Note item 1 in artifacts/specs/2247-matrix-driven-live-acl-round-trip-spec.mdx.
# test_acl_matrix_v3_current_in_sync (below) asserts the two currently agree,
# so any future drift between them surfaces as one clearly-labeled failure.
# None of these three imports touch nats-py, so collection stays safe even
# when nats-py is not installed.
from scripts._effective import effective_grants  # noqa: E402
from scripts._loader import load_matrix  # noqa: E402

from roxabi_contracts.verify import verify_deny  # noqa: E402

ACL_MATRIX_PATH = REPO_ROOT / "deploy/nats/acl-matrix.json"
ACL_MATRIX = load_matrix(ACL_MATRIX_PATH)
EFFECTIVE_GRANTS = effective_grants(ACL_MATRIX)
ACTIVE_IDENTITIES = sorted(EFFECTIVE_GRANTS)
FLOWS = ACL_MATRIX.get("request_reply_flows", [])


def _flow_id(flow: dict) -> str:
    return f"{flow['requester']}-{flow['responder']}"


def _identity_params() -> list[str]:
    """Parametrize over every active identity in the ACL matrix."""
    return list(ACTIVE_IDENTITIES)


def _non_flow_pair_target(flow: dict) -> str:
    """Active identity T such that (flow's responder, T) is not itself a flow pair.

    Excludes the responder itself and every requester paired with it across
    ALL request_reply_flows entries (not just this one) — so a responder that
    appears in multiple flows never accidentally gets probed against one of
    its own real pairs.
    """
    responder = flow["responder"]
    paired_requesters = {f["requester"] for f in FLOWS if f["responder"] == responder}
    for candidate in ACTIVE_IDENTITIES:
        if candidate != responder and candidate not in paired_requesters:
            return candidate
    raise AssertionError(
        f"no eligible non-flow-pair target identity for responder {responder!r} — "
        "every active identity is either the responder itself or one of its "
        "paired requesters"
    )


def _is_subscribe_permission_error(message: str) -> bool:
    """Subscribe-direction counterpart to factory.cli.ops._is_permission_error.

    That function's "publish" substring check is publish-only by design (it
    mirrors factory.cli.ops._probe, which only ever calls nc.publish()) and
    false-negatives on a real subscribe violation — confirmed empirically:
      publish violation:   'nats: permissions violation for publish to "..."'
      subscribe violation: 'nats: permissions violation for subscription to "..."'
    """
    msg = message.lower()
    return "permission" in msg and "subscri" in msg


async def _probe_subscribe(
    nc: Any, subject: str, errors: list[str], *, expect_deny: bool
) -> tuple[bool, str]:
    """Subscribe-direction counterpart to factory.cli.ops._probe.

    No subscribe-direction primitive exists in factory.cli.ops (it is
    publish-only) — this mirrors its flush + yield-once + inspect-errors-
    since-call idiom locally, reusing the same reliability property (more
    robust than a fixed sleep) without duplicating the publish-side code.
    """
    import asyncio

    import nats.errors

    before = len(errors)
    await nc.subscribe(subject)
    try:
        await nc.flush(timeout=2)
    except (asyncio.TimeoutError, TimeoutError, nats.errors.Error) as exc:
        return False, f"flush error: {exc}"
    await asyncio.sleep(0)  # NATS delivery window
    denied = any(_is_subscribe_permission_error(e) for e in errors[before:])
    if expect_deny:
        return (
            (True, "permission denied")
            if denied
            else (False, "subscribe accepted (expected deny)")
        )
    return (False, "permission denied") if denied else (True, "subscribed")


async def _settle_deny(
    errors: list[str],
    before: int,
    is_perm_error: Any,
    ok: bool,
    actual: str,
) -> tuple[bool, str]:
    """Bounded extra settle window for a deny-probe reported as accepted.

    Empirically observed (#2247 implementation): factory.cli.ops._probe (and
    the local _probe_subscribe above) only yield once, via a single
    zero-duration event-based tick, after flush() before inspecting captured
    errors. The async error_cb for the probed violation can land after that
    single yield — confirmed by re-inspecting `errors` moments later, after the
    connection had otherwise drained cleanly. Two distinct symptoms were
    observed empirically, both timing-shaped: (a) back-to-back probes on an
    already-"warm" connection needing one extra ~0.2s tick, and (b) a
    deny-probe that is the FIRST operation on a freshly-opened connection
    needing more cumulative elapsed time than a single 0.2s wait — a
    one-shot wait left 3/68 nodes flaky. A
    bounded poll (5 × 0.2s = up to 1s total) covers both without loosening
    the assertion itself or touching the reused primitive (out of scope —
    see spec Out of Scope).
    """
    import asyncio

    if ok:
        return ok, actual
    for _ in range(5):
        await asyncio.sleep(0.2)  # NATS delivery window
        if any(is_perm_error(e) for e in errors[before:]):
            return True, "permission denied (delayed delivery)"
    return ok, actual


async def _request_when_responders_ready(
    req_nc: Any,
    resp_nc: Any,
    subject: str,
    payload: bytes,
    *,
    request_timeout: float,
) -> Any:
    """Retry request() while the responder subscription propagates (#2275 flake).

    NATS Core drops requests with no registered subscriber. flush() round-trips
    the SUB to the server but cross-connection visibility can lag under CI load.
    """
    import asyncio

    from nats.errors import NoRespondersError

    last: NoRespondersError | None = None
    for attempt in range(5):
        try:
            return await req_nc.request(subject, payload, timeout=request_timeout)
        except NoRespondersError as exc:
            last = exc
            await resp_nc.flush(timeout=2)
            await asyncio.sleep(0.1 * (attempt + 1))  # NATS delivery window
    assert last is not None
    raise last


def _skip_without_nats_binaries() -> bool:
    """Skip locally when binaries are absent; on GHA conftest guard fails instead."""
    if os.getenv("GITHUB_ACTIONS") == "true":
        return False
    return not (NATS_AVAILABLE and NK_AVAILABLE)


pytestmark = [
    pytest.mark.live_acl,
    pytest.mark.skipif(
        _skip_without_nats_binaries(),
        reason="nats-server and nk must be on PATH — CI installs both",
    ),
    # Keep all tests in this file on a single pytest-xdist worker. Each test
    # would otherwise spawn its own module-scope fixture instance, multiplying
    # nats-server processes — and back-to-back runs hit TCP TIME_WAIT on the
    # ephemeral ports if many workers cycle them concurrently.
    pytest.mark.xdist_group(name="nats_server"),
]


# ── Module-scoped fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def rendered_auth_conf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Render auth.conf + seed files; return the directory.

    Writes:
      <tmp>/auth.conf         — nats-server authorization config
      <tmp>/<name>.seed       — raw nk seed bytes per active identity
    """
    from scripts._loader import load_matrix
    from scripts._nk import SubprocessNkeyProvider
    from scripts._renderer import render_auth_conf

    tmp = tmp_path_factory.mktemp("nats")
    # v3-current.json is auto-generated from deploy/nats/acl-matrix.json by
    # scripts/render_acl_parity.py; freshness is gate-enforced by
    # scripts/check-acl-specs-drift.sh (CI + pre-push).
    matrix_path = REPO_ROOT / "tests/scripts/fixtures/v3-current.json"
    matrix = load_matrix(matrix_path)

    provider = SubprocessNkeyProvider()
    active = {
        name: ident
        for name, ident in matrix["identities"].items()
        if ident["status"] == "active"
    }
    seeds = {name: provider.gen_seed(name) for name in active}
    pubkeys = {name: provider.pubkey_from_seed(seed) for name, seed in seeds.items()}
    text = render_auth_conf(matrix, pubkeys)

    (tmp / "auth.conf").write_text(text)
    for name, seed in seeds.items():
        seed_path = tmp / f"{name}.seed"
        seed_path.write_bytes(seed + b"\n")
        # 0o600: factory.cli.ops._read_seed (reused by the #2247 live-ACL
        # tests below) hardens against group/world-readable seed files.
        seed_path.chmod(0o600)
    return tmp


@pytest.fixture(scope="module")
def nats_server(
    rendered_auth_conf: Path,
) -> Generator[NatsServerEndpoints, None, None]:
    """Start nats-server with rendered auth.conf; shut down after tests.

    Uses OS-assigned ephemeral ports so back-to-back test runs don't collide
    on a TIME_WAIT socket — a previous hardcoded 4223/8222 binding could fail
    with ``address already in use`` for several seconds after teardown.
    """
    client_port = _free_port()
    monitor_port = _free_port()
    proc = subprocess.Popen(
        [
            "nats-server",
            "-a",
            "127.0.0.1",
            "-c",
            str(rendered_auth_conf / "auth.conf"),
            "-m",
            str(monitor_port),
            "-p",
            str(client_port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    monitor_url = f"http://127.0.0.1:{monitor_port}"
    # Poll healthz with 3s timeout
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{monitor_url}/healthz", timeout=0.5)
            break
        except OSError:
            time.sleep(0.05)  # event-based
    else:
        proc.terminate()
        raw = proc.stderr.read() if proc.stderr else b""
        stderr_log = raw.decode(errors="replace")
        pytest.fail(f"nats-server did not start within 3s\nstderr:\n{stderr_log}")

    yield NatsServerEndpoints(
        client_url=f"nats://127.0.0.1:{client_port}",
        monitor_url=monitor_url,
    )

    proc.terminate()
    proc.wait(timeout=5)


# ── Tests: structural (no live server needed) ─────────────────────────────────


def test_rendered_auth_conf_parses_correctly(rendered_auth_conf: Path) -> None:
    """parse_auth_conf round-trips the rendered file; user count matches active."""
    from scripts._loader import load_matrix
    from scripts._renderer import parse_auth_conf

    matrix = load_matrix(REPO_ROOT / "tests/scripts/fixtures/v3-current.json")
    active_count = sum(
        1 for ident in matrix["identities"].values() if ident["status"] == "active"
    )

    text = (rendered_auth_conf / "auth.conf").read_text()
    parsed = parse_auth_conf(text)

    assert len(parsed.users) == active_count, (
        f"Expected {active_count} users in auth.conf, got {len(parsed.users)}"
    )


def test_hub_identity_in_auth_conf(rendered_auth_conf: Path) -> None:
    """Rendered auth.conf contains a user entry whose comment_name is 'hub'."""
    from scripts._renderer import parse_auth_conf

    text = (rendered_auth_conf / "auth.conf").read_text()
    parsed = parse_auth_conf(text)

    names = [u.comment_name for u in parsed.users]
    assert "hub" in names, f"'hub' not found in auth.conf users; got: {names}"


def test_retired_identity_excluded(tmp_path: Path) -> None:
    """Render with v2-with-retired fixture; 'old-worker' must not appear."""
    from scripts._loader import load_matrix
    from scripts._renderer import render_auth_conf

    from tests.fakes.nkey_provider import FakeNkeyProvider

    matrix_path = FIXTURES_DIR / "v2-with-retired.json"
    matrix = load_matrix(matrix_path)

    provider = FakeNkeyProvider()
    active = {
        name: ident
        for name, ident in matrix["identities"].items()
        if ident["status"] == "active"
    }
    pubkeys = {
        name: provider.pubkey_from_seed(provider.gen_seed(name)) for name in active
    }
    text = render_auth_conf(matrix, pubkeys)

    # The retired identity name must not appear as a comment or nkey entry
    assert "old-worker" not in text, (
        "Retired identity 'old-worker' found in rendered auth.conf"
    )


def test_inbox_grant_derived_from_flows(rendered_auth_conf: Path) -> None:
    """clipool-worker publish allow includes '_inbox.hub.>' from hub flow."""
    from scripts._renderer import parse_auth_conf

    text = (rendered_auth_conf / "auth.conf").read_text()
    parsed = parse_auth_conf(text)

    clipool = next(
        (u for u in parsed.users if u.comment_name == "clipool-worker"), None
    )
    assert clipool is not None, "clipool-worker not found in auth.conf users"
    got = clipool.publish_allow
    assert "_inbox.hub.>" in got, (
        f"Expected '_inbox.hub.>' in clipool-worker publish_allow; got: {got}"
    )


# ── Tests: live server ────────────────────────────────────────────────────────


def test_nats_server_starts_with_auth_conf(nats_server: NatsServerEndpoints) -> None:
    """nats-server booted and healthz returns HTTP 200."""
    resp = urllib.request.urlopen(f"{nats_server.monitor_url}/healthz", timeout=2)
    assert resp.status == 200, f"healthz returned {resp.status}"


# ── Tests: matrix-driven live ACL round-trip (#2247) ───────────────────────
# Auto-covers every active identity/subject in acl-matrix.json (N2, N3) plus
# live cross-identity request/reply round-trips (N4, N5) — closing the
# postmortem's P0 gap without a 3rd per-identity static test file. Reuses
# factory.cli.ops's live-ACL primitives + roxabi_contracts.verify.verify_deny
# instead of reimplementing them (see spec Design Note item 3).


def test_acl_matrix_active_identities_nonempty() -> None:
    """Regression guard: a matrix edit must never silently zero out coverage.

    @pytest.mark.parametrize over an empty list below would otherwise
    disappear as zero collected test nodes with no failure at all.
    """
    assert len(ACTIVE_IDENTITIES) > 0, (
        "acl-matrix.json has no active identities — matrix authoring regression"
    )


def test_ci_hardfail_guard_noop_outside_github_actions() -> None:
    """N6 guard: `github_actions=False` never hard-fails, even with every dep missing.

    Pins the collection-time skip behavior that must survive outside GitHub
    Actions (Judgment Call above) — a future edit that drops the
    `github_actions` short-circuit would otherwise only surface as an
    unexpected hard-fail on some dev's machine, not as a failing test.
    """
    assert (
        _ci_hardfail_missing_deps(
            github_actions=False,
            nats_available=False,
            nk_available=False,
            nats_py_available=False,
        )
        == []
    )


def test_ci_hardfail_guard_fires_in_github_actions_with_missing_deps() -> None:
    """N6 guard: `github_actions=True` reports every missing dep by name.

    Regression coverage for the guard decision function — a future edit that
    deletes the guard, drops a dep from the check, or silently inverts the
    `== "true"` condition it's built from now fails this always-collected
    unit test instead of only a scenario nobody runs by default
    (`GITHUB_ACTIONS=true PATH=/usr/bin`, per the PR Test Plan).
    """
    assert _ci_hardfail_missing_deps(
        github_actions=True,
        nats_available=False,
        nk_available=True,
        nats_py_available=True,
    ) == ["nats-server"]
    assert _ci_hardfail_missing_deps(
        github_actions=True,
        nats_available=True,
        nk_available=False,
        nats_py_available=False,
    ) == ["nk", "nats-py"]


def test_ci_hardfail_guard_silent_in_github_actions_with_all_deps_present() -> None:
    """N6 guard: `github_actions=True` with every dep present reports nothing."""
    assert (
        _ci_hardfail_missing_deps(
            github_actions=True,
            nats_available=True,
            nk_available=True,
            nats_py_available=True,
        )
        == []
    )


@pytest.mark.skipif(
    not NATS_PY_AVAILABLE,
    reason="nats-py not installed — skipping live ACL test",
)
@pytest.mark.parametrize("identity", _identity_params())
def test_acl_matrix_publish(
    identity: str, nats_server: NatsServerEndpoints, rendered_auth_conf: Path
) -> None:
    """Every active identity's effective publish grants succeed; deny-probe denied.

    Sourced from effective_grants() (post group-expansion, post flow-inbox-
    injection) — add an identity or subject to acl-matrix.json and this picks
    it up with zero test-file edits. Tolerates empty publish lists (e.g.
    dashboard-reader): the deny-probe assertion still fires and is the
    substance of the test for those identities.
    """
    import asyncio

    from factory.cli.ops import (
        _expand_subject,
        _identity_connection,
        _is_permission_error,
        _probe,
    )

    pub_subjects, _sub_subjects = EFFECTIVE_GRANTS[identity]
    seed_path = rendered_auth_conf / f"{identity}.seed"

    async def _run() -> None:
        errors: list[str] = []
        async with _identity_connection(
            nats_server.client_url, seed_path, errors
        ) as nc:
            for subject in pub_subjects:
                ok, actual = await _probe(
                    nc, _expand_subject(subject), errors, expect_deny=False
                )
                assert ok, (
                    f"{identity}: expected publish to {subject!r} to succeed; "
                    f"got {actual}"
                )
            before = len(errors)
            ok, actual = await _probe(
                nc, verify_deny(identity), errors, expect_deny=True
            )
            ok, actual = await _settle_deny(
                errors, before, _is_permission_error, ok, actual
            )
            assert ok, f"{identity}: expected deny-probe to be denied; got {actual}"

    asyncio.run(_run())


@pytest.mark.skipif(
    not NATS_PY_AVAILABLE,
    reason="nats-py not installed — skipping live ACL test",
)
@pytest.mark.parametrize("identity", _identity_params())
def test_acl_matrix_subscribe(
    identity: str, nats_server: NatsServerEndpoints, rendered_auth_conf: Path
) -> None:
    """Every active identity's effective subscribe grants succeed; deny-probe denied.

    Mirrors test_acl_matrix_publish for the subscribe direction. factory.cli.ops
    has no subscribe-direction primitive (it is publish-only) — this uses the
    local _probe_subscribe/_is_subscribe_permission_error helpers above,
    built on the same reused connection/concretization/deny-subject primitives.
    Tolerates empty subscribe lists (e.g. gh-helper, ingress).
    """
    import asyncio

    from factory.cli.ops import _expand_subject, _identity_connection

    _pub_subjects, sub_subjects = EFFECTIVE_GRANTS[identity]
    seed_path = rendered_auth_conf / f"{identity}.seed"

    async def _run() -> None:
        errors: list[str] = []
        async with _identity_connection(
            nats_server.client_url, seed_path, errors
        ) as nc:
            for subject in sub_subjects:
                ok, actual = await _probe_subscribe(
                    nc, _expand_subject(subject), errors, expect_deny=False
                )
                assert ok, (
                    f"{identity}: expected subscribe to {subject!r} to succeed; "
                    f"got {actual}"
                )
            before = len(errors)
            ok, actual = await _probe_subscribe(
                nc, verify_deny(identity), errors, expect_deny=True
            )
            ok, actual = await _settle_deny(
                errors, before, _is_subscribe_permission_error, ok, actual
            )
            assert ok, (
                f"{identity}: expected subscribe deny-probe to be denied; got {actual}"
            )

    asyncio.run(_run())


@pytest.mark.skipif(
    not NATS_PY_AVAILABLE,
    reason="nats-py not installed — skipping live ACL test",
)
@pytest.mark.parametrize("flow", FLOWS, ids=_flow_id)
def test_flow_round_trip_positive(
    flow: dict, nats_server: NatsServerEndpoints, rendered_auth_conf: Path
) -> None:
    """Every request_reply_flows entry: requester's live request() gets a real reply.

    Requester and responder connect with their real inbox_prefix (matching
    production nats_connect(identity_name=...)) for every identity except
    llm-worker, whose real production inbox is _inbox.llmcli-llm per #1142
    (documented in acl-matrix.json's llm-worker.description) — harmless here
    since llm-worker only ever appears as a flow responder, never requester,
    so its own inbox is never exercised by this test.
    """
    import asyncio

    from factory.cli.ops import _expand_subject, _identity_connection

    requester, responder, subject = (
        flow["requester"],
        flow["responder"],
        flow["subject"],
    )
    concrete_subject = _expand_subject(subject)
    req_seed = rendered_auth_conf / f"{requester}.seed"
    resp_seed = rendered_auth_conf / f"{responder}.seed"

    async def _run() -> None:
        req_errors: list[str] = []
        resp_errors: list[str] = []
        async with _identity_connection(
            nats_server.client_url, req_seed, req_errors
        ) as req_nc:
            async with _identity_connection(
                nats_server.client_url, resp_seed, resp_errors
            ) as resp_nc:

                async def _respond(msg: Any) -> None:
                    await msg.respond(b"pong")

                await resp_nc.subscribe(concrete_subject, cb=_respond)
                await resp_nc.flush(timeout=2)
                request_timeout = (
                    10.0 if os.environ.get("GITHUB_ACTIONS") == "true" else 2.0
                )
                reply = await _request_when_responders_ready(
                    req_nc,
                    resp_nc,
                    concrete_subject,
                    b"ping",
                    request_timeout=request_timeout,
                )
                assert reply.data == b"pong", (
                    f"{requester}->{responder} on {concrete_subject!r}: "
                    f"unexpected reply {reply.data!r}"
                )

    asyncio.run(_run())


@pytest.mark.skipif(
    not NATS_PY_AVAILABLE,
    reason="nats-py not installed — skipping live ACL test",
)
@pytest.mark.parametrize("flow", FLOWS, ids=_flow_id)
def test_flow_round_trip_denies_non_flow_pair(
    flow: dict, nats_server: NatsServerEndpoints, rendered_auth_conf: Path
) -> None:
    """A responder publishing into a non-paired identity's inbox is denied.

    One-way publish-ACL check, NOT a round-trip: no request(), no reply, no
    callback to observe. An earlier draft of this test asserted a
    case-mutated (_INBOX vs _inbox) reply-to subject causes the requester's
    request() to time out — rejected during expert review, because a
    case-mutated publish times out regardless of whether the server's ACL
    allows or denies it (NATS subject matching is case-sensitive at the core
    protocol level for both authorization AND delivery), so that assertion
    would pass unconditionally and prove nothing. This test instead asserts
    real, deterministic ACL denial for a non-flow pair: responder Y publishes
    into a different active identity T's inbox, where (Y, T) is not itself a
    flow pair. Because Y never received a request from T, no
    allow_responses-derived dynamic grant is in play — denial is
    deterministic and the assertion is on an actual captured
    "permissions violation" error.
    """
    import asyncio

    from factory.cli.ops import _identity_connection, _is_permission_error, _probe

    responder = flow["responder"]
    target = _non_flow_pair_target(flow)
    resp_seed = rendered_auth_conf / f"{responder}.seed"
    probe_subject = f"_inbox.{target}.token"

    async def _run() -> None:
        errors: list[str] = []
        async with _identity_connection(
            nats_server.client_url, resp_seed, errors
        ) as nc:
            before = len(errors)
            ok, actual = await _probe(nc, probe_subject, errors, expect_deny=True)
            ok, actual = await _settle_deny(
                errors, before, _is_permission_error, ok, actual
            )
            assert ok, (
                f"{responder}: expected publish to non-flow-paired inbox "
                f"{probe_subject!r} to be denied; got {actual}"
            )

    asyncio.run(_run())


def test_acl_matrix_v3_current_in_sync() -> None:
    """acl-matrix.json's effective grants must match the v3-current.json fixture.

    The live nats-server for this file boots from the v3-current.json
    snapshot, while the matrix-driven tests above parametrize off the SSoT
    acl-matrix.json directly (Design Note item 1). This asserts the two
    currently agree so any future drift surfaces as one clearly-labeled
    failure instead of a batch of confusing per-identity ACL mismatches.
    """
    v3_matrix = load_matrix(FIXTURES_DIR / "v3-current.json")
    v3_grants = effective_grants(v3_matrix)
    assert EFFECTIVE_GRANTS == v3_grants, (
        "deploy/nats/acl-matrix.json and tests/scripts/fixtures/v3-current.json "
        "have drifted — regenerate the fixture "
        "(see scripts/check-acl-specs-drift.sh)"
    )


def test_retired_identity_connect_rejected(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Retired identity seed is not in auth.conf; connection must be rejected."""
    import asyncio
    import urllib.request

    if not (NATS_PY_AVAILABLE and NATS_AVAILABLE and NK_AVAILABLE):
        pytest.skip("nats-server, nk, and nats-py all required")

    from scripts._loader import load_matrix
    from scripts._nk import SubprocessNkeyProvider
    from scripts._renderer import render_auth_conf

    import nats

    tmp = tmp_path_factory.mktemp("nats_retired")
    matrix_path = FIXTURES_DIR / "v2-with-retired.json"
    matrix = load_matrix(matrix_path)

    provider = SubprocessNkeyProvider()
    # Generate seeds for ALL identities including retired so we can attempt connect
    seeds = {name: provider.gen_seed(name) for name in matrix["identities"]}
    # auth.conf only includes active identities
    active_pubkeys = {
        name: provider.pubkey_from_seed(seeds[name])
        for name, ident in matrix["identities"].items()
        if ident["status"] == "active"
    }
    text = render_auth_conf(matrix, active_pubkeys)
    (tmp / "auth.conf").write_text(text)
    for name, seed in seeds.items():
        (tmp / f"{name}.seed").write_bytes(seed + b"\n")

    client_port = _free_port()
    monitor_port = _free_port()
    proc = subprocess.Popen(
        [
            "nats-server",
            "-a",
            "127.0.0.1",
            "-c",
            str(tmp / "auth.conf"),
            "-m",
            str(monitor_port),
            "-p",
            str(client_port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{monitor_port}/healthz", timeout=0.5
            )
            break
        except OSError:
            time.sleep(0.05)  # event-based
    else:
        proc.terminate()
        raw = proc.stderr.read() if proc.stderr else b""
        stderr_text = raw.decode(errors="replace")
        pytest.fail(f"nats-server (retired test) did not start\n{stderr_text}")

    try:
        seed_str = (tmp / "old-worker.seed").read_text().strip()

        async def _connect_retired() -> None:
            await nats.connect(
                f"nats://127.0.0.1:{client_port}",
                nkeys_seed_str=seed_str,
                connect_timeout=2,
                allow_reconnect=False,
            )

        with pytest.raises(
            Exception,
            match=(
                r"authorization violation|Authorization Violation|auth error"
                r"|connection refused|no servers available"
            ),
        ):
            asyncio.run(_connect_retired())
    finally:
        proc.terminate()
        proc.wait(timeout=5)
