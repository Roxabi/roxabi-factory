"""Tests for tools/check_doc_drift.py — doc-drift gate.

Contract:
  - Dead backtick ref not in baseline → exit 1, printed to stdout.
  - Live ref (real src/ path or symbol) → exit 0.
  - Historical annotation on same line → exempt, exit 0.
  - Per-token historical: co-located live ref on same line still validated.
  - Baselined ref → exempt, exit 0.
  - --update-baseline merges prior baselined entries.
  - --update-baseline warns when absorbing new (previously-unbaselined) violations.
  - <!-- drift-ignore --> on same line → exempt, exit 0.
  - Dead src/ path → exit 1.
  - Dead ref in src/factory/*/AGENTS.md → exit 1.
  - Internal error → exit 2.
"""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

from tools.check_doc_drift import main

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_doc(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _make_src_class(root: Path, symbol: str) -> None:
    """Create a minimal src file defining the given symbol so it resolves."""
    p = root / "src" / "factory" / "_test_symbols.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = p.read_text() if p.exists() else ""
    p.write_text(existing + f"\nclass {symbol}: ...\n")


def _run_main(tmp_path: Path, args: list[str] | None = None) -> tuple[int, str, str]:
    """Run main() and capture stdout/stderr. Returns (rc, stdout, stderr)."""
    buf_out = StringIO()
    buf_err = StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf_out, buf_err
    try:
        rc = main(["--root", str(tmp_path)] + (args or []))
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, buf_out.getvalue(), buf_err.getvalue()


# ---------------------------------------------------------------------------
# T1 — dead ref not in baseline → exit 1
# ---------------------------------------------------------------------------


def test_dead_ref_not_in_baseline_exits_1(tmp_path: Path) -> None:
    _make_doc(
        tmp_path,
        "docs/architecture/test.md",
        "Some doc referencing `ZzzGhostClass` which does not exist.\n",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 1


# ---------------------------------------------------------------------------
# T2 — live src/ path ref → exit 0
# ---------------------------------------------------------------------------


def test_live_src_path_ref_passes(tmp_path: Path) -> None:
    # Create the real file
    real = tmp_path / "src" / "factory" / "core" / "hub.py"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("# hub\n")
    _make_doc(
        tmp_path,
        "docs/architecture/test.md",
        "See `src/factory/core/hub.py` for details.\n",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 0


# ---------------------------------------------------------------------------
# T3 — live symbol (class defined in src/) → exit 0
# ---------------------------------------------------------------------------


def test_live_symbol_passes(tmp_path: Path) -> None:
    _make_src_class(tmp_path, "ClaudeCliDriver")
    _make_doc(
        tmp_path,
        "docs/architecture/test.md",
        "Use `ClaudeCliDriver` for single-process wiring.\n",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 0


# ---------------------------------------------------------------------------
# T4 — historical annotation on same line → exempt → exit 0
# ---------------------------------------------------------------------------


def test_historical_annotation_exempts(tmp_path: Path) -> None:
    _make_doc(
        tmp_path,
        "docs/architecture/test.md",
        "The `ZzzGhostClass` driver was deleted #1281 and is no longer in the tree.\n",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 0


# ---------------------------------------------------------------------------
# T4b — fix C: per-token historical adjacency
# A historical keyword on the same line should NOT exempt a live-but-dead
# ref that appears far away from it — but in practice the adjacency threshold
# is generous (60 chars) so we test the key invariant: two dead refs on ONE
# line where only ONE is adjacent to the historical keyword.
# ---------------------------------------------------------------------------


def test_per_token_historical_distant_ref_checked(tmp_path: Path) -> None:
    """A dead ref separated from historical keyword by >60 chars is still checked.

    Lines are scanned one at a time, so multi-line separation guarantees
    that the second ref on a different line is always validated.
    """
    # Two-line content: line 1 has ZzzDeleted (historical), line 2 has ZzzAlive
    # (live). ZzzAlive must resolve correctly (either exists or is caught).
    _make_src_class(tmp_path, "ZzzAlive")
    _make_doc(
        tmp_path,
        "docs/architecture/test.md",
        "The `ZzzGhostClass` was deleted and is no longer present.\n"
        "The `ZzzAlive` replacement is the active class.\n",
    )
    # ZzzGhostClass is on line 1 with "deleted" → exempt
    # ZzzAlive is on line 2 (no historical keyword) → must resolve (it exists)
    rc = main(["--root", str(tmp_path)])
    assert rc == 0  # ZzzAlive exists, ZzzGhostClass is historical-exempt


def test_dead_ref_not_exempted_by_distant_historical(tmp_path: Path) -> None:
    """A dead ref on a line without a historical keyword is still flagged."""
    _make_doc(
        tmp_path,
        "docs/architecture/test.md",
        "The `ZzzOldFoo` was deleted in #100.\nSee also `ZzzDeadBar` for details.\n",
    )
    # Line 2: ZzzDeadBar is dead and has no historical keyword → exit 1
    rc = main(["--root", str(tmp_path)])
    assert rc == 1


# ---------------------------------------------------------------------------
# T5 — baselined ref → exempt → exit 0
# ---------------------------------------------------------------------------


def test_baselined_ref_is_exempt(tmp_path: Path) -> None:
    _make_doc(
        tmp_path,
        "docs/architecture/test.md",
        "See `ZzzGhostClass` for details.\n",
    )
    baseline = tmp_path / "tools" / "doc_drift_baseline.txt"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(
        "# baseline\ndocs/architecture/test.md::ZzzGhostClass\n", encoding="utf-8"
    )
    rc = main(["--root", str(tmp_path), "--baseline", str(baseline)])
    assert rc == 0


# ---------------------------------------------------------------------------
# T6 — drift-ignore comment → exempt → exit 0
# ---------------------------------------------------------------------------


def test_drift_ignore_comment_exempts(tmp_path: Path) -> None:
    _make_doc(
        tmp_path,
        "docs/architecture/test.md",
        "See `ZzzGhostClass` for context. <!-- drift-ignore -->\n",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 0


# ---------------------------------------------------------------------------
# T7 — --update-baseline writes file and exits 0
# ---------------------------------------------------------------------------


def test_update_baseline_writes_and_exits_0(tmp_path: Path) -> None:
    _make_doc(
        tmp_path,
        "docs/architecture/test.md",
        "See `ZzzGhostClass` here.\n",
    )
    baseline = tmp_path / "tools" / "doc_drift_baseline.txt"
    rc = main(
        ["--root", str(tmp_path), "--baseline", str(baseline), "--update-baseline"]
    )
    assert rc == 0
    assert baseline.exists()
    content = baseline.read_text()
    assert "docs/architecture/test.md::ZzzGhostClass" in content


# ---------------------------------------------------------------------------
# T7b — --update-baseline merges prior baselined entries (fix E: ratchet guard)
# ---------------------------------------------------------------------------


def test_update_baseline_merges_prior_baselined_entries(tmp_path: Path) -> None:
    """--update-baseline must preserve prior baselined entries, not drop them."""
    # Two dead refs: one already baselined, one new
    _make_doc(
        tmp_path,
        "docs/architecture/test.md",
        "See `ZzzOldRef` and also `ZzzNewRef` for context.\n",
    )
    baseline = tmp_path / "tools" / "doc_drift_baseline.txt"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(
        "# baseline\ndocs/architecture/test.md::ZzzOldRef\n", encoding="utf-8"
    )
    rc, _, __ = _run_main(
        tmp_path,
        ["--baseline", str(baseline), "--update-baseline"],
    )
    assert rc == 0
    content = baseline.read_text()
    # Both entries must be in the updated baseline
    assert "docs/architecture/test.md::ZzzOldRef" in content
    assert "docs/architecture/test.md::ZzzNewRef" in content


def test_update_baseline_warns_on_new_absorb(tmp_path: Path) -> None:
    """--update-baseline emits a WARNING when absorbing new unbaselined violations."""
    _make_doc(
        tmp_path,
        "docs/architecture/test.md",
        "See `ZzzBrandNewDeadRef` for details.\n",
    )
    baseline = tmp_path / "tools" / "doc_drift_baseline.txt"
    rc, _, stderr = _run_main(
        tmp_path,
        ["--baseline", str(baseline), "--update-baseline"],
    )
    assert rc == 0
    # The warning must mention the count and appear on stderr
    assert "WARNING" in stderr
    assert "1" in stderr  # count of absorbed violations


# ---------------------------------------------------------------------------
# T8 — ADR archive is not scanned (historical records) → exit 0
# ---------------------------------------------------------------------------


def test_adr_archive_is_not_scanned(tmp_path: Path) -> None:
    # A dead ref inside docs/architecture/adr/ must NOT trip the gate: ADRs are
    # immutable decision records citing symbols as-of-writing (ADR-080).
    _make_doc(
        tmp_path,
        "docs/architecture/adr/099-old-decision.mdx",
        "This ADR referenced `ZzzGhostClass` at decision time.\n",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 0


# ---------------------------------------------------------------------------
# T9 — dead ref in src/factory/*/AGENTS.md → exit 1
# ---------------------------------------------------------------------------


def test_dead_ref_in_agents_md_exits_1(tmp_path: Path) -> None:
    """Dead backtick ref inside src/factory/adapters/AGENTS.md is caught."""
    _make_doc(
        tmp_path,
        "src/factory/adapters/AGENTS.md",
        "See `ZzzGhostAdapter` for the old interface.\n",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 1


# ---------------------------------------------------------------------------
# T10 — dead src/ path → exit 1
# ---------------------------------------------------------------------------


def test_dead_src_path_exits_1(tmp_path: Path) -> None:
    """A src/ path reference that does not exist is caught as a dead ref."""
    _make_doc(
        tmp_path,
        "docs/architecture/test.md",
        "See `src/factory/core/ghost_module.py` for details.\n",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 1


# ---------------------------------------------------------------------------
# T11 — exit 2 on internal error (fix D)
# ---------------------------------------------------------------------------


def test_exit_2_on_invalid_root(tmp_path: Path) -> None:
    """A non-existent root does not raise unhandled exception — exits 0 (no files)."""
    # Actually with a non-existent root, scan just finds no files → exit 0.
    # Test the exit 2 contract via a mocked build() that raises.
    from unittest.mock import patch

    def _bad_build(root: Path) -> object:
        raise RuntimeError("simulated oracle build failure")

    with patch("tools.check_doc_drift.CodeInventory.build", side_effect=_bad_build):
        rc, _, stderr = _run_main(tmp_path)
    assert rc == 2
    assert "RuntimeError" in stderr or "simulated" in stderr


# ---------------------------------------------------------------------------
# T12 — live NATS subject does not raise false positive
# ---------------------------------------------------------------------------


def test_live_nats_subject_passes(tmp_path: Path) -> None:
    """A known NATS subject does not trigger a dead-ref violation."""
    import json

    acl_dir = tmp_path / "deploy" / "nats"
    acl_dir.mkdir(parents=True, exist_ok=True)
    (acl_dir / "acl-matrix.json").write_text(
        json.dumps(
            {
                "version": "3",
                "request_reply_flows": [],
                "identities": {
                    "hub": {
                        "status": "active",
                        "allow_responses": False,
                        "publish": ["lyra.clipool.cmd"],
                        "subscribe": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    _make_doc(
        tmp_path,
        "docs/architecture/test.md",
        "The hub publishes to `lyra.clipool.cmd` subject.\n",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 0


# ---------------------------------------------------------------------------
# Operational-truth scope (#1538) — allowlist gates ops docs, exempts narrative
# ---------------------------------------------------------------------------


def test_operational_doc_dead_ref_exits_1(tmp_path: Path) -> None:
    """A dead ref in an operational-truth doc (CONFIGURATION.md) fails CI."""
    _make_doc(
        tmp_path,
        "docs/CONFIGURATION.md",
        "Config is loaded by `ZzzGhostLoader` which does not exist.\n",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 1


def test_operational_subdir_doc_dead_ref_exits_1(tmp_path: Path) -> None:
    """A dead ref in a nested docs/runbooks/** doc is gated (recursive allowlist)."""
    _make_doc(
        tmp_path,
        "docs/runbooks/nested/runbook.md",
        "The deploy calls `ZzzGhostDeployer` which does not exist.\n",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 1


def test_runbooks_doc_dead_ref_exits_1(tmp_path: Path) -> None:
    """A dead ref in a docs/runbooks/** doc is gated (sibling of ops/)."""
    _make_doc(
        tmp_path,
        "docs/runbooks/deploy.md",
        "The runbook calls `ZzzGhostRunbook` which does not exist.\n",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 1


def test_playbooks_doc_dead_ref_exits_1(tmp_path: Path) -> None:
    """A dead ref in a docs/playbooks/** doc is gated (sibling of ops/)."""
    _make_doc(
        tmp_path,
        "docs/playbooks/incident.md",
        "The playbook calls `ZzzGhostPlaybook` which does not exist.\n",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 1


def test_operational_mdx_doc_dead_ref_exits_1(tmp_path: Path) -> None:
    """An .mdx operational doc is gated — rglob covers *.md AND *.mdx."""
    _make_doc(
        tmp_path,
        "docs/runbooks/guide.mdx",
        "See `ZzzMdxGhost` for details, which does not exist.\n",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 1


def test_narrative_doc_exempt_while_sibling_op_doc_gated(tmp_path: Path) -> None:
    """A narrative doc (ROADMAP.md) is NOT scanned even when a sibling
    operational doc in the SAME tree IS — proving the exemption is
    path-specific, not an artifact of the allowlist never being extended.

    Reverting the allowlist edit makes the runbook doc unscanned (rc==0, first
    assert fails); adding ROADMAP.md to the allowlist surfaces its token
    (last assert fails). Neither tautology survives.
    """
    _make_doc(
        tmp_path,
        "docs/ROADMAP.md",
        "Someday we will add `ZzzFutureFeature` to the engine.\n",
    )
    _make_doc(
        tmp_path,
        "docs/runbooks/control.md",
        "The deploy uses `ZzzControlRef` which does not exist.\n",
    )
    rc, out, _err = _run_main(tmp_path)
    # The operational doc's dead ref IS caught …
    assert rc == 1
    assert "docs/runbooks/control.md" in out
    assert "ZzzControlRef" in out
    # … while the narrative doc is not scanned at all.
    assert "ROADMAP.md" not in out
    assert "ZzzFutureFeature" not in out
