"""Tests for V2 HTTP API (N1–N4) and stale-token assertion (SC-Code-5)."""

from __future__ import annotations

import hashlib
import pathlib
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from factory.blobstore.serve import build_app

# Small PNG-like payload for PUT tests.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 100


def _make_client(token_path: pathlib.Path, blob_root: pathlib.Path) -> TestClient:
    """Build a TestClient against the future build_app(token_path, blob_root) API.

    Enters the client context manager so the ASGI lifespan (FsBlobStore open)
    is triggered before the first request.
    """
    app = build_app(token_path=token_path, blob_root=blob_root)
    client = TestClient(app)
    client.__enter__()
    return client


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def token_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Write 'test-token' to a temp file and return its path."""
    p = tmp_path / "blobstore.tok"
    p.write_text("test-token")
    return p


@pytest.fixture()
def blob_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return a fresh temp dir for blob storage."""
    d = tmp_path / "blobs"
    d.mkdir()
    return d


@pytest.fixture()
def client(token_path: pathlib.Path, blob_root: pathlib.Path):  # type: ignore[return]
    """TestClient wired with token_path + blob_root."""
    app = build_app(token_path=token_path, blob_root=blob_root)
    with TestClient(app) as c:
        yield c


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _put_headers() -> dict[str, str]:
    return {
        **_auth_headers(),
        "Content-Type": "image/png",
        "X-Blob-Source": "test-source",
        "X-Blob-Platform-Ref": "test-ref",
        "X-Blob-Platform-Message-Id": "test-msg-id",
        "X-Blob-Filename": "test.png",
    }


# ---------------------------------------------------------------------------
# N1 — PUT /blobs
# ---------------------------------------------------------------------------


class TestPutBlob:
    def test_put_returns_201_with_blob_ref_on_valid_bearer(
        self, client: TestClient
    ) -> None:
        """PUT /blobs with valid bearer returns 201 and a BlobRef-shaped JSON body."""
        # Arrange
        headers = _put_headers()
        # Act
        response = client.put("/blobs", content=_PNG_BYTES, headers=headers)
        # Assert
        assert response.status_code == 201
        body = response.json()
        assert "store_key" in body
        assert "content_hash" in body
        assert "size" in body
        assert "mime" in body

    def test_put_store_key_is_content_address(self, client: TestClient) -> None:
        """PUT response store_key is 'sha256:<hex>' and Location matches."""
        response = client.put("/blobs", content=_PNG_BYTES, headers=_put_headers())
        assert response.status_code == 201
        body = response.json()
        expected_hex = hashlib.sha256(_PNG_BYTES).hexdigest()
        assert body["store_key"] == f"sha256:{expected_hex}"
        assert response.headers["location"] == f"/blobs/sha256:{expected_hex}"

    def test_put_returns_401_on_wrong_bearer(self, client: TestClient) -> None:
        """PUT /blobs with wrong bearer returns 401."""
        # Arrange
        headers = {
            **_put_headers(),
            "Authorization": "Bearer wrong-token",
        }
        # Act
        response = client.put("/blobs", content=_PNG_BYTES, headers=headers)
        # Assert
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# N2 — GET /blobs/{store_key}
# ---------------------------------------------------------------------------


class TestGetBlob:
    def test_get_returns_200_with_body_after_put(self, client: TestClient) -> None:
        """PUT then GET the returned store_key; body matches uploaded bytes."""
        # Arrange — PUT first
        put_resp = client.put("/blobs", content=_PNG_BYTES, headers=_put_headers())
        assert put_resp.status_code == 201
        store_key = put_resp.json()["store_key"]
        # Act
        response = client.get(f"/blobs/{store_key}", headers=_auth_headers())
        # Assert
        assert response.status_code == 200
        assert response.content == _PNG_BYTES

    def test_get_returns_404_when_unknown_store_key(self, client: TestClient) -> None:
        """GET on a never-PUT store_key returns 404."""
        response = client.get("/blobs/nonexistent-key", headers=_auth_headers())
        assert response.status_code == 404

    def test_put_get_roundtrip_via_sha256_wire_key(self, client: TestClient) -> None:
        """PUT → sha256 wire key → GET returns original bytes (canonical roundtrip)."""
        put_resp = client.put("/blobs", content=_PNG_BYTES, headers=_put_headers())
        assert put_resp.status_code == 201
        wire_key = put_resp.json()["store_key"]
        expected_hex = hashlib.sha256(_PNG_BYTES).hexdigest()
        assert wire_key == f"sha256:{expected_hex}"
        # GET via the content-address wire key must return the original bytes.
        get_resp = client.get(f"/blobs/{wire_key}", headers=_auth_headers())
        assert get_resp.status_code == 200
        assert get_resp.content == _PNG_BYTES

    def test_get_returns_404_on_bogus_sha256_wire_key(self, client: TestClient) -> None:
        """GET with a valid-prefix but non-existent sha256 key returns 404."""
        bogus = "sha256:" + "a" * 64
        response = client.get(f"/blobs/{bogus}", headers=_auth_headers())
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# N3 — HEAD /blobs/{store_key}
# ---------------------------------------------------------------------------


class TestHeadBlob:
    def test_head_returns_200_when_exists(self, client: TestClient) -> None:
        """HEAD /blobs/{store_key} returns 200 with no body after a PUT."""
        # Arrange
        put_resp = client.put("/blobs", content=_PNG_BYTES, headers=_put_headers())
        assert put_resp.status_code == 201
        store_key = put_resp.json()["store_key"]
        # Act
        response = client.head(f"/blobs/{store_key}", headers=_auth_headers())
        # Assert
        assert response.status_code == 200
        assert response.content == b""

    def test_head_returns_404_when_unknown(self, client: TestClient) -> None:
        """HEAD /blobs/{store_key} returns 404 for an unknown key."""
        # Arrange
        # Act
        response = client.head("/blobs/nonexistent-key", headers=_auth_headers())
        # Assert
        assert response.status_code == 404

    def test_head_does_not_call_store_exists(
        self, blob_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HEAD /blobs/{store_key} must not call FsBlobStore.exists — L1 (#1362).

        The handler was refactored (T3) to query the DB directly (≤2 SELECTs).
        Calling store.exists() would be a redundant 3rd DB hit.  We assert the
        absence of that call by replacing exists with a sentinel that raises if
        reached; a successful HEAD proves the handler never touched it.
        """

        # Arrange — PUT a blob so HEAD can succeed on the happy path
        app = build_app(token="test-token", blob_root=blob_root)
        with TestClient(app) as client:
            put_resp = client.put(
                "/blobs",
                content=_PNG_BYTES,
                headers=_put_headers(),
            )
            assert put_resp.status_code == 201
            store_key = put_resp.json()["store_key"]

            # Replace exists on the live store instance so the monkeypatch is
            # scoped to the already-opened store (lifespan has run by this point).
            def _exists_must_not_be_called(*_a: object, **_kw: object) -> None:
                raise AssertionError("HEAD must not call store.exists()")

            monkeypatch.setattr(app.state.store, "exists", _exists_must_not_be_called)

            # Act — HEAD on the known key
            response = client.head(f"/blobs/{store_key}", headers=_auth_headers())

        # Assert — 200 proves the handler reached the success path without
        # calling exists() (which would have raised AssertionError → 500).
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# N4 — DELETE /blobs/{store_key}
# ---------------------------------------------------------------------------


class TestDeleteBlob:
    def test_delete_returns_204_when_exists(self, client: TestClient) -> None:
        """DELETE returns 204; subsequent HEAD returns 404."""
        # Arrange
        put_resp = client.put("/blobs", content=_PNG_BYTES, headers=_put_headers())
        assert put_resp.status_code == 201
        store_key = put_resp.json()["store_key"]
        # Act — delete
        del_resp = client.delete(f"/blobs/{store_key}", headers=_auth_headers())
        # Assert — delete succeeded
        assert del_resp.status_code == 204
        # Assert — subsequent HEAD shows key is gone
        head_resp = client.head(f"/blobs/{store_key}", headers=_auth_headers())
        assert head_resp.status_code == 404

    def test_delete_returns_404_when_unknown(self, client: TestClient) -> None:
        """DELETE on an unknown store_key returns 404."""
        # Arrange
        # Act
        response = client.delete("/blobs/nonexistent-key", headers=_auth_headers())
        # Assert
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# SC-Code-5 — Stale-token two-direction assertion
# ---------------------------------------------------------------------------


class TestStaleToken:
    def test_token_is_read_once_at_startup(self, tmp_path: pathlib.Path) -> None:
        """Token is read once at startup; changing the file has no effect.

        Two-direction assertion (SC-Code-5):
          (a) OLD token still passes after file is overwritten with NEW.
          (b) NEW token (what is currently on disk) is rejected.
        Both branches are required — OLD-passes alone proves nothing about re-reads.
        """
        # Arrange — write OLD token to file, boot app
        tok_file = tmp_path / "blobstore.tok"
        tok_file.write_text("OLD")
        blob_root = tmp_path / "blobs"
        blob_root.mkdir()
        client = _make_client(tok_file, blob_root)

        # Sanity: OLD passes before overwrite
        pre_resp = client.get(
            "/blobs/anything", headers={"Authorization": "Bearer OLD"}
        )
        # We only care about non-401 here (could be 404 once handlers exist)
        assert pre_resp.status_code != 401

        # Overwrite the file with NEW
        tok_file.write_text("NEW")

        # Act + Assert (a): OLD still passes — token was cached at startup
        old_resp = client.get(
            "/blobs/anything", headers={"Authorization": "Bearer OLD"}
        )
        assert old_resp.status_code != 401, (
            "OLD token should still be accepted after file overwrite "
            "(token must be read once at startup, not per-request)"
        )

        # Act + Assert (b): NEW is rejected — on disk but NOT the startup value
        new_resp = client.get(
            "/blobs/anything", headers={"Authorization": "Bearer NEW"}
        )
        assert new_resp.status_code == 401, (
            "NEW token (current file content) must be rejected "
            "(server must not re-read the token file)"
        )


# ---------------------------------------------------------------------------
# SC-Code-12 — 401 emits audit event with result="unauthorized"
# ---------------------------------------------------------------------------


class TestUnauthorizedAuditEmission:
    """401 responses must emit a BlobAuditEvent(result='unauthorized') — SC-Code-12."""

    def test_401_on_missing_bearer_emits_audit_event(
        self, blob_root: pathlib.Path
    ) -> None:
        """GET without Authorization header returns 401 and emits audit event."""
        app = build_app(token="test-token", blob_root=blob_root)
        mock_sink = AsyncMock()
        mock_sink.emit = AsyncMock()

        with TestClient(app) as client:
            # Inject mock after lifespan start so it isn't overwritten
            app.state.audit_sink = mock_sink
            response = client.get("/blobs/some-key")

        assert response.status_code == 401
        mock_sink.emit.assert_awaited_once()
        event = mock_sink.emit.call_args[0][0]
        assert event.result == "unauthorized"
        assert event.op == "get"

    def test_401_on_wrong_bearer_emits_audit_event(
        self, blob_root: pathlib.Path
    ) -> None:
        """PUT with wrong bearer returns 401 and emits audit event."""
        app = build_app(token="test-token", blob_root=blob_root)
        mock_sink = AsyncMock()
        mock_sink.emit = AsyncMock()

        with TestClient(app) as client:
            app.state.audit_sink = mock_sink
            response = client.put(
                "/blobs",
                content=b"data",
                headers={"Authorization": "Bearer wrong-token"},
            )

        assert response.status_code == 401
        mock_sink.emit.assert_awaited_once()
        event = mock_sink.emit.call_args[0][0]
        assert event.result == "unauthorized"
        assert event.op == "put"

    def test_401_audit_event_does_not_contain_rejected_token(
        self, blob_root: pathlib.Path
    ) -> None:
        """Regression guard: audit event payload must not leak the rejected bearer."""
        app = build_app(token="test-token", blob_root=blob_root)
        mock_sink = AsyncMock()
        mock_sink.emit = AsyncMock()
        rejected_token = "super-secret-bad-token"

        with TestClient(app) as client:
            app.state.audit_sink = mock_sink
            response = client.get(
                "/blobs/some-key",
                headers={"Authorization": f"Bearer {rejected_token}"},
            )

        assert response.status_code == 401
        mock_sink.emit.assert_awaited_once()
        event = mock_sink.emit.call_args[0][0]
        # Serialize to JSON and ensure the rejected token value is absent
        event_json = event.model_dump_json()
        assert rejected_token not in event_json, (
            f"Rejected token found in audit event payload: {event_json!r}"
        )

    def test_401_no_audit_when_sink_absent(self, blob_root: pathlib.Path) -> None:
        """When audit_sink is not on app.state, 401 still returns without error."""
        app = build_app(token="test-token", blob_root=blob_root)

        with TestClient(app) as client:
            # Remove audit_sink to simulate un-provisioned state
            del app.state.audit_sink
            response = client.get(
                "/blobs/key", headers={"Authorization": "Bearer wrong"}
            )

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Error paths — N1 PUT failure modes (Fix A, Fix B, Fix D)
# ---------------------------------------------------------------------------


class TestErrorPaths:
    """500 error paths for PUT — BlobWriteError, BlobConsistencyError, oversized."""

    def test_put_returns_500_on_blob_write_error(
        self, blob_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N1 PUT → 500 when FsBlobStore.put raises BlobWriteError; no detail leak."""
        from roxabi_blobs.errors import BlobWriteError

        async def _failing_put(*_a: object, **_kw: object) -> None:
            raise BlobWriteError("INTERNAL_DETAIL_MUST_NOT_LEAK")

        app = build_app(token="test-token", blob_root=blob_root)
        with TestClient(app) as client:
            monkeypatch.setattr(app.state.store, "put", _failing_put)
            response = client.put(
                "/blobs",
                content=b"x" * 100,
                headers={
                    "Authorization": "Bearer test-token",
                    "Content-Type": "application/octet-stream",
                    "X-Blob-Source": "test",
                },
            )

        # Assert
        assert response.status_code == 500
        body = response.json()
        assert "INTERNAL_DETAIL_MUST_NOT_LEAK" not in str(body)
        assert body.get("detail") == "blob write failed"

    def test_put_returns_500_on_blob_consistency_error(
        self, blob_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N1 PUT → 500 when FsBlobStore.put raises BlobConsistencyError; no leak."""
        from roxabi_blobs.errors import BlobConsistencyError

        async def _failing_put(*_a: object, **_kw: object) -> None:
            raise BlobConsistencyError("CONSISTENCY_DETAIL_MUST_NOT_LEAK")

        app = build_app(token="test-token", blob_root=blob_root)
        with TestClient(app) as client:
            monkeypatch.setattr(app.state.store, "put", _failing_put)
            response = client.put(
                "/blobs",
                content=b"x" * 100,
                headers={
                    "Authorization": "Bearer test-token",
                    "Content-Type": "application/octet-stream",
                    "X-Blob-Source": "test",
                },
            )

        # Assert — BlobConsistencyError is caught by the bare except → "internal error"
        assert response.status_code == 500
        body = response.json()
        assert "CONSISTENCY_DETAIL_MUST_NOT_LEAK" not in str(body)
        assert body.get("detail") == "internal error"

    @pytest.mark.xfail(
        reason=(
            "V8 ships without a pre-read 413 gate — oversized blobs that exceed "
            "FsBlobStore's internal cap surface as BlobWriteError → 500. "
            "Documented in src/factory/blobstore/AGENTS.md §Oversized-blob handling."
        )
    )
    def test_put_oversized_blob_returns_413_per_s2_decision(
        self, blob_root: pathlib.Path
    ) -> None:
        """Captured intent: pre-read 413 gate deferred to post-V8; currently 500."""
        app = build_app(token="test-token", blob_root=blob_root)
        with TestClient(app) as client:
            response = client.put(
                "/blobs",
                content=b"x" * (500 * 1024 * 1024),  # 500 MiB sentinel
                headers={
                    "Authorization": "Bearer test-token",
                    "Content-Type": "application/octet-stream",
                    "X-Blob-Source": "test",
                },
            )
        # When the 413 gate is added, this line should pass; until then xfail.
        assert response.status_code == 413


# ---------------------------------------------------------------------------
# N2 — Path traversal returns 404 (Fix C)
# ---------------------------------------------------------------------------


class TestPathTraversal:
    """Path-traversal keys must return 404 — no oracle for invalid vs missing."""

    def test_get_returns_404_on_path_traversal_no_oracle(
        self, client: TestClient
    ) -> None:
        """GET path traversal returns 404; body must not echo the traversal string."""
        # Both percent-encoded and raw forms resolve via
        # FsBlobStore._safe_resolve_in_root → BlobNotFoundError → 404.
        traversals = [
            "../../../etc/passwd",
            "..%2F..%2Fetc%2Fpasswd",
        ]
        for traversal in traversals:
            response = client.get(
                f"/blobs/{traversal}",
                headers={"Authorization": "Bearer test-token"},
            )
            # Assert — 404 (not 400 — no oracle that reveals format check)
            assert response.status_code == 404, f"path={traversal!r}"
            body = str(response.json())
            assert "etc/passwd" not in body, f"leaked in body path={traversal!r}"
            assert "passwd" not in body, f"leaked in body path={traversal!r}"


# ---------------------------------------------------------------------------
# Bonus — audit emission wired on successful PUT (Blocker #1 regression guard)
# ---------------------------------------------------------------------------


class TestAuditEmissionOnSuccess:
    """Successful PUT must emit a BlobAuditEvent(op='put', result='ok')."""

    def test_put_success_emits_audit_event_with_result_ok(
        self, blob_root: pathlib.Path
    ) -> None:
        """PUT returning 201 must trigger _emit_audit with result='ok'."""
        from unittest.mock import AsyncMock

        app = build_app(token="test-token", blob_root=blob_root)
        mock_sink = AsyncMock()
        mock_sink.emit = AsyncMock()

        with TestClient(app) as client:
            # Inject after lifespan so it is not overwritten
            app.state.audit_sink = mock_sink
            response = client.put(
                "/blobs",
                content=_PNG_BYTES,
                headers={
                    "Authorization": "Bearer test-token",
                    "Content-Type": "image/png",
                    "X-Blob-Source": "test-source",
                },
            )

        # Assert
        assert response.status_code == 201
        mock_sink.emit.assert_awaited_once()
        event = mock_sink.emit.call_args[0][0]
        assert event.op == "put"
        assert event.result == "ok"
