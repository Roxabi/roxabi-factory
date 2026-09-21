"""Pure JSON event parser for CLI streaming protocol.

Extracted from cli_streaming.py — parses NDJSON lines into LlmEvents
without any I/O or async concerns.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from typing import Iterable

from roxabi_contracts.errors import KNOWN_CODES, WorkerError

from ...streaming.event_emitter import EventEmitter
from ...streaming.state_machine import StateMachine
from ...transport import SanitizedError
from ..messaging.events import (
    LlmEvent,
    ResultLlmEvent,
    TextLlmEvent,
    ThinkingLlmEvent,
    ToolResultLlmEvent,
    ToolUseDeltaLlmEvent,
    ToolUseEndLlmEvent,
    ToolUseLlmEvent,
)
from ..messaging.utils.metrics import emit_populated_total
from .cli_error_classify import _resolve_cli_worker_error

# Anthropic CLI NDJSON wire constants — kept here so the parser is the single
# source of truth on what shapes upstream emits.
_DELTA_INPUT_JSON = "input_json_delta"
_BLOCK_TYPE_TOOL_USE = "tool_use"
_BLOCK_TYPE_TOOL_RESULT = "tool_result"
_BLOCK_TYPE_THINKING = "thinking"
_DELTA_THINKING = "thinking_delta"
_DELTA_SIGNATURE = "signature_delta"

log = logging.getLogger(__name__)


class CliStreamingParser:
    """Pure JSON event parser for CLI streaming protocol.

    Parses NDJSON lines from the CLI subprocess stdout into LlmEvent objects.
    Maintains session state (session_id, error) across parse calls.

    Implements the ``factory.streaming.Parser[str, LlmEvent]`` Protocol via
    ``feed`` (alias of ``parse_line``), ``finalize``, and ``is_done``.
    Composed, not inherited — see streaming/AGENTS.md §Protocol is structural.
    """

    def __init__(self, pool_id: str) -> None:
        self.pool_id = pool_id
        self.session_id: str | None = None
        self.error: str | None = None
        self._had_text_delta = False
        self._done = False
        self._pending: deque[LlmEvent] = deque()
        # StateMachine[str, None] — dedup guard: tool_ids already announced.
        # mark_seen(tool_id) returns True on first call only; second call
        # (post-hoc assistant duplicate) is silently dropped.
        self._sm_dedup: StateMachine[str, None] = StateMachine()
        # StateMachine[int, str] — open tool blocks: index → tool_id.
        # open(idx, tool_id) on content_block_start; close(idx) returns
        # tool_id on content_block_stop.
        self._sm_tool_blocks: StateMachine[int, str] = StateMachine()
        # StateMachine[int, int] — open thinking block: index → index (sentinel).
        # open(idx, idx) on thinking content_block_start; close(idx) on stop.
        # Only one thinking block open at a time per turn.
        self._sm_thinking: StateMachine[int, int] = StateMachine()
        # Slice 4 (#1282) T16 — SanitizedError boundary for the cli.parse terminal
        # envelope. Translator wraps SanitizedError → ResultLlmEvent(is_error=True).
        # session_id is captured at emit time (see _handle_json_decode_error).
        self._emitter: EventEmitter[ResultLlmEvent] = EventEmitter(
            error_translator=lambda err: ResultLlmEvent(
                is_error=True,
                duration_ms=0,
                cost_usd=None,
                error_text=err.message,
                session_id=self.session_id,
                worker_error=WorkerError(
                    code=err.code,
                    message=err.message,
                    retryable=err.retryable,
                ),
            )
        )

    # -- backward-compat properties so existing tests that probe internal state
    # -- continue to pass without modification (test_cli_streaming_parse.py:994,1113).

    @property
    def _open_thinking_index(self) -> int | None:
        """Compat shim — index of the currently open thinking block, or None.

        Returns a scalar (never a mutable ref). Mutations are not possible.
        """
        if self._sm_thinking.open_blocks:
            return next(iter(self._sm_thinking.open_blocks))
        return None

    @property
    def _open_tool_blocks(self) -> dict[int, str]:
        """Compat shim — returns a SHALLOW COPY of open tool blocks.

        Mutations to the returned dict do NOT affect parser state.
        """
        return dict(self._sm_tool_blocks.open_blocks)

    # -- Parser[str, LlmEvent] Protocol aliases (see streaming/AGENTS.md §Protocol)
    # -- ``feed`` is the protocol name; ``parse_line`` is the legacy public API.
    # -- Both are kept for backward compatibility with existing callers.

    def feed(self, item: str) -> deque[LlmEvent]:
        """Parser Protocol alias for ``parse_line``.

        Satisfies ``factory.streaming.Parser[str, LlmEvent].feed`` so that
        this class passes ``isinstance(parser, Parser)`` checks and can be
        used wherever a ``Parser`` is expected without an adapter layer.
        """
        return self.parse_line(item)

    def finalize(self) -> deque[LlmEvent]:
        """Parser Protocol: flush any remaining buffered events.

        For ``CliStreamingParser`` the pending deque is drained incrementally
        by ``parse_line``; ``finalize`` returns whatever is still buffered
        (e.g. after a truncated stream) and marks the parser as done.
        """
        self._done = True
        return self._pending

    def is_done(self) -> bool:
        """Parser Protocol: return True once a terminal result event was parsed."""
        return self._done

    def parse_line(self, line: str) -> deque[LlmEvent]:
        """Parse a JSON line, update state, and return events to yield.

        Returns a deque of LlmEvent objects. Caller should pop from left.
        Sets self._done = True when result event is parsed.
        """
        if self._done or not line:
            return self._pending

        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            self._handle_json_decode_error(line, exc)
            return self._pending

        msg_type = data.get("type", "")
        handler = {
            "assistant": self._handle_assistant,
            "stream_event": self._handle_stream_event,
            "user": self._handle_user_tool_result,
            "result": self._handle_result,
        }.get(msg_type)

        if handler is not None:
            for ev in handler(data):
                self._pending.append(ev)
        elif msg_type == "system" and data.get("subtype") == "init":
            self._handle_system_init(data)

        return self._pending

    def _handle_json_decode_error(self, line: str, exc: json.JSONDecodeError) -> None:
        """Handle JSON decode failure — emit cli.parse terminal envelope.

        Spec C4 path (b): a `{`-shaped line that fails to parse is protocol
        corruption. Non-JSON lines (debug output, blank lines) are silently
        skipped. Kept inline for T16 (Slice 4 wires via EventEmitter).
        """
        if not line.lstrip().startswith("{"):
            return
        # Sanitize bus-bound message (#1219, sibling of #1212/#1215).
        # JSONDecodeError.__str__ on current CPython is well-behaved,
        # but defense-in-depth keeps `.doc` content (the raw malformed
        # line) off the bus across Python versions and custom decoder
        # subclasses. Full diagnostic preserved in log.warning below.
        log.warning("CLI JSON parse error: %r", exc)
        meta = KNOWN_CODES["cli.parse"]
        # Slice 4 (#1282) T16 — route cli.parse terminal envelope through
        # EventEmitter. The exact message format is preserved for downstream
        # test compatibility (test_worker_error_e2e.py asserts this string).
        # type(exc).__name__ is a deterministic, sanitized identifier — not
        # a cascade producer.
        emit_populated_total(domain="cli")
        self._done = True
        for result_event in self._emitter.emit_terminal(
            SanitizedError(
                code="cli.parse",
                message=f"CLI emitted malformed JSON: {type(exc).__name__}",
                retryable=meta.default_retryable,
            )
        ):
            self._pending.append(result_event)

    def _handle_system_init(self, data: dict) -> Iterable[LlmEvent]:
        """Handle system/init — capture session_id, emit nothing."""
        self.session_id = data.get("session_id", "") or None
        log.debug(
            "[pool:%s] streaming init: session=%s",
            self.pool_id,
            self.session_id,
        )
        return ()

    def _handle_assistant(self, data: dict) -> Iterable[LlmEvent]:
        """Handle assistant message — emit ToolUseLlmEvent for unseen tool_ids."""
        blocks = data.get("message", {}).get("content", [])
        events: list[LlmEvent] = []
        for b in blocks:
            if b.get("type") != _BLOCK_TYPE_TOOL_USE:
                continue
            tool_id = b.get("id", "")
            # Slice 3 dedupe (#1100 review): CLI emits the tool_use block twice —
            # once at content_block_start and again post-hoc here (input populated).
            # Skip post-hoc duplicate; args are reconstructed from delta stream.
            if tool_id and not self._sm_dedup.mark_seen(tool_id):
                continue
            events.append(
                ToolUseLlmEvent(
                    tool_name=b.get("name", ""),
                    tool_id=tool_id,
                    input=b.get("input", {}),
                )
            )
        return events

    def _handle_stream_event(self, data: dict) -> Iterable[LlmEvent]:
        """Handle stream_event — dispatch content_block_start/delta/stop."""
        event_data = data.get("event", data)
        event_type = event_data.get("type", "")
        if event_type == "content_block_start":
            return self._handle_content_block_start(event_data)
        if event_type == "content_block_delta":
            return self._handle_content_block_delta(event_data)
        if event_type == "content_block_stop":
            return self._handle_content_block_stop(event_data)
        return ()

    def _handle_content_block_start(self, event_data: dict) -> Iterable[LlmEvent]:
        """Handle content_block_start — register thinking or tool_use blocks."""
        cb = event_data.get("content_block", {})
        cb_type = cb.get("type")
        if cb_type == _BLOCK_TYPE_THINKING:
            idx = event_data.get("index")
            if isinstance(idx, int):
                self._sm_thinking.open(idx, idx)
            # No event emitted on start — emission happens on first delta.
            return ()
        if cb_type == _BLOCK_TYPE_TOOL_USE:
            tool_id = cb.get("id", "")
            idx = event_data.get("index")
            if isinstance(idx, int) and tool_id:
                self._sm_tool_blocks.open(idx, tool_id)
            if tool_id and self._sm_dedup.mark_seen(tool_id):
                return [
                    ToolUseLlmEvent(
                        tool_name=cb.get("name", ""),
                        tool_id=tool_id,
                        input={},
                    )
                ]
        return ()

    def _handle_content_block_delta(self, event_data: dict) -> Iterable[LlmEvent]:
        """Handle content_block_delta — emit text, thinking, or tool delta events."""
        delta = event_data.get("delta", {})
        delta_type = delta.get("type", "")
        if delta_type == _DELTA_THINKING:
            idx = event_data.get("index")
            thinking = delta.get("thinking", "")
            if isinstance(idx, int) and self._sm_thinking.is_open(idx) and thinking:
                return [ThinkingLlmEvent(text=thinking)]
        elif delta_type == _DELTA_SIGNATURE:
            pass  # API-replay metadata; no user-facing render
        elif delta_type == "text_delta":
            text = delta.get("text", "")
            if text:
                self._had_text_delta = True
                return [TextLlmEvent(text=text)]
        elif delta_type == _DELTA_INPUT_JSON:
            idx = event_data.get("index")
            partial_json = delta.get("partial_json", "")
            if isinstance(idx, int) and partial_json:
                tool_id = self._sm_tool_blocks.get(idx)
                if tool_id is not None:
                    return [
                        ToolUseDeltaLlmEvent(
                            tool_id=tool_id,
                            partial_json=partial_json,
                        )
                    ]
        return ()

    def _handle_content_block_stop(self, event_data: dict) -> Iterable[LlmEvent]:
        """Handle content_block_stop — close open tool or thinking blocks."""
        idx = event_data.get("index")
        events: list[LlmEvent] = []
        if isinstance(idx, int):
            tool_id = self._sm_tool_blocks.close(idx)
            if tool_id is not None:
                events.append(ToolUseEndLlmEvent(tool_id=tool_id))
            if self._sm_thinking.is_open(idx):
                self._sm_thinking.close(idx)
        return events

    def _handle_user_tool_result(self, data: dict) -> Iterable[LlmEvent]:
        """Handle user message — emit ToolResultLlmEvent for tool_result blocks."""
        blocks = data.get("message", {}).get("content", [])
        events: list[LlmEvent] = []
        for b in blocks:
            if b.get("type") != _BLOCK_TYPE_TOOL_RESULT:
                continue
            content = b.get("content", "")
            # Anthropic CLI may pack content as a list of typed blocks.
            # Render text-only this slice; non-text blocks placeholder as
            # `[{type}]` so non-renderable payloads never silently vanish.
            if isinstance(content, list):
                parts = []
                for blk in content:
                    if isinstance(blk, dict):
                        if blk.get("type") == "text":
                            parts.append(str(blk.get("text", "")))
                        else:
                            parts.append(f"[{blk.get('type', '?')}]")
                content = "".join(parts)
            events.append(
                ToolResultLlmEvent(
                    tool_id=b.get("tool_use_id", ""),
                    content=str(content),
                    is_error=bool(b.get("is_error", False)),
                )
            )
        return events

    def _handle_result(self, data: dict) -> Iterable[LlmEvent]:
        """Handle result — terminal event; sets _done, emits ResultLlmEvent.

        Path (a) via _classify_cli_error preserved verbatim.
        Downgrade logic is_error=True + subtype=success + had_text_delta preserved.

        Name collision: CSP takes ``dict``, SP takes ``ResultLlmEvent`` —
        intentional shadowing by class context, NOT shared behavior. Cross-ref:
        the other ``_handle_result`` in ``StreamProcessor``.
        """
        sid = data.get("session_id", "")
        if sid:
            self.session_id = sid
        is_error = data.get("is_error", False)
        subtype = data.get("subtype", "")
        # Classify based on observed stream, not self-reported flags.
        # CLI reports is_error=True + subtype="success" both when:
        #   (a) a tool call failed but the model recovered and
        #       streamed a valid answer via text_delta events, and
        #   (b) the CLI exited early (auth failure, crash) and
        #       emitted the error text only in the result field.
        # Only (a) should downgrade to success — and the signal for
        # that is whether any text was actually streamed.
        if is_error and subtype == "success" and self._had_text_delta:
            log.info(
                "[pool:%s] streaming result is_error=True but"
                " subtype=success with streamed text — treating"
                " as success",
                self.pool_id,
            )
            is_error = False
        elif is_error:
            errors = data.get("errors", [])
            self.error = (
                errors[0]
                if errors
                else data.get("result") or subtype or "Unknown streaming error"
            )
            log.warning(
                "[pool:%s] streaming result is_error=True"
                " subtype=%s had_text_delta=%s duration_ms=%d"
                " result=%r",
                self.pool_id,
                subtype,
                self._had_text_delta,
                data.get("duration_ms", 0),
                (data.get("result") or "")[:200],
            )
        log.info(
            "[pool:%s] streaming result: %dms",
            self.pool_id,
            data.get("duration_ms", 0),
        )
        self._done = True
        # Path (a): upstream CLI emits is_error=True — classify to cli.* code.
        worker_error: WorkerError | None = None
        if is_error:
            worker_error = _resolve_cli_worker_error(subtype, self.error or "")
            emit_populated_total(domain="cli")
        return [
            ResultLlmEvent(
                is_error=is_error,
                duration_ms=data.get("duration_ms", 0),
                cost_usd=None,
                # #1252: route the scrubbed message — not raw ``self.error``
                # (upstream wire content) — to the bus, matching path (b).
                error_text=worker_error.message if worker_error else None,
                session_id=data.get("session_id") or None,
                worker_error=worker_error,
            )
        ]
