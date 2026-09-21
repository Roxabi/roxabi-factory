"""Unit tests for FACTORY_JOBS stream provisioning (no live NATS).

Verifies idempotency: calling ensure_jobs_stream twice does not raise.
All tests use mock JetStreamContext — no live NATS server required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import nats.errors
import pytest
from nats.js.errors import BadRequestError

from factory.infrastructure.jobs.stream_setup import (
    STREAM_NAME,
    SUBJECTS,
    _stream_config,
    ensure_jobs_stream,
)

# ---------------------------------------------------------------------------
# ensure_jobs_stream
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ensure_jobs_stream_add_success() -> None:
    """Happy path: add_stream succeeds → update_stream must NOT be called."""
    js = MagicMock()
    js.add_stream = AsyncMock(return_value=MagicMock())
    js.update_stream = AsyncMock()

    await ensure_jobs_stream(js)

    js.add_stream.assert_awaited_once()
    js.update_stream.assert_not_awaited()


@pytest.mark.anyio
async def test_ensure_jobs_stream_update_on_existing() -> None:
    """Stream already exists (BadRequestError) → update_stream called with config."""
    js = MagicMock()
    js.add_stream = AsyncMock(side_effect=BadRequestError())
    js.update_stream = AsyncMock(return_value=MagicMock())
    expected_cfg = _stream_config()

    await ensure_jobs_stream(js)

    js.update_stream.assert_awaited_once()
    assert js.update_stream.await_args == call(expected_cfg)


@pytest.mark.anyio
async def test_ensure_jobs_stream_idempotent_double_call() -> None:
    """Second call with existing stream still succeeds (update path)."""
    js = MagicMock()
    js.add_stream = AsyncMock(side_effect=[MagicMock(), BadRequestError()])
    js.update_stream = AsyncMock(return_value=MagicMock())

    await ensure_jobs_stream(js)
    await ensure_jobs_stream(js)

    assert js.add_stream.await_count == 2
    js.update_stream.assert_awaited_once()


@pytest.mark.anyio
async def test_ensure_jobs_stream_add_error_propagates() -> None:
    """A non-BadRequest nats error on add_stream must propagate (fail-fast).

    ADR-079: a provisioning failure must abort hub boot, never be swallowed.
    """
    js = MagicMock()
    js.add_stream = AsyncMock(side_effect=nats.errors.Error())
    js.update_stream = AsyncMock()

    with pytest.raises(nats.errors.Error):
        await ensure_jobs_stream(js)

    js.update_stream.assert_not_awaited()


@pytest.mark.anyio
async def test_ensure_jobs_stream_update_error_propagates() -> None:
    """A nats error on update_stream (after BadRequest on add) must propagate."""
    js = MagicMock()
    js.add_stream = AsyncMock(side_effect=BadRequestError())
    js.update_stream = AsyncMock(side_effect=nats.errors.Error())

    with pytest.raises(nats.errors.Error):
        await ensure_jobs_stream(js)


# ---------------------------------------------------------------------------
# Subject-enumeration invariants
# ---------------------------------------------------------------------------


def test_stream_subjects_exclude_runtime_lanes() -> None:
    """Runtime harness lanes stay core-NATS — must NOT appear in WorkQueue SUBJECTS."""
    for token in ("omp", "claude"):
        matches = [s for s in SUBJECTS if token in s]
        assert matches == [], (
            f"factory.jobs.{token} captured in WorkQueue SUBJECTS: {matches}. "
            "Runtime lanes intentionally excluded (see ADR-088)."
        )


def test_stream_subjects_exclude_wildcard() -> None:
    """factory.jobs.> bare wildcard must NOT appear in SUBJECTS."""
    assert "factory.jobs.>" not in SUBJECTS, (
        "factory.jobs.> wildcard forbidden in WorkQueue SUBJECTS — "
        "enumerate subjects explicitly (see AGENTS.md invariants)."
    )


def test_stream_subjects_expected_four() -> None:
    """Exactly the four expected subjects must be present in SUBJECTS."""
    expected = {
        "factory.jobs.vault.>",
        "factory.jobs.web-intel.>",
        "factory.jobs.test.>",
        "factory.jobs.dlq.>",
    }
    assert set(SUBJECTS) == expected, (
        f"SUBJECTS mismatch.\n  expected: {sorted(expected)}\n"
        f"  got:      {sorted(SUBJECTS)}"
    )


# ---------------------------------------------------------------------------
# Stream config sanity checks
# ---------------------------------------------------------------------------


def test_stream_name_constant() -> None:
    """STREAM_NAME must equal the fleet-convention name used in ACL/advisory."""
    assert STREAM_NAME == "FACTORY_JOBS"


def test_stream_uses_work_queue_retention() -> None:
    """Stream must use WorkQueue retention for single-delivery guarantee."""
    from nats.js.api import RetentionPolicy

    cfg = _stream_config()
    assert cfg.retention == RetentionPolicy.WORK_QUEUE
