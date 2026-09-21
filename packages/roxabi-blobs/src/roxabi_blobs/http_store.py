"""HTTP client for FsBlobStore-backed remote BlobStore. ADR-067 + #1330 V8."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from .errors import BlobNotFoundError
from .models import BlobRef

# Default write (and overall/read) timeout raised from 5 s to 30 s to accommodate
# 20 MiB non-audio CDN→PUT transfers (#1552 / parent Open-Q1).  connect stays at
# 5 s — it is unaffected by payload size.
_raw_write_timeout = os.environ.get("FACTORY_BLOBSTORE_WRITE_TIMEOUT_S")
try:
    _DEFAULT_WRITE_TIMEOUT_S: float = (
        float(_raw_write_timeout) if _raw_write_timeout else 30.0
    )
except ValueError:
    raise ValueError(
        f"FACTORY_BLOBSTORE_WRITE_TIMEOUT_S must be a float, got {_raw_write_timeout!r}"
    ) from None


class HttpBlobStore:
    """HTTP client for a remote `lyra blobstore serve` service.

    Per-request timeout: connect=5 s, write/read/pool=``timeout`` seconds
    (default 30 s, overridable via ``FACTORY_BLOBSTORE_WRITE_TIMEOUT_S`` env var).
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = _DEFAULT_WRITE_TIMEOUT_S,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = _transport
        self._client: httpx.AsyncClient | None = None
        # ASGI lifespan support (test seam only — production uses real HTTP)
        self._lifespan_task: asyncio.Task[None] | None = None
        self._lifespan_started: asyncio.Event | None = None
        self._lifespan_shutdown: asyncio.Event | None = None

    async def _start_asgi_lifespan(self, app: Any) -> None:
        """Start the ASGI lifespan for an in-process app (test seam only).

        ASGITransport does not trigger startup/shutdown; we drive it manually
        so FsBlobStore.open() (and app.state.store) is available before requests.
        """
        started = asyncio.Event()
        self._lifespan_started = started
        shutdown = asyncio.Event()
        self._lifespan_shutdown = shutdown

        receive_queue: asyncio.Queue[dict] = asyncio.Queue()
        await receive_queue.put({"type": "lifespan.startup"})

        async def receive() -> dict:
            return await receive_queue.get()

        async def send(event: dict) -> None:
            if event["type"] == "lifespan.startup.complete":
                started.set()

        async def run() -> None:
            scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
            try:
                await app(scope, receive, send)
            except Exception:  # noqa: BLE001
                pass

        self._lifespan_task = asyncio.create_task(run())
        await started.wait()

    async def _stop_asgi_lifespan(self) -> None:
        """Send shutdown event to the ASGI lifespan task."""
        if self._lifespan_shutdown is not None and self._lifespan_task is not None:
            self._lifespan_shutdown.set()
            # Feed shutdown event to the receive queue
            # Task may have already exited; guard with shield
            try:
                await asyncio.wait_for(self._lifespan_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._lifespan_task.cancel()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # When _transport is an ASGITransport, start the ASGI lifespan first
            # so that app.state is initialised before any request.
            if isinstance(self._transport, httpx.ASGITransport):
                await self._start_asgi_lifespan(self._transport.app)
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._token}"},
                transport=self._transport,  # None → default network transport
                timeout=httpx.Timeout(self._timeout, connect=5.0),
            )
        return self._client

    async def put(  # noqa: PLR0913 — signature locked by ADR-067 §Interface
        self,
        data: bytes,
        *,
        mime: str,
        source: str,
        filename: str | None = None,
        platform_ref: str | None = None,
        platform_message_id: str | None = None,
    ) -> BlobRef:
        """PUT bytes to the remote store; returns BlobRef with id populated."""
        client = await self._ensure_client()
        headers: dict[str, str] = {
            "Content-Type": mime,
            "X-Blob-Source": source,
        }
        if platform_ref is not None:
            headers["X-Blob-Platform-Ref"] = platform_ref
        if platform_message_id is not None:
            headers["X-Blob-Platform-Message-Id"] = platform_message_id
        if filename is not None:
            headers["X-Blob-Filename"] = filename
        resp = await client.put("/blobs", content=data, headers=headers)
        resp.raise_for_status()
        return BlobRef.model_validate(resp.json())

    async def get(self, store_key: str) -> bytes:
        """GET bytes by store_key; raises BlobNotFoundError on 404.

        ``store_key`` is server-issued and opaque — callers must not construct
        or sanitise it.  The BlobStore server enforces path-containment:
        ``FsBlobStore._safe_resolve_in_root`` ensures a traversal key that
        resolves outside the blob root → 404 with no oracle leak.
        Do NOT add client-side ``store_key`` sanitisation here — it would break
        the legitimate ``/blobs/{store_key:path}`` legacy-key slash semantics.
        This invariant is regression-locked by
        ``tests/blobstore/test_serve_api.py::TestPathTraversal`` and
        ``packages/roxabi-blobs/tests/test_security.py::TestPathTraversal``.
        """
        client = await self._ensure_client()
        resp = await client.get(f"/blobs/{store_key}")
        if resp.status_code == 404:
            raise BlobNotFoundError(store_key)
        resp.raise_for_status()
        return resp.content

    async def exists(self, content_hash: str) -> BlobRef | None:
        """HEAD /blobs/{content_hash}; returns sentinel BlobRef or None.

        Over HTTP the argument is treated as a store_key (wire path), NOT a
        content_hash as in FsBlobStore.exists — see AGENTS.md §HttpBlobStore.

        Returns a **sentinel BlobRef** (``is_sentinel=True``,
        ``content_hash=""``) on hit. The sentinel carries no metadata beyond
        existence — ``content_hash``/``size``/``mime``/``created_at`` are
        placeholders. Callers needing the full envelope should PUT and capture
        the response.

        WARNING: do NOT forward this sentinel into a ``roxabi_contracts.BlobRef``
        constructor — the wire model no longer validates ``content_hash`` against a
        sentinel constant, so a sparse sentinel would silently carry
        ``content_hash=""``; callers must PUT to obtain a full envelope.
        """
        # HEAD endpoint only returns 200/404; reconstruct a minimal BlobRef on hit.
        # Full BlobRef data is not available via HEAD — callers needing the full
        # envelope should PUT and capture the response instead.
        client = await self._ensure_client()
        resp = await client.head(f"/blobs/{content_hash}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        # HEAD returns no body — synthesise a sentinel BlobRef so Protocol
        # callers that only test truthiness get a non-None result. is_sentinel=True
        # makes the sparseness machine-checkable (BlobRef validator rejects
        # content_hash="" without it; must not be forwarded as a wire BlobRef).
        return BlobRef(
            store_key=content_hash,
            content_hash="",
            mime="application/octet-stream",
            size=0,
            source="http",
            created_at=datetime.now(tz=UTC),
            is_sentinel=True,
        )

    async def delete(self, blob_ref_id: int) -> None:
        """DELETE /blobs/{blob_ref_id}; numeric path resolved server-side."""
        client = await self._ensure_client()
        resp = await client.delete(f"/blobs/{blob_ref_id}")
        if resp.status_code == 404:
            raise BlobNotFoundError(str(blob_ref_id))
        resp.raise_for_status()

    async def __aenter__(self) -> "HttpBlobStore":
        await self._ensure_client()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await self._stop_asgi_lifespan()
