#!/usr/bin/env python3
"""check_adr_archive_tables.py — ADR <-> domain-page "ADR archive" table coherence
prover (issue #2221, analysis Appendix B "SC2"). Manual prover — NOT wired into
scripts/qg / .dev/stack.yml; run by hand when auditing ADR/artifact coherence.

Ground truth:
  - An ADR's *activity* is location-based: docs/architecture/adr/NNN-*.mdx is
    active, docs/architecture/adr/archive/NNN-*.mdx is archived. (A handful of
    archive/ files carry no `status:` frontmatter key at all — only
    `superseded_by:` — so activity is never inferred from frontmatter text.)
  - An ADR's *owning* domain page is derived structurally from its own
    "> **Current truth** -> [...](...)" banner link, resolved relative to
    docs/architecture/adr/. This naturally excludes ADR-086 (owner is the root
    docs/ARCHITECTURE.md), ADR-093 (owner is docs/runbooks/operator-log.md) and
    ADR-101 (owner is brand/ + packages/shared/) from the "docs/architecture/*.md
    page" ownership checks below, since their targets resolve outside that glob
    — no hardcoded exemption list needed, though this comment documents it since
    the task spec calls them out by number.
  - Every "## ADR archive" heading (case-insensitive) in docs/architecture/*.md
    (excluding the generated docs/architecture/CURRENT.generated.md) introduces
    a markdown table whose first column lists one or more ADR numbers (bare
    "045", prefixed "ADR-084", or comma-separated "037, 040, 047, 062" in one
    cell) and whose "Status" column (located by header text, not a fixed index
    — job-model.md puts Status in column 2, everyone else puts it last) is
    freeform prose describing that ADR's fate.

Mismatch categories (all row checks are word-boundary, case-insensitive, so
"supersedes ADR-032" — active voice, describing what THIS row's ADR did to
another one — is never confused with "superseded [by ...]" describing this
row's own ADR):
  1. Row for an ARCHIVED ADR whose status text contains none of
     superseded/absorbed/archived — e.g. a stale bare "Accepted" left over
     from before the ADR was consolidated away.
  2. Row for an ACTIVE ADR whose own frontmatter status is "amended" but whose
     status text contains no amend/amended/amends/amendment.
  3. Row for an ACTIVE ADR whose status text contains "superseded" — the
     table is claiming an archival that the ADR file (still living under
     adr/, not adr/archive/) does not reflect.
  4. An ACTIVE ADR whose owning page has an "## ADR archive" table, but no row
     in it references that ADR's number.
  5. An ACTIVE ADR whose owning page has NO "## ADR archive" heading at all.
  (Unreachable-today but checked defensively: a table row referencing an ADR
  number with no matching file under adr/ or adr/archive/ at all.)

Output: one `MISMATCH <page>:<line> ADR-NNN <reason>` line per finding.
Exit 0 iff no output, exit 1 otherwise. (Manual prover, not a tools/ gate —
does not follow the tools/AGENTS.md "exit 0 = ran clean" contract.)

Run:
    python3 scripts/check_adr_archive_tables.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(out.stdout.strip())


ROOT = repo_root()
ADR_DIR = ROOT / "docs" / "architecture" / "adr"
ARCHIVE_DIR = ADR_DIR / "archive"
ARCH_DIR = ROOT / "docs" / "architecture"
EXCLUDE_PAGE = ARCH_DIR / "CURRENT.generated.md"

NUM_RE = re.compile(r"^(\d+)-")
STATUS_FM_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)
BANNER_RE = re.compile(r"current truth", re.IGNORECASE)
LINK_TARGET_RE = re.compile(r"\(([^)]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s*.*adr archive.*$", re.IGNORECASE)

ACK_ARCHIVED_RE = re.compile(r"\b(superseded|absorbed|archived)\b", re.IGNORECASE)
AMEND_RE = re.compile(r"\bamend\w*\b", re.IGNORECASE)
SUPERSEDED_RE = re.compile(r"\bsuperseded\b", re.IGNORECASE)

Mismatch = tuple[str, int, str]  # (page_rel, line, message)


@dataclass
class AdrEntry:
    number: int
    active: bool
    status: str = ""  # only meaningful when active
    owned_pages: set[str] = field(default_factory=set)  # repo-relative posix paths


def frontmatter_status(text: str) -> str:
    m = STATUS_FM_RE.search(text)
    if not m:
        return ""
    return m.group(1).strip().strip('"').strip("'")


def banner_link_targets(text: str) -> list[str]:
    for line in text.splitlines():
        if BANNER_RE.search(line):
            return LINK_TARGET_RE.findall(line)
    return []


def build_adr_index() -> dict[int, AdrEntry]:
    index: dict[int, AdrEntry] = {}

    # Archived first (location-only; active — processed next — wins on the
    # known 068 collision, matching the real-world intent: storage.md's "068"
    # row is unambiguously the active Ecosystem Service Plane ADR, never the
    # archived SELinux z-label one).
    for f in sorted(ARCHIVE_DIR.glob("*.mdx")):
        m = NUM_RE.match(f.name)
        if not m:
            continue
        n = int(m.group(1))
        index[n] = AdrEntry(number=n, active=False)

    for f in sorted(ADR_DIR.glob("*.mdx")):
        m = NUM_RE.match(f.name)
        if not m:
            continue
        n = int(m.group(1))
        text = f.read_text(encoding="utf-8")
        status = frontmatter_status(text).lower()
        owned: set[str] = set()
        for target in banner_link_targets(text):
            resolved = (ADR_DIR / target).resolve()
            try:
                rel = resolved.relative_to(ROOT).as_posix()
            except ValueError:
                continue
            if (
                resolved.parent == ARCH_DIR
                and resolved != EXCLUDE_PAGE
                and resolved.suffix == ".md"
            ):
                owned.add(rel)
        index[n] = AdrEntry(number=n, active=True, status=status, owned_pages=owned)

    return index


@dataclass
class TableRow:
    line: int
    numbers: list[int]
    status_text: str


def _split_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table_columns(header_cells: list[str]) -> tuple[int, int]:
    """Locate the ADR-number and Status columns by header text (job-model.md
    puts Status in column 2, everyone else puts it last — never assume a
    fixed index)."""
    adr_col = 0
    for idx, cell in enumerate(header_cells):
        if cell.lower() == "adr":
            adr_col = idx
            break
    status_col = len(header_cells) - 1
    for idx, cell in enumerate(header_cells):
        if "status" in cell.lower():
            status_col = idx
            break
    return adr_col, status_col


def _parse_row(
    line: str, lineno: int, adr_col: int, status_col: int
) -> TableRow | None:
    cells = _split_cells(line)
    if all(re.match(r"^[\s:-]*$", c) for c in cells):
        return None
    adr_cell = cells[adr_col] if adr_col < len(cells) else ""
    status_cell = cells[status_col] if status_col < len(cells) else ""
    numbers = [int(x) for x in re.findall(r"\d+", adr_cell)]
    if not numbers:
        return None
    return TableRow(line=lineno, numbers=numbers, status_text=status_cell)


def find_table_rows(lines: list[str], heading_idx: int) -> list[TableRow]:
    """Parse the markdown table immediately following the heading at
    lines[heading_idx]. Returns [] if no table (a bare "|"-less body) follows.
    """
    i = heading_idx + 1
    n = len(lines)
    while i < n and lines[i].strip() == "":
        i += 1
    if i >= n or not lines[i].lstrip().startswith("|"):
        return []
    adr_col, status_col = _table_columns(_split_cells(lines[i]))
    i += 1
    if i < n and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i]):
        i += 1  # separator row

    rows: list[TableRow] = []
    while i < n and lines[i].lstrip().startswith("|"):
        row = _parse_row(lines[i], i + 1, adr_col, status_col)
        if row is not None:
            rows.append(row)
        i += 1
    return rows


def _row_mismatches(
    n: int, row: TableRow, entry: AdrEntry | None, page_rel: str
) -> list[Mismatch]:
    if entry is None:
        return [
            (
                page_rel,
                row.line,
                f"ADR-{n:03d} referenced in table but no matching "
                f"ADR file found under docs/architecture/adr/",
            )
        ]

    if not entry.active:
        if ACK_ARCHIVED_RE.search(row.status_text):
            return []
        return [
            (
                page_rel,
                row.line,
                f"ADR-{n:03d} is archived but row status "
                f"'{row.status_text}' does not acknowledge it "
                f"(expected superseded/absorbed/archived wording)",
            )
        ]

    found: list[Mismatch] = []
    if entry.status.startswith("amended") and not AMEND_RE.search(row.status_text):
        found.append(
            (
                page_rel,
                row.line,
                f"ADR-{n:03d} frontmatter status=amended but row "
                f"status '{row.status_text}' omits amendment",
            )
        )
    if SUPERSEDED_RE.search(row.status_text):
        found.append(
            (
                page_rel,
                row.line,
                f"ADR-{n:03d} row status '{row.status_text}' claims "
                f"superseded but ADR-{n:03d} is active "
                f"(frontmatter status={entry.status or 'unknown'})",
            )
        )
    return found


def _check_page(page: Path, index: dict[int, AdrEntry]) -> list[Mismatch]:
    page_rel = page.relative_to(ROOT).as_posix()
    lines = page.read_text(encoding="utf-8").splitlines()

    heading_idx = next(
        (idx for idx, line in enumerate(lines) if HEADING_RE.match(line.strip())),
        None,
    )
    owned_here = {n for n, e in index.items() if e.active and page_rel in e.owned_pages}

    if heading_idx is None:
        return [
            (
                page_rel,
                1,
                f"ADR-{n:03d} page has no '## ADR archive' table "
                f"(ADR-{n:03d}'s Current-truth banner points here)",
            )
            for n in sorted(owned_here)
        ]

    mismatches: list[Mismatch] = []
    seen: set[int] = set()
    for row in find_table_rows(lines, heading_idx):
        for n in row.numbers:
            seen.add(n)
            mismatches.extend(_row_mismatches(n, row, index.get(n), page_rel))

    for n in sorted(owned_here - seen):
        mismatches.append(
            (
                page_rel,
                heading_idx + 1,
                f"ADR-{n:03d} is active and owns this page (Current-truth "
                f"banner) but has no row in its '## ADR archive' table",
            )
        )
    return mismatches


def main() -> int:
    # Path.glob() on a missing directory silently yields nothing — a renamed
    # docs/architecture/ tree would index zero ADRs and exit 0 (false-clean).
    # A prover's exit 0 must mean "actually checked": fail loudly instead.
    for d in (ADR_DIR, ARCHIVE_DIR, ARCH_DIR):
        if not d.is_dir():
            raise SystemExit(f"ERROR: expected directory missing: {d}")
    index = build_adr_index()
    if not index:
        raise SystemExit(f"ERROR: no ADR files found under {ADR_DIR}")
    mismatches: list[Mismatch] = []
    for page in sorted(p for p in ARCH_DIR.glob("*.md") if p != EXCLUDE_PAGE):
        mismatches.extend(_check_page(page, index))

    if mismatches:
        mismatches.sort(key=lambda m: (m[0], m[1]))
        for page_rel, line, msg in mismatches:
            print(f"MISMATCH {page_rel}:{line} {msg}")
        return 1

    print("check_adr_archive_tables: no mismatches found — OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
