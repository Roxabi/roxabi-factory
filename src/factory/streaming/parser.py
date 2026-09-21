"""Parser[InT, OutT] Protocol — stream-axis primitive.

Defines the structural contract shared by all streaming parsers in lyra.
Implementations compose (not inherit) this Protocol.

Concrete implementations (duck-typed, Phase 5 — #1282):
  - factory.core.cli.cli_streaming_parser.CliStreamingParser  (str → LlmEvent)
  - factory.core.processors.stream_processor.StreamProcessor  (LlmEvent → RenderEvent)
"""

from __future__ import annotations

from typing import Generic, Iterable, Protocol, TypeVar, runtime_checkable

# Variance enforced by static analysis (pyright). @runtime_checkable is
# shape-only — isinstance(x, Parser) does not see variance. See streaming/AGENTS.md
# §Protocol is structural.
InT = TypeVar("InT", contravariant=True)
OutT = TypeVar("OutT", covariant=True)


@runtime_checkable
class Parser(Protocol, Generic[InT, OutT]):
    """Stream-axis parser: per-source impls consume InT, emit OutT.

    Composed (not inherited) by CliStreamingParser (str → LlmEvent) and
    StreamProcessor (LlmEvent → RenderEvent). See spec #1282 §Breadboard.
    """

    def feed(self, item: InT) -> Iterable[OutT]: ...
    def finalize(self) -> Iterable[OutT]: ...
    def is_done(self) -> bool: ...
