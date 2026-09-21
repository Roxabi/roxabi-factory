#!/usr/bin/env python3
"""Semantic oracle for codebase symbol/module/subject resolution.

Provides CodeInventory.build() which performs a single AST-based pass over
src/**/*.py, packages/*/src/**/*.py, and tools/**/*.py (names only) to collect:
  - modules: set of dotted module names (factory.core.hub, roxabi_nats.connect, …)
  - symbols: dict mapping bare name → set of defining module paths
  - subjects: frozenset of NATS subject literals and wildcard patterns

Used by check_doc_drift.py to replace textual regex guessing with semantic
resolution.  Stdlib-only: ast, builtins, json, pathlib, re.

Exit-code contract for build() callers: if syntax_errors is non-empty, the
caller should exit 2 (script broke — scanned source has parse errors).
"""

from __future__ import annotations

import ast
import builtins
import json
import re
import sys
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

Kind = Literal["module", "symbol", "subject", "path", "unknown"]


class Verdict:
    """Resolution result for a single token."""

    __slots__ = ("exists", "kind")

    def __init__(self, exists: bool, kind: Kind) -> None:
        self.exists = exists
        self.kind = kind

    def __repr__(self) -> str:
        return f"Verdict(exists={self.exists!r}, kind={self.kind!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Verdict):
            return NotImplemented
        return self.exists == other.exists and self.kind == other.kind


# ---------------------------------------------------------------------------
# Project namespace configuration
# ---------------------------------------------------------------------------


def _compute_project_prefixes() -> frozenset[str]:
    """Derive project namespace prefixes from packages/ layout at import time.

    Always includes "factory" (the main package).  Adds one prefix per
    packages/<pkg>/src/ directory, converting hyphens to underscores so the
    dotted module name matches the filesystem layout.  A new package added to
    packages/ is then automatically covered without editing this file.
    """
    prefixes: set[str] = {"factory"}
    packages_dir = Path(__file__).resolve().parent.parent / "packages"
    if packages_dir.is_dir():
        for pkg_dir in packages_dir.iterdir():
            if (pkg_dir / "src").is_dir():
                prefixes.add(pkg_dir.name.replace("-", "_"))
    return frozenset(prefixes)


# Dotted-name prefixes owned by this project.  Other dotted tokens are
# treated as external → kind=unknown (no false positive).
# Derived at import time from packages/ layout; a new package is auto-covered.
_PROJECT_PREFIXES: frozenset[str] = _compute_project_prefixes()

# Well-known external / generic PascalCase names that appear in project docs
# but are NOT project-defined classes.  The oracle skips these (kind=unknown)
# to avoid false positives.  Only add entries confirmed as external-only.
_EXTERNAL_KNOWN_NAMES: frozenset[str] = frozenset(
    [
        # stdlib exceptions and warnings
        "KeyError",
        "ValueError",
        "TypeError",
        "RuntimeError",
        "OSError",
        "AttributeError",
        "NotImplementedError",
        "StopAsyncIteration",
        "DeprecationWarning",
        "UserWarning",
        "StopIteration",
        "Exception",
        "BaseException",
        "ImportError",
        "FileNotFoundError",
        "PermissionError",
        "TimeoutError",
        "ConnectionError",
        "OverflowError",
        "IndexError",
        "NameError",
        "UnicodeDecodeError",
        "UnicodeEncodeError",
        # typing / generic terms
        "PascalCase",
        "CamelCase",
        "TypeVar",
        "Protocol",
        "Optional",
        "Union",
        "Dict",
        "List",
        "Tuple",
        "Set",
        "Any",
        "Callable",
        "Iterator",
        "AsyncIterator",
        "Generator",
        "AsyncGenerator",
        # framework / external library names
        "GitHub",
        "Discord",
        "Telegram",
        "FastAPI",
        "Pydantic",
        "Python",
        "MagicMock",
        "AsyncMock",
        # nats-py external exceptions
        "NoRespondersError",
        "BucketNotFoundError",
        # anthropic / LLM library types
        "InputJsonDelta",
        # HTTP/ASGI transports
        "ASGITransport",
        "HTTPTransport",
        # omp-rpc external class — defined in the omp_rpc package, not in this
        # repo. Code uses it only as omp_rpc.RpcClient (attribute access, which
        # the AST pass does not index). Confirmed: no class RpcClient under src/.
        "RpcClient",
        # generic doc terms that are not project classes
        "RunError",
    ]
)

# ---------------------------------------------------------------------------
# NATS wildcard matching
# ---------------------------------------------------------------------------


def _nats_matches(pattern: str, subject: str) -> bool:
    """Return True if *subject* matches NATS wildcard *pattern*.

    NATS wildcards:
      *  — matches exactly one token (no dots allowed in that token)
      >  — matches one or more tokens at the end; must be the last token

    A pattern with `>` in a non-terminal position is malformed; return False.
    """
    p_tokens = pattern.split(".")
    # Guard: `>` must be the last token if present anywhere in the pattern
    for i, pt in enumerate(p_tokens):
        if pt == ">" and i != len(p_tokens) - 1:
            return False
    s_tokens = subject.split(".")
    pi = 0
    si = 0
    while pi < len(p_tokens) and si < len(s_tokens):
        pt = p_tokens[pi]
        if pt == ">":
            # > must be last and matches all remaining subject tokens (≥1)
            return si < len(s_tokens)
        if pt == "*":
            pi += 1
            si += 1
        elif pt == s_tokens[si]:
            pi += 1
            si += 1
        else:
            return False
    return pi == len(p_tokens) and si == len(s_tokens)


def _subject_matches_any(token: str, subjects: frozenset[str]) -> bool:
    """Return True if *token* is in *subjects* or matches a wildcard pattern."""
    if token in subjects:
        return True
    for pat in subjects:
        if ("*" in pat or ">" in pat) and _nats_matches(pat, token):
            return True
    return False


# ---------------------------------------------------------------------------
# Module-name derivation
# ---------------------------------------------------------------------------


def _module_name_from_path(py_file: Path, src_root: Path) -> str | None:
    """Derive dotted module name from *py_file* relative to *src_root*.

    Returns None if the file is not under src_root or is not a .py file.
    """
    try:
        rel = py_file.relative_to(src_root)
    except ValueError:
        return None
    parts = list(rel.parts)
    if not parts:
        return None
    last = parts[-1]
    if not last.endswith(".py"):
        return None
    parts[-1] = last[:-3]  # strip .py
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


# ---------------------------------------------------------------------------
# AST collection helpers
# ---------------------------------------------------------------------------


def _collect_top_level_names(tree: ast.Module) -> set[str]:
    """Return names defined at module top level (classes, functions, assignments)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _collect_import_names(tree: ast.Module) -> set[str]:
    """Return names introduced by import statements at module level."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                eff = alias.asname if alias.asname else alias.name.split(".")[-1]
                names.add(eff)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                eff = alias.asname if alias.asname else alias.name
                names.add(eff)
    return names


# ---------------------------------------------------------------------------
# Template-token detection
# ---------------------------------------------------------------------------


def _is_template_token(token: str) -> bool:
    """Return True if *token* is a documentation template/pattern, not a ref.

    Templates are skipped (kind=unknown) to avoid false positives.
    See _is_template_by_char and _is_template_by_shape for the two groups.
    """
    return _is_template_by_char(token) or _is_template_by_shape(token)


def _is_template_by_char(token: str) -> bool:
    """Return True if token contains documentation-notation characters."""
    # <...> / {...} → placeholder; | → alternation; () → call; :: → annotation
    return (
        "<" in token
        or "{" in token
        or "|" in token
        or "(" in token
        or ")" in token
        or "::" in token
    )


def _is_template_by_shape(token: str) -> bool:
    """Return True if token has a glob/wildcard shape that marks it as a pattern."""
    # Path tokens with shell glob chars or line-range annotation
    if token.startswith(("src/", "packages/")):
        if "*" in token or "?" in token:
            return True
        if re.search(r":\d", token):
            return True
        return False
    # Non-path tokens with slash (lyra.outbound/, factory.adapters/__init__.py)
    if "/" in token:
        return True
    # NATS namespace doc-patterns: lyra.* or lyra.something.*
    if token == "lyra.*" or re.match(r"^lyra\.[a-z_.]+\.\*$", token):
        return True
    # Any non-path token containing * (lyra.nats.*_client)
    if "*" in token:
        return True
    return False


# ---------------------------------------------------------------------------
# Subject extraction
# ---------------------------------------------------------------------------


def _looks_like_nats_subject(val: str) -> bool:
    """Heuristic: does *val* look like a NATS subject literal?"""
    if not val or " " in val or "\n" in val or len(val) > 200:
        return False
    lower = val.lower()
    return (
        lower.startswith("lyra.")
        or lower.startswith("factory.")
        or lower.startswith("$js.")
        or lower.startswith("$kv.")
        or lower.startswith("_inbox.")
    )


def _extract_subjects_from_contracts(contracts_src: Path) -> set[str]:
    """Walk roxabi-contracts src AST and collect NATS subject string constants."""
    subjects: set[str] = set()
    for py_file in contracts_src.rglob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _looks_like_nats_subject(node.value):
                    subjects.add(node.value)
    return subjects


def _extract_subjects_from_acl(acl_path: Path) -> set[str]:
    """Parse acl-matrix.json and return all publish/subscribe subject literals."""
    subjects: set[str] = set()
    if not acl_path.exists():
        return subjects
    try:
        data = json.loads(acl_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return subjects

    for flow in data.get("request_reply_flows", []):
        subj = flow.get("subject", "")
        if subj and isinstance(subj, str):
            subjects.add(subj)

    for identity in data.get("identities", {}).values():
        for entry in identity.get("publish", []):
            if isinstance(entry, str):
                subjects.add(entry)
        for entry in identity.get("subscribe", []):
            if isinstance(entry, str):
                subjects.add(entry)

    return subjects


# ---------------------------------------------------------------------------
# Build helpers (extracted to keep build() below complexity threshold)
# ---------------------------------------------------------------------------


def _discover_src_roots(root: Path) -> list[Path]:
    """Return all src roots to scan: root/src plus packages/*/src."""
    roots: list[Path] = []
    main_src = root / "src"
    if main_src.is_dir():
        roots.append(main_src)
    pkg_root = root / "packages"
    if pkg_root.is_dir():
        for pkg_dir in sorted(pkg_root.iterdir()):
            pkg_src = pkg_dir / "src"
            if pkg_src.is_dir():
                roots.append(pkg_src)
    return roots


def _ast_pass(
    src_roots: list[Path],
) -> tuple[set[str], dict[str, set[str]], list[tuple[Path, str]]]:
    """Walk src_roots with AST, collecting modules, symbols, syntax errors."""
    modules: set[str] = set()
    symbols: dict[str, set[str]] = {}
    syntax_errors: list[tuple[Path, str]] = []

    for src_root in src_roots:
        for py_file in sorted(src_root.rglob("*.py")):
            mod_name = _module_name_from_path(py_file, src_root)
            if mod_name is None:
                continue
            modules.add(mod_name)
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError as exc:
                syntax_errors.append((py_file, str(exc)))
                continue
            top_names = _collect_top_level_names(tree)
            import_names = _collect_import_names(tree)
            for name in top_names | import_names:
                if not name:
                    continue
                if name not in symbols:
                    symbols[name] = set()
                symbols[name].add(mod_name)

    return modules, symbols, syntax_errors


def _collect_subjects(root: Path) -> frozenset[str]:
    """Collect all NATS subjects from ACL matrix and contracts.

    Emits a WARNING to stderr when no subjects are found, which typically
    indicates a missing or malformed acl-matrix.json — subject refs will be
    misclassified as dead modules instead of dead subjects in that case.
    """
    all_subjects: set[str] = set()
    acl_path = root / "deploy" / "nats" / "acl-matrix.json"
    all_subjects |= _extract_subjects_from_acl(acl_path)
    pkg_root = root / "packages"
    if pkg_root.is_dir():
        contracts_src = pkg_root / "roxabi-contracts" / "src"
        if contracts_src.is_dir():
            all_subjects |= _extract_subjects_from_contracts(contracts_src)
    if not all_subjects:
        print(
            "WARNING: no NATS subjects loaded from acl-matrix.json/contracts"
            " — subject refs may be misclassified",
            file=sys.stderr,
        )
    return frozenset(all_subjects)

def _index_tool_symbols(
    root: Path,
    symbols: dict[str, set[str]],
    syntax_errors: list[tuple[Path, str]],
) -> None:
    """Record top-level names defined in tools/**/*.py.

    Gate docs (tools/AGENTS.md) cite types that live in the gate scripts, not
    in src/. Those names are real symbols; omitting tools/ made them look dead
    (``Rule`` in check_doc_semantic_drift.py).

    Indexed under ``tools.<stem>`` and NOT added to the module set: a tools
    filename must not become a project module prefix, or an unrelated
    ``<stem>.PascalCase`` doc token would resolve as a dead symbol.
    """
    tools_dir = root / "tools"
    if not tools_dir.is_dir():
        return
    for py_file in sorted(tools_dir.rglob("*.py")):
        rel = py_file.relative_to(tools_dir)
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        mod_name = "tools." + rel.with_suffix("").as_posix().replace("/", ".")
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError as exc:
            syntax_errors.append((py_file, str(exc)))
            continue
        for name in _collect_top_level_names(tree):
            if not name:
                continue
            symbols.setdefault(name, set()).add(mod_name)


# ---------------------------------------------------------------------------
# CodeInventory
# ---------------------------------------------------------------------------


class CodeInventory:
    """Semantic oracle: modules, symbols, NATS subjects from a codebase scan.

    Build once with CodeInventory.build(root), then call resolve(token) for
    each backtick token found in documentation.
    """

    def __init__(  # noqa: PLR0913
        self,
        root: Path,
        modules: set[str],
        symbols: dict[str, set[str]],
        subjects: frozenset[str],
        builtin_names: frozenset[str],
        syntax_errors: list[tuple[Path, str]],
    ) -> None:
        self._root = root
        self.modules = modules
        self.symbols = symbols
        self.subjects = subjects
        self._builtins = builtin_names
        self.syntax_errors = syntax_errors

    # ------------------------------------------------------------------ build



    @classmethod
    def build(cls, root: Path) -> "CodeInventory":
        """Build a CodeInventory in a single pass over root.

        Scans:
          - src/**/*.py  (if src/ exists)
          - packages/*/src/**/*.py  (for each package under packages/)
          - tools/**/*.py  (top-level names only; not registered as modules)
          - deploy/nats/acl-matrix.json  (NATS subject literals)
          - packages/roxabi-contracts/src/**/*.py  (NATS subject constants)

        Files with SyntaxError are recorded in .syntax_errors; the caller
        should exit 2 if syntax_errors is non-empty.
        """
        src_roots = _discover_src_roots(root)
        modules, symbols, syntax_errors = _ast_pass(src_roots)
        _index_tool_symbols(root, symbols, syntax_errors)
        subjects = _collect_subjects(root)
        return cls(
            root=root,
            modules=modules,
            symbols=symbols,
            subjects=subjects,
            builtin_names=frozenset(dir(builtins)),
            syntax_errors=syntax_errors,
        )

    # ------------------------------------------------------------------ to_dict

    def to_dict(self) -> dict[str, object]:
        """Return a stable, JSON-serializable snapshot of the inventory.

        The dict has three keys:
          - "modules":  sorted list of all dotted module names
          - "symbols":  dict mapping bare name → sorted list of defining modules
          - "subjects": sorted list of all NATS subject literals/patterns

        Deterministic (sorted everywhere) so that two builds over the same
        codebase yield identical dicts.  Intended as the shared serialization
        contract consumed by #1532 and any downstream tool that needs an
        inventory without re-scanning.
        """
        return {
            "modules": sorted(self.modules),
            "symbols": {k: sorted(v) for k, v in sorted(self.symbols.items())},
            "subjects": sorted(self.subjects),
        }

    # ------------------------------------------------------------------ resolve

    def resolve(self, token: str) -> Verdict:
        """Resolve a single backtick token to a Verdict.

        Resolution order:
          0. Pathologically long token (>200 chars) → kind=unknown (O(N²) guard)
          1. Template/glob tokens → kind=unknown (no false positive)
          2. Starts with src/ or packages/ → path resolution
          3. Contains a dot → module/qualified-symbol/subject resolution
          4. Bare word → builtin / project symbol / PascalCase dead ref
          5. Genuinely external → kind=unknown
        """
        t = token.strip()
        if not t:
            return Verdict(exists=False, kind="unknown")

        # 0. Length guard — prevents O(N²) on pathological tokens
        if len(t) > 200:
            return Verdict(exists=False, kind="unknown")

        # 1. Template/glob tokens (documentation patterns, not real refs)
        if _is_template_token(t):
            return Verdict(exists=False, kind="unknown")

        # 2. Path tokens
        if t.startswith(("src/", "packages/")):
            return self._resolve_path(t)

        # 3. Dotted token: module, qualified symbol, or NATS subject
        if "." in t:
            return self._resolve_dotted_with_subjects(t)

        # 4. Bare NATS subject (exact match only, no dots)
        if self._looks_like_subject_token(t):
            exists = _subject_matches_any(t, self.subjects)
            return Verdict(exists=exists, kind="subject")

        # 5. Bare word
        return self._resolve_bare(t)

    # ------------------------------------------------------------------ internals

    def _resolve_path(self, token: str) -> Verdict:
        """Resolve src/... or packages/... path token.

        Guards against directory traversal (rejects tokens containing ..).
        Also tries package-relative: a token like src/roxabi_nats/__init__.py
        may live at packages/roxabi-nats/src/roxabi_nats/__init__.py.
        """
        if ".." in token.split("/"):
            return Verdict(exists=False, kind="path")

        # Root-relative
        candidate = self._root / token
        try:
            resolved = candidate.resolve()
            resolved.relative_to(self._root.resolve())
            if resolved.exists():
                return Verdict(exists=True, kind="path")
        except (ValueError, OSError):
            pass

        # Package-relative: try packages/<pkg>/<token> for each package
        if token.startswith("src/"):
            pkg_root = self._root / "packages"
            if pkg_root.is_dir():
                for pkg_dir in pkg_root.iterdir():
                    candidate = pkg_dir / token
                    try:
                        resolved = candidate.resolve()
                        resolved.relative_to(self._root.resolve())
                        if resolved.exists():
                            return Verdict(exists=True, kind="path")
                    except (ValueError, OSError):
                        continue

        return Verdict(exists=False, kind="path")

    def _looks_like_subject_token(self, token: str) -> bool:
        """Return True if token looks like a NATS subject (no dots, project-ns)."""
        lower = token.lower()
        if any(
            lower.startswith(p)
            for p in ("lyra.", "factory.", "$js.", "$kv.", "_inbox.")
        ):
            return True
        return token in self.subjects

    def _resolve_symbol_at_split(self, token: str, parts: list[str]) -> Verdict | None:
        """Try each split point for a module.Symbol pattern.

        Returns a Verdict when confident, or None to fall through to subject check.

        SCOPED CHECK: `suffix in self.symbols` is NOT sufficient — `symbols` maps
        name → set(defining modules).  A name imported/defined elsewhere must not
        cause `module.name` to resolve live when `module` doesn't define/import it.
        We require `prefix_module in self.symbols[suffix]` to scope the check.
        """
        for split in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:split])
            suffix_parts = parts[split:]
            suffix = ".".join(suffix_parts)
            if prefix not in self.modules:
                continue
            # prefix is a valid module — scope symbol check to this module
            if suffix in self.symbols and prefix in self.symbols[suffix]:
                return Verdict(exists=True, kind="symbol")
            # Single uppercase-initial suffix → class reference
            if (
                len(suffix_parts) == 1
                and suffix_parts[0]
                and suffix_parts[0][0].isupper()
            ):
                if _subject_matches_any(token, self.subjects):
                    return Verdict(exists=True, kind="subject")
                return Verdict(exists=False, kind="symbol")
            # Multi-segment / lowercase suffix → fall through to subject check
            break
        return None

    def _resolve_dotted_with_subjects(self, token: str) -> Verdict:
        """Resolve a dotted token: module, symbol, subject, or dead ref.

        Priority:
          1. Direct module match → kind=module, exists=True
          2. Qualified symbol (module.Symbol) → kind=symbol
          3. NATS subject (lyra.*, $JS.*, $KV.*, _inbox.*) → kind=subject
          4. Project-namespace root not found → kind=module, exists=False
          5. External / unknown → kind=unknown
        """
        # 1. Direct module match
        if token in self.modules:
            return Verdict(exists=True, kind="module")

        parts = token.split(".")

        # 2. Qualified symbol via split-point scan
        symbol_verdict = self._resolve_symbol_at_split(token, parts)
        if symbol_verdict is not None:
            return symbol_verdict

        # Also check: last-dot prefix is a module and final segment is a symbol
        # SCOPED: require mod_prefix in symbols[last] to avoid false positives
        # when 'last' is imported elsewhere but not defined in mod_prefix.
        last = parts[-1]
        mod_prefix = ".".join(parts[:-1])
        if mod_prefix in self.modules:
            if last in self.symbols and mod_prefix in self.symbols[last]:
                return Verdict(exists=True, kind="symbol")
            # lowercase final segment — fall through to subject check

        # 3. NATS subject check (before declaring project token dead)
        if _subject_matches_any(token, self.subjects):
            return Verdict(exists=True, kind="subject")

        # 3b. Dead subject-namespace disambiguation ($JS/$KV/_inbox, lyra.*, factory.*)
        namespace_verdict = self._resolve_dead_namespace(token, parts)
        if namespace_verdict is not None:
            return namespace_verdict

        # 4. Project namespace root not found as module or subject (non-factory
        # project prefixes, e.g. roxabi_nats, are pure module namespaces).
        if parts[0] in _PROJECT_PREFIXES:
            return Verdict(exists=False, kind="module")

        # 5. External / unknown
        return Verdict(exists=False, kind="unknown")

    def _resolve_dead_namespace(self, token: str, parts: list[str]) -> Verdict | None:
        """Verdict for a token in a NATS subject namespace, or None if not owned.

        A subject-namespace token not in the live subjects set is a dead subject
        reference. $JS/$KV/_inbox namespaces are always NATS-owned (not project
        prefixes), so they bypass the _PROJECT_PREFIXES guard → kind=subject.

        `lyra.` is the legacy NATS subject namespace — a dead `lyra.X` is a dead
        subject. `factory.` is special post-#1670: it is BOTH the Python package
        prefix AND the live subject root. Real `factory.*` subjects resolve in the
        caller via the subjects set; for a DEAD `factory.X` we disambiguate by
        module shape — a real submodule prefix (`factory.<sub>…` in modules) means
        a dead module/submodule, otherwise the token is subject-shaped → orphan
        subject (so check_subject_literals can flag undeclared `factory.*` literals).

        Known limitation (#1670 review): a few prefixes are BOTH a module dir AND a
        live subject root — `factory.inbound`, `factory.outbound`, `factory.typing`.
        A *dead* token under one of these (e.g. a not-yet-declared
        `factory.inbound.newplatform.bot`) resolves here as kind=module, so
        check_subject_literals would not flag it as an orphan subject. Impact is
        low: every currently-declared subject in these namespaces is covered by a
        `>` wildcard in the subjects set and resolves in the caller (step 3) before
        reaching this method — only a future, undeclared literal would be missed.
        See test_resolve_dead_factory_module_subject_collision.
        """
        lower = token.lower()
        if any(lower.startswith(p) for p in ("$js.", "$kv.", "_inbox.", "lyra.")):
            return Verdict(exists=False, kind="subject")
        if lower.startswith("factory."):
            if any(".".join(parts[:k]) in self.modules for k in range(2, len(parts))):
                return Verdict(exists=False, kind="module")
            return Verdict(exists=False, kind="subject")
        return None

    def _resolve_bare(self, token: str) -> Verdict:
        """Resolve a bare word (no dots).

        Only flags as dead symbol when confident this is a project-owned class.
        External symbols, ALL_CAPS constants, and known third-party names
        resolve as kind=unknown (no false positive).
        """
        # Python builtins always live
        if token in self._builtins:
            return Verdict(exists=True, kind="symbol")

        # Known project or imported symbol
        if token in self.symbols:
            return Verdict(exists=True, kind="symbol")

        # ALL_CAPS tokens: HTTP verbs, Unix signals, config flags, enum values
        if re.match(r"^[A-Z][A-Z0-9_]+$", token):
            return Verdict(exists=False, kind="unknown")

        # PascalCase (mixed case, no underscores) → project-class candidate
        if re.match(r"^[A-Z][a-zA-Z0-9]*[a-z][a-zA-Z0-9]*$", token):
            if token in _EXTERNAL_KNOWN_NAMES:
                return Verdict(exists=False, kind="unknown")
            return Verdict(exists=False, kind="symbol")

        # Genuinely external or non-classifiable
        return Verdict(exists=False, kind="unknown")
