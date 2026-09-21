"""FACTORY_JOBS JetStream stream provisioning (ADR-079 sole-provisioner, ADR-088).

Stream FACTORY_JOBS: WorkQueue retention, FILE storage.
Subjects are explicitly enumerated — factory.jobs.omp intentionally excluded
(core-NATS queue group, no bound consumer on this stream; see AGENTS.md and
ADR-088 §Context).

MaxAge=7d, MaxMsgs=100_000, duplicate_window=120s.
All provisioning is idempotent — safe to call on every hub restart.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import nats.errors
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import BadRequestError

if TYPE_CHECKING:
    from nats.js.client import JetStreamContext

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stream identity
# ---------------------------------------------------------------------------

STREAM_NAME = "FACTORY_JOBS"

# Explicit enumeration — factory.jobs.omp intentionally excluded (core-NATS
# queue group, no bound consumer benefit; factory.jobs.> wildcard forbidden —
# see AGENTS.md invariants).
SUBJECTS: list[str] = [
    "factory.jobs.vault.>",
    "factory.jobs.web-intel.>",
    "factory.jobs.test.>",
    "factory.jobs.dlq.>",
]

# ---------------------------------------------------------------------------
# Stream constants
# ---------------------------------------------------------------------------

_MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # 7 days — ops DLQ inspection window
_MAX_MSGS = 100_000  # discard=OLD at limit
_DUPLICATE_WINDOW_SECONDS = 120  # dedup window for Nats-Msg-Id header


# ---------------------------------------------------------------------------
# Config builder (internal)
# ---------------------------------------------------------------------------


def _stream_config() -> StreamConfig:
    """Return the canonical FACTORY_JOBS StreamConfig."""
    return StreamConfig(
        name=STREAM_NAME,
        subjects=SUBJECTS,
        retention=RetentionPolicy.WORK_QUEUE,
        storage=StorageType.FILE,
        max_age=float(_MAX_AGE_SECONDS),
        max_msgs=_MAX_MSGS,
        discard=DiscardPolicy.OLD,
        num_replicas=1,
        duplicate_window=_DUPLICATE_WINDOW_SECONDS,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def ensure_jobs_stream(js: "JetStreamContext") -> None:
    """Create or update FACTORY_JOBS stream idempotently.

    Pattern: try add_stream first; on BadRequestError (already exists) try
    update_stream to converge config. Any other nats.errors.Error is re-raised.

    Called by hub_standalone.py before announce_hub_ready() (ADR-079).
    Fail-fast: a provisioning failure prevents the hub from announcing readiness.
    """
    cfg = _stream_config()
    try:
        await js.add_stream(cfg)
        log.info("FACTORY_JOBS stream created")
    except BadRequestError:
        try:
            await js.update_stream(cfg)
            log.info("FACTORY_JOBS stream config updated")
        except nats.errors.Error:
            log.exception("FACTORY_JOBS stream update failed")
            raise
    except nats.errors.Error:
        log.exception("FACTORY_JOBS stream add failed")
        raise
