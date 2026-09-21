"""BasePlatformAdapter — compatibility alias for OutboundAdapterBase.

Root cause (P5 audit): issue #1660 requested a BasePlatformAdapter with
__init__, _register_routes stubs, and delegation of send_streaming/send to
OutboundAdapterBase.  Audit of the existing codebase shows that
OutboundAdapterBase (shared/_base_outbound.py) already provides the full
contract:

  - send()            — abstract, platform-specific reply delivery
  - send_streaming()  — concrete, shared OutboundEmitter algorithm
  - _make_emitter()   — abstract, platform-specific stage composition
  - _start_typing()   — abstract, typing indicator start
  - _cancel_typing()  — abstract, typing indicator cancel
  - configure_tool_display()   — post-construction setter (avoids MRO issues)
  - configure_typing_publisher() — post-construction setter

Correction class: Patch (no new structure needed — existing class satisfies
the contract).  Introducing a parallel ABC would create a nominal hierarchy
without behavioural value and risk MRO complexity on DiscordAdapter
(discord.Client must remain first in MRO).

Decision: BasePlatformAdapter is exported as a direct alias of
OutboundAdapterBase.  New platform adapters should subclass
OutboundAdapterBase directly (the canonical name); BasePlatformAdapter
exists for callers that import via this module.

Why no __init__ or _register_routes:
  - OutboundAdapterBase intentionally has no __init__ (see _base_outbound.py
    header and AGENTS.md: "OutboundAdapterBase has NO __init__") to preserve
    cooperative MRO with discord.Client.
  - _register_routes is not a cross-platform concern — Telegram uses FastAPI
    routes; Discord uses gateway events; future platforms may use neither.
    A stub here would be misleading.  Platform-specific registration belongs
    in each adapter's bootstrap / _register_routes method.
"""

from __future__ import annotations

from factory.adapters.shared._base_outbound import OutboundAdapterBase

__all__ = ["BasePlatformAdapter"]

# Alias — no new class body needed.
BasePlatformAdapter = OutboundAdapterBase
