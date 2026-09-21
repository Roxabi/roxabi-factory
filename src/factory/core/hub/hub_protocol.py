"""Protocol types for the hub: ChannelAdapter, RoutingKey, Binding.

``ChannelAdapter`` is a **role interface** (Fowler) co-located with the hub
sub-domain. It describes the contract a channel (Telegram/Discord/CLI/NATS)
must fulfil to plug into the hub. RoutingKey and Binding share the same hub
sub-domain vocabulary — splitting them across packages would obscure that
cohesion.

Why it does NOT live in ``core/ports/``: that directory holds **driven
(secondary) ports** (Cockburn) — capabilities the domain consumes from the
outside world (LLM, TTS, STT, audit). ``ChannelAdapter`` is internal: it
defines the role channels play *inside* the hub, not an external capability.

ISP debt
--------
``ChannelAdapter`` currently fuses two distinct collaborations:

- inbound (driver direction): ``normalize``, ``normalize_audio`` — channels
  drive the hub by surfacing platform events as ``InboundMessage``.
- outbound (driven direction): ``send``, ``send_streaming`` — the hub drives
  channels to emit responses.

In a fully orthodox hexagonal split this would become two ports
(``MessageReceiver`` + ``MessageSender``) living under ``core/ports/inbound/``
and ``core/ports/outbound/``. See the "orthodoxie pure" note in
``src/factory/core/AGENTS.md`` — tracked as future work, not blocking.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from ..auth.trust import TrustLevel
from ..messaging.message import (
    InboundMessage,
    OutboundAttachment,
    OutboundAudio,
    OutboundAudioChunk,
    OutboundMessage,
    Platform,
)

if TYPE_CHECKING:
    from ..messaging.render_events import RenderEvent


class ChannelAdapter(Protocol):
    """Interface every channel adapter must implement.

    Security contract: adapters are responsible for verifying the identity
    of the sender (e.g. via platform token, signed webhook, or session)
    before constructing an InboundMessage. The hub trusts ``InboundMessage.user_id``
    as the authenticated sender identity (used for rate limiting and pairing) and
    ``InboundMessage.scope_id`` as the conversation scope (used for pool routing).
    Never derive either from unverified inbound data.
    """

    def normalize(self, raw: Any) -> InboundMessage: ...

    def normalize_audio(
        self,
        raw: Any,
        audio_bytes: bytes,
        mime_type: str,
        *,
        trust_level: TrustLevel,
        pending: Any = None,
    ) -> InboundMessage: ...

    async def send(
        self, original_msg: InboundMessage, outbound: OutboundMessage
    ) -> None: ...

    async def send_streaming(
        self,
        original_msg: InboundMessage,
        events: AsyncIterator[RenderEvent],
        outbound: OutboundMessage | None = None,
    ) -> None:
        """Stream response to the channel with edit-in-place.

        *events* yields ``RenderEvent`` objects from the ``StreamProcessor``
        pipeline: ``ToolCallStartRenderEvent`` / ``ToolCallArgsRenderEvent`` /
        ``ToolCallEndRenderEvent`` mid-turn followed by ``TextStartRenderEvent``
        / ``TextDeltaRenderEvent`` / ``TextEndRenderEvent`` (v2 triplet).

        When *outbound* is provided, adapters write the platform message ID
        to ``outbound.metadata["reply_message_id"]`` after sending the
        placeholder, mirroring the contract of :meth:`send`.
        """
        ...

    async def render_audio(self, msg: OutboundAudio, inbound: InboundMessage) -> None:
        """Send an outbound audio envelope (voice note) to the channel."""
        ...

    async def render_audio_stream(
        self,
        chunks: AsyncIterator[OutboundAudioChunk],
        inbound: InboundMessage,
    ) -> None:
        """Stream outbound audio chunks to the channel.

        Adapters buffer chunks via ``buffer_audio_chunks()`` and send when
        complete. Mirrors ``send_streaming()`` for text.
        """
        ...

    async def render_voice_stream(
        self,
        chunks: AsyncIterator[OutboundAudioChunk],
        inbound: InboundMessage,
    ) -> None:
        """Stream TTS audio to an active voice session (Discord voice channel).

        Adapters that support voice playback implement this method.
        """
        ...

    async def render_attachment(
        self, msg: OutboundAttachment, inbound: InboundMessage
    ) -> None:
        """Send an outbound attachment (image/video/document/file) to the channel."""
        ...


class RoutingKey(NamedTuple):
    """Routing key: (platform, bot_id, scope_id). Use scope_id='*' for wildcard."""

    platform: Platform
    bot_id: str
    scope_id: str

    def to_pool_id(self) -> str:
        """Canonical pool ID: '{platform.value}:{bot_id}:{scope_id}'.

        Use this method as the single source of truth for pool ID format (ADR-001 §4).
        Never construct the pool ID string inline.
        """
        return f"{self.platform.value}:{self.bot_id}:{self.scope_id}"


@dataclass(frozen=True)
class Binding:
    agent_name: str
    pool_id: str
    # Public-bot handle for ADR-090 §5 deny refusals on this route. Carries no
    # authorization — a denied PUBLIC sender is pointed here, never routed here.
    public_bot: str | None = None
