"""Channel-agnostic StreamProcessor: LlmEvent → RenderEvent pipeline.

Consumes an async stream of ``LlmEvent`` objects (from any LLM driver) and
produces ``RenderEvent`` objects consumed by outbound adapters (Telegram,
Discord, TTS tee, turn logger).

Pipeline contract (v2)
-----------------------
- ``TextLlmEvent``    → emit ``TextStartRenderEvent`` on first chunk, then one
                         ``TextDeltaRenderEvent`` per chunk, then
                         ``TextEndRenderEvent`` at block boundary.
- ``ToolUseLlmEvent`` → emit ``ToolCallStartRenderEvent`` / ``ToolCallArgsRenderEvent``
                         / ``ToolCallEndRenderEvent`` lifecycle events.
- ``ResultLlmEvent``  → close open text block (``TextEndRenderEvent``), synthesize
                         orphan ``ToolCallEnd`` events, emit ``RunFinishedRenderEvent``.

Hexagonal boundary
------------------
No imports from ``aiogram``, ``discord``, or ``anthropic`` are permitted here.
Only stdlib and factory-internal modules may be used.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from typing import TYPE_CHECKING, assert_never

if TYPE_CHECKING:
    from factory.core.messaging.messages import MessageManager
    from roxabi_contracts.errors import WorkerError

from factory.core.exceptions import (
    HubUnavailableError,
    StreamChunkTimeout,
    WorkerUnavailableError,
)
from factory.core.messaging.events import (
    LlmEvent,
    ResultLlmEvent,
    TextLlmEvent,
    ThinkingLlmEvent,
    ToolResultLlmEvent,
    ToolUseDeltaLlmEvent,
    ToolUseEndLlmEvent,
    ToolUseLlmEvent,
)
from factory.core.messaging.render_events import (
    RenderEvent,
    RunErrorRenderEvent,
    RunFinishedRenderEvent,
    RunStartedRenderEvent,
    TextEndRenderEvent,
)
from factory.core.messaging.utils.user_error_resolver import resolve_user_error
from factory.core.processors.stream_close import StreamCloseHandler
from factory.core.processors.stream_text import StreamTextHandler
from factory.core.processors.stream_tool import StreamToolHandler
from factory.core.trace import TraceContext
from factory.errors import ProviderError
from factory.streaming.event_emitter import EventEmitter
from factory.streaming.state_machine import StateMachine
from factory.transport import SanitizedError

log = logging.getLogger(__name__)

_STREAM_INFRA_ERRORS: tuple[type[BaseException], ...] = (
    ProviderError,
    WorkerUnavailableError,
    HubUnavailableError,
    StreamChunkTimeout,
    OSError,
    ConnectionError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
)


class StreamProcessor:
    """Translate an ``AsyncIterator[LlmEvent]`` into ``AsyncIterator[RenderEvent]``.

    One instance per turn — do not reuse across turns.

    Implements (duck-typed) the ``factory.streaming.Parser[LlmEvent, RenderEvent]``
    Protocol via ``process`` (maps to ``feed``), ``finalize``, and ``is_done``.
    Composed, not inherited — see spec #1282 §Breadboard.

    Parameters
    ----------
    show_intermediate:
        When ``True`` (default), ``TextDeltaRenderEvent`` chunks are emitted
        as they arrive so adapters can display text progressively.
        When ``False``, text is silently accumulated and closed via
        ``TextEndRenderEvent`` at the end of the block with no intermediate
        deltas.
    """

    def __init__(
        self,
        *,
        show_intermediate: bool = True,
        msg_manager: MessageManager | None = None,
        bot_name: str | None = None,
    ) -> None:
        self._show_intermediate = show_intermediate
        self._msg_manager = msg_manager
        self._bot_name = bot_name

        # --- Slice 3 (#1282) StateMachine instances (one per concern) ---
        # _sm_text: open text block (key = block_id, value = "text" sentinel).
        # _sm_reasoning: open reasoning block (key = block_id, value = "reasoning").
        # _sm_tool: open tool call IDs (key = tool_id, value = tool_name).
        self._sm_text: StateMachine[str, str] = StateMachine()
        self._sm_reasoning: StateMachine[str, str] = StateMachine()
        self._sm_tool: StateMachine[str, str] = StateMachine()

        # tool_id → tool_name map; kept as plain dict because the lookup
        # semantics differ from StateMachine open/close (we need to read the
        # name even after the tool call has ended).
        self._tool_id_to_name: dict[str, str] = {}

        # --- Extracted sub-handlers (Slice 3 / #1282 / #1590) ---
        # Circular references are resolved after instantiation: handlers are
        # created with None close_handler, then wired together.
        self._text_handler = StreamTextHandler(
            self._sm_text, self._sm_reasoning, show_intermediate, None
        )
        self._tool_handler = StreamToolHandler(
            self._sm_tool, self._tool_id_to_name, None, self._sm_text
        )
        self._close_handler = StreamCloseHandler(
            self._sm_text,
            self._sm_reasoning,
            self._sm_tool,
            self._tool_handler,
        )
        # Wire back-references (close_handler needs tool_handler; tool_handler
        # needs close_handler for reasoning cleanup on every non-Thinking event).
        self._text_handler._close_handler = self._close_handler
        self._tool_handler._close_handler = self._close_handler

        # --- reuse guard ---
        self._consumed: bool = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def process(
        self, events: AsyncIterator[LlmEvent]
    ) -> AsyncGenerator[RenderEvent, None]:
        """Process an async stream of ``LlmEvent`` objects.

        Yields ``RenderEvent`` objects as they are produced.

        Uses the local ``EventEmitter`` only for ``emit_terminal``;
        ``flush``/``emit_ok`` are intentionally unused — events are yielded
        directly. See ``streaming/AGENTS.md`` §Ordering rule for the deferred
        narrowing rationale.

        Parameters
        ----------
        events:
            Async iterator of ``LlmEvent`` objects from any LLM driver.

        Yields
        ------
        RenderEvent
            **v2 Text triplet (Slice 2, #1099):** for each contiguous text block,
            emits ``TextStartRenderEvent`` (once, on first chunk), then one
            ``TextDeltaRenderEvent`` per chunk, then ``TextEndRenderEvent`` at the
            block boundary (ToolUse, ResultLlmEvent, truncation, or exception).
            Interleaved text→tool→text sequences produce multiple bracketed blocks
            each with an independent ``message_id``.

            ``ToolCallStartRenderEvent`` / ``ToolCallArgsRenderEvent`` /
            ``ToolCallEndRenderEvent`` are emitted for tool-call lifecycle.
            ``RunStartedRenderEvent`` opens the turn; ``RunFinishedRenderEvent``
            closes it.

        Notes
        -----
        Single-flight: the instance may only be used for one call to ``process()``.
        ``_result_*`` attrs are reset here so no state leaks across calls (the
        reuse guard in ``_mark_consumed`` also enforces single-flight).
        """
        self._mark_consumed()
        run_id = TraceContext.get_trace_id() or f"synthetic-{TraceContext.generate()}"
        # Slice 4 (#1282) — SanitizedError boundary: instantiate a local emitter
        # whose translator closes over ``run_id`` (a per-call value that cannot
        # be captured in __init__). The emitter is purely a translation gateway;
        # ``emit_ok`` / ``flush`` are not used here.
        #
        # RenderEvent is a TypeAlias (Union[...]), not a concrete class, so the
        # Generic bound is left inferred by the translator return type.
        # #1113: propagate the originating SanitizedError.code (a KNOWN_CODES
        # key, e.g. ``stream.error`` for infra exceptions or a WorkerError code
        # such as ``cli.auth`` for soft errors) onto RunErrorRenderEvent.code,
        # rather than discarding it as ``None``.
        _emitter: EventEmitter[RunErrorRenderEvent] = EventEmitter(
            error_translator=lambda err: RunErrorRenderEvent(
                run_id=run_id, message=err.message, code=err.code
            )
        )
        yield RunStartedRenderEvent(run_id=run_id)
        # Reset soft-error state; written by _process_events, read post-finally.
        self._result_is_error = False
        self._result_worker_error: "WorkerError | None" = None
        self._result_error_text: str | None = None
        try:
            async for re in self._process_events(events):
                yield re
        except _STREAM_INFRA_ERRORS as exc:
            # Slice 1 (#1098): infrastructure-level exception during stream
            # processing. Surface a RunErrorRenderEvent then re-raise so the
            # adapter's existing exception handler (sets stream_error and
            # falls through to classify_stream_error) keeps working.
            #
            # message=type(exc).__name__ — never str(exc): exception strings can
            # carry file paths, internal hostnames, auth-token fragments from
            # httpx/aiohttp errors, DB connection strings, etc. RunErrorRenderEvent
            # is published on the NATS bus where any subscriber can read it.
            for re in self._close_handler.close_exception_path():
                yield re
            # Slice 4 (#1282) T15 — Site A: infrastructure exception path.
            # message=type(exc).__name__ — never str(exc) (exception strings
            # can carry file paths, auth tokens, hostnames).
            # yield from is invalid in async generators; use for loop instead.
            for _ev in _emitter.emit_terminal(
                SanitizedError(
                    code="stream.error",
                    message=type(exc).__name__,
                    retryable=False,
                )
            ):
                yield _ev
            raise
        finally:
            # Eagerly finalize the input iterator on both success and exception
            # paths so generators holding resources (e.g. CLI subprocess pipes)
            # release them deterministically rather than on GC.
            _aclose = getattr(events, "aclose", None)
            if _aclose is not None:
                await _aclose()
        if self._result_is_error:
            # Soft error: LLM backend returned an error response. The run
            # completed cleanly (no infrastructure exception), but the user
            # should still see an ❌ prefix on the rendered message. Emit
            # RunErrorRenderEvent instead of RunFinishedRenderEvent so the
            # adapter's dispatch ladder can flag the turn as error.
            # Slice 4 (#1282) T15 — Site B: route through EventEmitter.
            # worker_error carries driver-curated message (already sanitized
            # upstream by cli_streaming_parser) — safe for NATS broadcast.
            # F5 (security): when _we is None, _result_error_text is raw
            # upstream wire content; scrub via SanitizedError.from_message
            # before publishing on the NATS bus.
            _we = self._result_worker_error
            user_msg = resolve_user_error(
                worker_error=_we,
                error_text=self._result_error_text,
                msg_manager=self._msg_manager,
                bot_name=self._bot_name,
            )
            if _we is not None:
                _sanitized = SanitizedError(
                    code=_we.code,
                    message=user_msg,
                    retryable=_we.retryable,
                )
            else:
                _sanitized = SanitizedError(
                    code="stream.error",
                    message=user_msg,
                    retryable=False,
                )
            for _ev in _emitter.emit_terminal(_sanitized):
                yield _ev
        else:
            yield RunFinishedRenderEvent(run_id=run_id, outcome="success")

    async def _process_events(
        self, events: AsyncIterator[LlmEvent]
    ) -> AsyncGenerator[RenderEvent, None]:
        """Consume the raw LlmEvent stream and yield RenderEvents.

        Separated from ``process()`` so the outer shell owns only the
        try/except/finally lifecycle envelope (≤5 branches), keeping it below
        PLR0912. Captures soft-error state onto ``self._result_*`` attrs so
        ``process()`` can inspect them post-finally.

        Parameters
        ----------
        events:
            The same iterator passed to ``process()``. Finalization (``aclose``)
            is the caller's responsibility — done in ``process()``'s finally block.
        """
        _result_received = False
        async for event in events:
            if isinstance(event, ResultLlmEvent):  # pyright: ignore[reportUnnecessaryIsInstance] — DEBT:defensive-narrow-payloads
                _result_received = True
                self._result_is_error = event.is_error
                self._result_worker_error = event.worker_error
                self._result_error_text = event.error_text
            for re in self._dispatch_event(event):
                yield re

        # Stream ended without ResultLlmEvent (truncation or upstream error)
        if not _result_received:
            for re in self._close_handler.close_truncated_stream():
                yield re

    # ------------------------------------------------------------------
    # Per-LlmEvent sub-handlers (Slice 3, #1282)
    # ------------------------------------------------------------------

    def _dispatch_event(self, event: LlmEvent) -> Iterator[RenderEvent]:
        """Route a single ``LlmEvent`` to the appropriate sub-handler.

        Returns a synchronous ``Iterator[RenderEvent]`` — all sub-handlers are
        pure synchronous generators (no ``await`` inside). The caller uses
        ``yield from`` to forward events into the outer async generator.

        The ``assert_never`` fallthrough (cross-slice invariant 3) ensures that
        when the ``LlmEvent`` union widens without a matching branch being added
        here, pyright surfaces the gap at type-check time AND an ``AssertionError``
        is raised at runtime — so no new event type is ever silently dropped.
        """
        if isinstance(event, TextLlmEvent):
            yield from self._text_handler.handle_text(event)
        elif isinstance(event, ToolUseLlmEvent):
            yield from self._tool_handler.handle_tool_use(event)
            if self._show_intermediate:
                self._text_handler.clear_pending_text()
        elif isinstance(event, ToolUseDeltaLlmEvent):
            yield from self._tool_handler.handle_tool_use_delta(event)
        elif isinstance(event, ToolUseEndLlmEvent):
            yield from self._tool_handler.handle_tool_use_end(event)
        elif isinstance(event, ToolResultLlmEvent):
            yield from self._tool_handler.handle_tool_result(event)
        elif isinstance(event, ResultLlmEvent):  # pyright: ignore[reportUnnecessaryIsInstance] — DEBT:defensive-narrow-payloads
            yield from self._handle_result(event)
        elif isinstance(event, ThinkingLlmEvent):  # pyright: ignore[reportUnnecessaryIsInstance]
            yield from self._text_handler.handle_thinking(event)
        else:
            assert_never(event)  # pyright: ignore[reportUnreachableCode] — DEBT:defensive-narrow-payloads

    def _handle_result(self, event: ResultLlmEvent) -> Iterator[RenderEvent]:
        """Close open text/reasoning blocks; synthesize orphan ToolCallEnds.

        Name collision: CSP takes ``dict``, SP takes ``ResultLlmEvent`` —
        intentional shadowing by class context, NOT shared behavior. Cross-ref:
        the other ``_handle_result`` in ``CliStreamingParser``.
        """
        yield from self._close_handler.close_reasoning_if_open()
        # ───── Slice 2 (#1099) v2 Text triplet — close open block ─────
        open_text = next(iter(self._sm_text.open_blocks), None)
        if open_text is not None:
            yield TextEndRenderEvent(message_id=open_text)
            self._sm_text.close(open_text)
        # Synthesize ToolCallEnd for any open tool_call_ids that
        # never received a content_block_stop (truncated stream,
        # partial tool call). Loud WARN log per orphan.
        yield from self._tool_handler.synth_orphan_tool_ends()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _mark_consumed(self) -> None:
        """Guard against reuse — raise if process() was already called."""
        if self._consumed:
            raise RuntimeError(
                "StreamProcessor.process() called more than once. "
                "Create a new instance per turn."
            )
        self._consumed = True


__all__ = ["StreamProcessor"]
