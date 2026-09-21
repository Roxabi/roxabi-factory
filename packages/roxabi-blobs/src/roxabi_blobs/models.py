"""Pydantic envelope returned by `BlobStore` operations.

Mirrors ADR-067 §BlobRef. V2 (#1064) adds a wire-side mirror in
`roxabi-contracts`; the two are kept in sync by spec, not by import
(avoids storage ↔ transport cycle).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BlobRef(BaseModel):
    """Reference to a content-addressed blob.

    `store_key` is the opaque handle returned by `BlobStore.put` and
    consumed by `BlobStore.get`. For the FS impl it is the absolute path;
    for a future S3/MinIO impl it would be the `s3://bucket/key` URI.
    Callers MUST treat it as opaque.

    Sentinel BlobRefs (`is_sentinel=True`) are sparse — only `store_key` is
    meaningful; `content_hash`, `size`, `mime`, etc. carry placeholder values.
    Produced by `HttpBlobStore.exists()` where HEAD has no body. See
    `AGENTS.md §HttpBlobStore.exists`.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    store_key: str = Field(
        description="Opaque handle for BlobStore.get — do not parse."
    )
    content_hash: str = Field(description="sha256(data) as lower-case hex.")
    mime: str
    size: int = Field(ge=0)
    filename: str | None = None
    source: str = Field(
        description="Ingestion source: 'telegram', 'discord', 'tts', 'stt', ..."
    )
    platform_ref: str | None = Field(
        default=None,
        description=(
            "Recovery handle on source platform "
            "(e.g. 'tg:<file_id>', 'discord:<ch>/<msg>/<att>')."
        ),
    )
    platform_message_id: str | None = Field(
        default=None,
        description="Distinct from platform_ref; reconstructs the conversation thread.",
    )
    id: int | None = Field(
        default=None,
        description="SQLite blob_refs row id; None for non-FS impls or pre-put state.",
    )
    created_at: datetime = Field(description="Provenance: when this ref was ingested.")
    is_sentinel: bool = Field(
        default=False,
        description=(
            "True for sparse BlobRefs from HEAD-only paths (HttpBlobStore.exists). "
            "When True, content_hash/size/mime/created_at are placeholders — "
            "callers must not use them. "
            "Sparse HEAD-only refs carry content_hash='' guarded by is_sentinel."
        ),
    )

    @field_validator("created_at")
    @classmethod
    def _ensure_tz_aware(cls, v: datetime) -> datetime:
        """Reject naive datetimes — callers downstream rely on `astimezone(...)`."""
        if v.tzinfo is None:
            raise ValueError(
                "BlobRef.created_at must be timezone-aware (got naive datetime)"
            )
        return v

    @model_validator(mode="after")
    def _require_content_hash_unless_sentinel(self) -> BlobRef:
        # Mirrors roxabi_contracts.BlobRef guard: content_hash=="" is only valid
        # for sentinel BlobRefs; otherwise it would silently pass a wrong-typed
        # value to any downstream sha256/dedup check (#1367).
        if self.content_hash == "" and not self.is_sentinel:
            raise ValueError(
                "BlobRef.content_hash must be non-empty unless is_sentinel=True"
            )
        return self
