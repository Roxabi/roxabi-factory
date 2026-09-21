#!/usr/bin/env python3
"""Scan docs and AGENTS.md files for dead backtick references.

Checks src/... paths, module paths, NATS subjects, and CamelCase symbols
against the codebase using a semantic AST-based oracle (CodeInventory).

Exit 0 = clean. Exit 1 = new dead references found. Exit 2 = script error.

Usage:
    python tools/check_doc_drift.py [--root ROOT] [--baseline PATH] [--update-baseline]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Import shim — supports both script invocation (python tools/check_doc_drift.py)
# and import from project root (from tools.check_doc_drift import main).
# ---------------------------------------------------------------------------

try:
    from tools.code_inventory import CodeInventory
except ImportError:
    # Running as a standalone script: tools/ is not a proper package on sys.path.
    # Insert the directory containing this file so code_inventory.py is importable.
    _tools_dir = Path(__file__).resolve().parent
    if str(_tools_dir) not in sys.path:
        sys.path.insert(0, str(_tools_dir))
    from code_inventory import CodeInventory  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Patterns & constants
# ---------------------------------------------------------------------------

_BACKTICK_RE = re.compile(r"`([^`\n]+)`")

_HISTORICAL_RE = re.compile(
    r"deleted|removed|superseded|renamed|formerly|no longer"
    r"|legacy|historical|#\d+ deleted",
    re.IGNORECASE,
)

# Regex to split a line into whitespace-delimited clauses, used to scope the
# historical-annotation check per-token.  A clause is a run of characters
# between whitespace/punctuation that does not contain backtick boundaries.
# Rule (C): historical exemption is PER-TOKEN, not per-line.
# We scope the exemption to backtick-enclosed tokens that are *adjacent to*
# a historical keyword on the same line.  "Adjacent" means the token and the
# keyword appear in the same comma-delimited clause OR are within 60 chars of
# each other on the line.  This allows:
#   "The `OldFoo` driver was deleted; `LiveBar` remains." → OldFoo exempt,
#   LiveBar still checked.
_ADJACENT_THRESHOLD = 60  # characters between token start and keyword match


def _default_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_baseline(root: Path) -> Path:
    return root / "tools" / "doc_drift_baseline.txt"


# ---------------------------------------------------------------------------
# Scan targets
# ---------------------------------------------------------------------------


def _collect_agents_mds(root: Path, seen: set[Path], out: list[Path]) -> None:
    """Append all non-hidden AGENTS.md files under root's search dirs."""

    def add(p: Path) -> None:
        if p not in seen and p.is_file():
            seen.add(p)
            out.append(p)

    for sr in (root, root / "src", root / "packages", root / "plugins"):
        if not sr.is_dir():
            continue
        for cm in sorted(sr.rglob("AGENTS.md")):
            if any(p.startswith(".") for p in cm.relative_to(root).parts):
                continue
            add(cm)


def _collect_operational_docs(root: Path, seen: set[Path], out: list[Path]) -> None:
    """Append operational-truth docs (#1538) — docs that describe the REAL
    running system and cite live symbols/paths, so they are drift-gated.

    Narrative/aspirational docs (COMMANDS, OBSERVABILITY, docs/history/**) are
    EXEMPT by omission — they cite illustrative or future code by design, so
    gating them would produce false positives.

    QUICKSTART/GETTING-STARTED/MULTI-BOT (container-only onboarding, #2201) and
    debt-tracking.md (#2200) ARE scanned — their prior exemption is exactly what
    masked the dead pre-#1057 credential flow and debt-tracking doc-rot, so they
    are now first-class scan targets.
    """

    def add(p: Path) -> None:
        if p not in seen and p.is_file():
            seen.add(p)
            out.append(p)

    docs = root / "docs"
    for rel in (
        "CONFIGURATION.md",
        "DEPLOYMENT.md",
        "QUICKSTART.md",
        "GETTING-STARTED.md",
        "MULTI-BOT.md",
        "agent-management.md",
        "bot-management.md",
        "data-dirs.md",
        "debt-tracking.md",
    ):
        add(docs / rel)
    for sub in ("runbooks", "playbooks"):
        d = docs / sub
        if not d.is_dir():
            continue
        for ext in ("*.md", "*.mdx"):
            for f in sorted(d.rglob(ext)):
                add(f)


def _collect_scan_files(root: Path) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []

    def add(p: Path) -> None:
        if p not in seen and p.is_file():
            seen.add(p)
            out.append(p)

    arch = root / "docs" / "architecture"
    if arch.is_dir():
        # ADR archive = immutable decision records; they cite symbols as-of-writing
        # and are NOT drift-gated (per ADR-080: ADRs carry historical INTENT).
        adr_dir = arch / "adr"
        for ext in ("*.md", "*.mdx"):
            for f in sorted(arch.rglob(ext)):
                if adr_dir in f.parents:
                    continue
                if f.name == "CURRENT.generated.md":
                    continue
                add(f)
    add(root / "docs" / "ARCHITECTURE.md")
    standards = root / "docs" / "standards"
    if standards.is_dir():
        for f in sorted(standards.rglob("*.md")):
            add(f)
    _collect_operational_docs(root, seen, out)
    _collect_agents_mds(root, seen, out)
    return out


# ---------------------------------------------------------------------------
# Per-token historical scope check (fix C)
# ---------------------------------------------------------------------------


def _token_is_historical(token_start: int, line: str) -> bool:
    """Return True if the token at *token_start* is adjacent to a historical keyword.

    "Adjacent" = the historical keyword match is within _ADJACENT_THRESHOLD
    characters of the token's start position.  This prevents a historical
    annotation for one dead symbol from exempting a *different* live-but-dead
    reference on the same line.

    Rule (fix C): exemption is scoped per-token, not per-line.  A historical
    keyword far away (>60 chars) does NOT exempt the token.  In practice both
    tokens in a short sentence share the exemption — the threshold covers
    same-clause co-location.

    Lines are scanned separately, so a historical keyword on line N never
    exempts a token on line N+1.
    """
    for m in _HISTORICAL_RE.finditer(line):
        if abs(m.start() - token_start) <= _ADJACENT_THRESHOLD:
            return True
    return False


def _resolve_package_relative_path(root: Path, doc_path: Path, token: str) -> bool:
    """Return True if *token* is a package-relative path that exists.

    When scanning ``packages/<pkg>/AGENTS.md``, ``src/...`` tokens should be
    resolved relative to ``packages/<pkg>/`` first, then repo root.  This
    prevents false positives where a package AGENTS.md references a path
    inside its own ``src/`` tree and the gate incorrectly tries repo-root
    ``src/`` first.
    """
    if not token.startswith("src/"):
        return False
    try:
        rel = doc_path.relative_to(root / "packages")
    except ValueError:
        return False
    parts = rel.parts
    if len(parts) < 1:
        return False
    pkg_dir = parts[0]
    candidate = root / "packages" / pkg_dir / token
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
        if resolved.exists():
            return True
    except (ValueError, OSError):
        pass
    return False


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def _load_baseline(path: Path) -> frozenset[str]:
    if not path.exists():
        return frozenset()
    return frozenset(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def _baseline_key(relpath: str, token: str) -> str:
    return f"{relpath}::{token}"


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

Violation = tuple[str, int, str, str]  # (relpath, lineno, token, reason)


def _scan_file(
    root: Path,
    path: Path,
    baseline: frozenset[str],
    oracle: CodeInventory,
) -> tuple[list[Violation], list[Violation]]:
    new_v: list[Violation] = []
    base_v: list[Violation] = []
    relpath = path.relative_to(root).as_posix()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], []

    for lineno, line in enumerate(lines, 1):
        if "<!-- drift-ignore -->" in line:
            continue
        # Find all backtick tokens and their positions on the line
        token_matches = list(_BACKTICK_RE.finditer(line))
        if not token_matches:
            continue

        for m in token_matches:
            token = m.group(1)
            token_start = m.start()

            # Package-relative path short-circuit for packages/*/AGENTS.md
            if _resolve_package_relative_path(root, path, token):
                continue

            verdict = oracle.resolve(token)
            # kind=unknown → external / unclassifiable → skip (no false positive)
            if verdict.kind == "unknown":
                continue
            # Token exists → not a dead ref
            if verdict.exists:
                continue

            # Dead reference candidate — apply historical adjacency check (fix C)
            if _token_is_historical(token_start, line):
                continue

            reason = f"not found in src/ ({verdict.kind})"
            key = _baseline_key(relpath, token)
            (base_v if key in baseline else new_v).append(
                (relpath, lineno, token, reason)
            )
    return new_v, base_v


def scan(
    root: Path,
    baseline: frozenset[str],
    oracle: CodeInventory,
) -> tuple[list[Violation], list[Violation]]:
    all_new: list[Violation] = []
    all_base: list[Violation] = []
    for f in _collect_scan_files(root):
        n, b = _scan_file(root, f, baseline, oracle)
        all_new.extend(n)
        all_base.extend(b)
    return all_new, all_base


# ---------------------------------------------------------------------------
# Baseline update
# ---------------------------------------------------------------------------


def _write_baseline(path: Path, violations: list[Violation]) -> None:
    keys = sorted({_baseline_key(r, t) for r, _, t, _ in violations})
    hdr = (
        "# doc_drift_baseline.txt — burn-down list of known doc-rot\n"
        "# Tracked in epic #1530. Pre-existing violations to purge over time.\n"
        "# Format: relpath::token  (sorted, one per line)\n"
        "# DO NOT add new entries — fix the doc or add a historical annotation.\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(hdr + "\n".join(keys) + ("\n" if keys else ""), encoding="utf-8")
    print(f"Baseline written: {len(keys)} entries → {path}")


# ---------------------------------------------------------------------------
# CLI (fix D: exit 2 on unexpected internal error)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the doc-drift gate.

    Exit codes:
      0 — clean (no unbaselined violations)
      1 — unbaselined violations found
      2 — script error (unexpected exception or syntax errors in scanned sources)
    """
    parser = argparse.ArgumentParser(
        description=(
            "Scan docs and AGENTS.md files for dead backtick references. "
            "Exit 0 = clean. Exit 1 = new violations. Exit 2 = script error."
        )
    )
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        default=False,
        help="Rewrite baseline from all current violations, then exit 0.",
    )
    args = parser.parse_args(argv)

    try:
        root: Path = (args.root or _default_root()).resolve()
        baseline_path: Path = args.baseline or _default_baseline(root)
        baseline = _load_baseline(baseline_path)

        # Build the semantic oracle
        oracle = CodeInventory.build(root)

        # Surface syntax errors as a fatal condition (exit 2)
        if oracle.syntax_errors:
            for path, msg in oracle.syntax_errors:
                print(
                    f"ERROR: SyntaxError in {path}: {msg}",
                    file=sys.stderr,
                )
            print(
                f"check_doc_drift: {len(oracle.syntax_errors)} source file(s) "
                "failed to parse — cannot run gate reliably.",
                file=sys.stderr,
            )
            return 2

        new_violations, baselined = scan(root, baseline, oracle)

        if args.update_baseline:
            all_violations = new_violations + baselined
            # Fix E: warn when rewrite absorbs previously-unbaselined violations
            # (ratchet-growth guard — new violations would be silently baselined)
            if new_violations:
                print(
                    f"WARNING: --update-baseline is absorbing {len(new_violations)} "
                    "new (previously-unbaselined) violation(s). "
                    "Review before committing: these were not in the prior baseline.",
                    file=sys.stderr,
                )
            _write_baseline(baseline_path, all_violations)
            return 0

        if new_violations:
            for relpath, lineno, token, reason in sorted(new_violations):
                print(f"{relpath}:{lineno} → `{token}` → {reason}")
            n = len(new_violations)
            b = len(baselined)
            print(f"\n{n} dead reference(s) found ({b} baselined).", file=sys.stderr)
            print(
                "Suppress with: historical annotation (deleted/removed/...) "
                "or <!-- drift-ignore -->. Fix the doc to clear the violation.",
                file=sys.stderr,
            )
            return 1

        b = len(baselined)
        print(f"doc-drift: OK — 0 new violations ({b} in burn-down baseline).")
        return 0

    except Exception as exc:  # noqa: BLE001
        print(
            f"check_doc_drift: unexpected error: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
