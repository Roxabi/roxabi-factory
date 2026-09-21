"""Self-contained value types for the LLM driven port.

Extracted from ``core/agent/agent_config.py`` (ModelConfig) and
``core/messaging/events.py`` (LlmEvent + friends) so that
``core/ports/llm.py`` is fully self-contained and imports nothing from
sibling ``core/`` subpackages.

Constraints
-----------
- stdlib + pydantic only — no framework imports (aiogram, discord, anthropic).
- No imports from ``factory.core.agent``, ``factory.core.messaging``, or any
  ``factory.*`` subpackage.  TYPE_CHECKING-only imports are permitted.

Immutability contract (LlmEvent variants)
-----------------------------------------
All event dataclasses use ``frozen=True`` which prevents re-assignment of
fields (``event.field = x`` raises ``FrozenInstanceError``) but does **not**
prevent in-place mutation of mutable containers (``event.input["k"] = v``
succeeds).  Callers must never mutate event objects after construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

if TYPE_CHECKING:
    from roxabi_contracts.errors import WorkerError

# ---------------------------------------------------------------------------
# ModelConfig
# ---------------------------------------------------------------------------

# Literal default kept here to preserve the ports isolation contract
# (stdlib + pydantic only — no factory.* imports at runtime).
# AgentDefaultsConfig.DEFAULT_MODEL must equal this value; both files
# hold the same string so either can be the canonical reference point.
_DEFAULT_MODEL: str = "claude-opus-4-6"

_VALID_BACKENDS: frozenset[str] = frozenset({"claude-cli", "nats", "omp-rpc"})


class ModelConfig(BaseModel):
    """Per-agent model configuration.

    backend: execution backend — "claude-cli" (Claude Code subscription)
             or "nats" (multi-provider via NATS → llmCLI worker).
    model:   model identifier passed to the backend CLI.
    max_turns: max agentic turns per conversation turn.
             None (or 0 in DB) means unlimited — the backend imposes no cap.
             Default is None (unlimited). Set an explicit positive integer to
             throttle long-running agents.
    tools:   allowed tools (empty = backend defaults).
    cwd:     working directory for the Claude subprocess (claude-cli only).
             None → defaults to the factory project root.
             Useful to point a dedicated agent at another project so it reads
             that project's AGENTS.md and has access to its files.
    This will evolve into an intelligent model selection system.
    """

    model_config = ConfigDict(frozen=True)

    backend: str = "claude-cli"
    model: str = _DEFAULT_MODEL
    max_turns: int | None = None  # None = unlimited (0 sentinel in DB)
    tools: tuple[str, ...] = ()
    # cwd is spawn-routing config, not model identity.
    # Changing cwd should not trigger the "model_config mismatch" warning
    # in CliPool.send() — that check is for backend/model/tools changes only.
    cwd: Path | None = None
    skip_permissions: bool = False
    streaming: bool = False
    # #1101 — per-agent extended-thinking config (effort token budget)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None

    @field_validator("backend")
    @classmethod
    def _validate_backend(cls, v: str) -> str:
        # Pydantic-level guard so direct construction (tests, NATS payload
        # deserialization, ad-hoc code) cannot bypass _VALID_BACKENDS.
        if v not in _VALID_BACKENDS:
            raise ValueError(
                f"Invalid backend {v!r}: must be one of {sorted(_VALID_BACKENDS)}"
            )
        return v

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ModelConfig):
            return NotImplemented
        return (
            self.backend == other.backend
            and self.model == other.model
            and self.max_turns == other.max_turns
            and self.tools == other.tools
            and self.skip_permissions == other.skip_permissions
            and self.streaming == other.streaming
            and self.effort == other.effort
            # cwd excluded — spawn-routing config, not model identity
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.backend,
                self.model,
                self.max_turns,
                self.tools,
                self.skip_permissions,
                self.streaming,
                self.effort,
                # cwd intentionally excluded
            )
        )


# ---------------------------------------------------------------------------
# LlmEvent variants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextLlmEvent:
    """A chunk of text from the LLM response stream."""

    text: str


@dataclass(frozen=True)
class ThinkingLlmEvent:
    """A chunk of extended-thinking text from the LLM (Slice 4 of #1096).

    Emitted by cli_streaming_parser on Anthropic CLI thinking_delta
    events (content_block.type == "thinking" + delta.type ==
    "thinking_delta" under --effort low|medium|high|xhigh|max).
    """

    text: str


@dataclass(frozen=True)
class ToolUseLlmEvent:
    """Emitted when the LLM calls a tool.

    ``input`` is empty at ``ContentBlockStart`` time (SDK); the full input dict
    is populated via ``InputJsonDelta`` events. On the clipool path the full
    args dict may be present at start time.
    """

    tool_name: str
    tool_id: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolUseDeltaLlmEvent:
    """Tool-use input streaming chunk (partial JSON fragment).

    Slice 3 of #1096. Emitted on Anthropic CLI ``content_block_delta`` events
    whose ``delta.type == "input_json_delta"`` (per ADR-028
    ``--include-partial-messages``). ``partial_json`` is only valid JSON when
    concatenated with the rest of the deltas for the same ``tool_id``.
    """

    tool_id: str
    partial_json: str


@dataclass(frozen=True)
class ToolUseEndLlmEvent:
    """Tool-use content block end (input streaming complete).

    Slice 3 of #1096. Emitted on Anthropic CLI ``content_block_stop`` events
    that close a previously-opened ``tool_use`` block.
    """

    tool_id: str


@dataclass(frozen=True)
class ToolResultLlmEvent:
    """Tool execution result, paired by ``tool_id`` with a prior ``ToolUseLlmEvent``.

    Slice 3 of #1096. Emitted on Anthropic CLI user-message blocks of
    ``type=tool_result``. ``content`` is rendered text-only this slice;
    list-of-typed-blocks are concatenated with ``[{type}]`` placeholders for
    non-text blocks (rich rendering deferred to a later slice).
    """

    tool_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ResultLlmEvent:
    """Final event in every stream — signals turn completion.

    ``cost_usd`` is always ``None`` for ``ClaudeCliDriver`` (not present in
    NDJSON result envelope).

    ``error_text`` is the driver-curated **presentation cache** of
    ``worker_error.message`` — an in-process convenience field surfaced
    directly to user-facing renderers (e.g. ``_shared_streaming_emitter``)
    so adapters don't have to reach into the structured ``worker_error``
    envelope for the display string. Populated by drivers/parsers (see
    ``cli_streaming_parser.py``); always co-populated with ``worker_error``
    on terminal failure events. ``None`` or empty on success. Not present on
    NATS wire contracts — only ``worker_error`` crosses the wire. See
    ADR-066 (absorbed into ADR-049) archive Status for the dual-field rationale.
    """

    is_error: bool
    duration_ms: int
    cost_usd: float | None = None
    error_text: str | None = None
    session_id: str | None = None
    worker_error: "WorkerError | None" = None


# Union type exported for type annotations and ``isinstance`` checks.
LlmEvent = (
    TextLlmEvent
    | ThinkingLlmEvent
    | ToolUseLlmEvent
    | ToolUseDeltaLlmEvent
    | ToolUseEndLlmEvent
    | ToolResultLlmEvent
    | ResultLlmEvent
)

__all__ = [
    "LlmEvent",
    "ModelConfig",
    "ResultLlmEvent",
    "TextLlmEvent",
    "ThinkingLlmEvent",
    "ToolResultLlmEvent",
    "ToolUseDeltaLlmEvent",
    "ToolUseEndLlmEvent",
    "ToolUseLlmEvent",
]
