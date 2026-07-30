"""Static detection of syntax the *floor* interpreter cannot evaluate.

The floor is the lowest Python in `.github/workflows/tests.yml`'s matrix —
currently 3.9. `pyproject.toml` here declares no `[project]` table and no
`requires-python`, so the matrix is not a second opinion about the floor, it
is the only declaration of it, and it is the thing that actually produces the
legs a violation takes out.

## What is a violation

PEP 604 unions (`X | None`) are `types.UnionType` construction at runtime, and
that type does not exist before 3.10. So on 3.9 the union raises `TypeError`
wherever Python *evaluates* it:

- parameter, return and **module- or class-level** variable annotations, in
  files without `from __future__ import annotations`;
- `isinstance()` / `issubclass()` second arguments, **regardless** of the
  future import — those are ordinary runtime expressions, not annotations, so
  PEP 563 does nothing for them. This is the case a hand-rolled check misses.

## What is deliberately not a violation

Annotations on **function-local** variables are never evaluated by any Python
(PEP 526), so `def f(): x: int | None = None` cannot break a 3.9 leg. It is
not flagged. A scanner's scope has to match the defect's; flagging things that
look like the defect but cannot cause it is how a guard loses the trust it
exists to provide (`claude-supertool` #577).

String annotations (`x: "int | None"`) are likewise never evaluated.

## The walk

Names first and unconditionally — dot-prefixed anything (`.git`, `.venv`,
`.tox`), plus `venv`, `node_modules`, `build`, `dist`, `__pycache__`,
`*.egg-info`. Then, if the root is a git repository, whatever git reports as
ignored. Names before git because `.git` is never in git's ignored set and
because a bare tarball has no git at all; git after names so a directory
nobody anticipated is exempt without anyone having to name it.

The *ignored* set, never the tracked set: a tracked-files walk would exempt
the file being written right now, which is exactly when this guard earns its
keep.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

STATUS_OK = "ok"
STATUS_VIOLATIONS = "violations"
STATUS_SKIPPED = "skipped"

PEP604_NATIVE = (3, 10)

_EXCLUDED_NAMES = frozenset({"venv", "node_modules", "build", "dist", "__pycache__"})
_RUNTIME_TYPE_CHECKS = frozenset({"isinstance", "issubclass"})
_MATRIX_RE = re.compile(r"python-version:\s*\[([^\]]*)\]")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


@dataclass(frozen=True)
class Violation:
    """One construct the floor interpreter would raise on, and where."""

    path: Path
    lineno: int
    reason: str
    snippet: str


@dataclass(frozen=True)
class GuardReport:
    """Three states. `skipped` is not a pass — it is the guard saying it did
    not run, with the reason, rather than reporting its own silence as a
    clean result."""

    status: str
    reason: str
    floor: Optional[Tuple[int, int]]
    files_scanned: int
    violations: Tuple[Violation, ...]


# ── The floor ─────────────────────────────────────────────────────────────

def declared_floor(root: Path) -> Optional[Tuple[int, int]]:
    """Lowest interpreter in the CI matrix, or None if it cannot be read."""
    workflow = root / ".github" / "workflows" / "tests.yml"
    try:
        text = workflow.read_text(encoding="utf-8")
    except OSError:
        return None

    matrix = _MATRIX_RE.search(text)
    if matrix is None:
        return None
    versions = [(int(a), int(b)) for a, b in _VERSION_RE.findall(matrix.group(1))]
    return min(versions) if versions else None


# ── The walk ──────────────────────────────────────────────────────────────

def _git_ignored(root: Path) -> Set[str]:
    if not (root / ".git").exists():
        return set()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--ignored",
             "--exclude-standard", "--directory"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()
    return {line.rstrip("/") for line in result.stdout.splitlines() if line.strip()}


def _excluded_by_name(part: str) -> bool:
    return part.startswith(".") or part in _EXCLUDED_NAMES or part.endswith(".egg-info")


def repo_python_files(root: Path) -> List[Path]:
    """Every `.py` file in the repository that is source rather than machine
    state which happens to sit in the working tree."""
    ignored = _git_ignored(root)
    files = []
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if any(_excluded_by_name(part) for part in parts):
            continue
        posix = path.relative_to(root).as_posix()
        if any(posix == entry or posix.startswith(entry + "/") for entry in ignored):
            continue
        files.append(path)
    return files


# ── Detection ─────────────────────────────────────────────────────────────

def _unions(node: ast.AST) -> List[ast.BinOp]:
    return [
        child for child in ast.walk(node)
        if isinstance(child, ast.BinOp) and isinstance(child.op, ast.BitOr)
    ]


def _snippet(node: ast.AST) -> str:
    try:
        text = ast.unparse(node)
    except Exception:  # pragma: no cover - unparse is total on parsed trees
        return "<unprintable>"
    return text if len(text) <= 90 else text[:87] + "..."


def _has_future_annotations(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(alias.name == "annotations" for alias in node.names):
                return True
    return False


def _arguments(args: ast.arguments) -> List[ast.arg]:
    collected = list(getattr(args, "posonlyargs", [])) + list(args.args) + list(args.kwonlyargs)
    for extra in (args.vararg, args.kwarg):
        if extra is not None:
            collected.append(extra)
    return collected


def _flag(out: List[Violation], path: Path, node: ast.AST, reason: str) -> None:
    for union in _unions(node):
        out.append(Violation(path, union.lineno, reason, _snippet(union)))


def _called_name(func: ast.expr) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _scan(node: ast.AST, *, in_function: bool, future: bool,
          path: Path, out: List[Violation]) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if not future:
            for arg in _arguments(node.args):
                if arg.annotation is not None:
                    _flag(out, path, arg.annotation, "parameter annotation")
            if node.returns is not None:
                _flag(out, path, node.returns, "return annotation")
        for child in ast.iter_child_nodes(node):
            _scan(child, in_function=True, future=future, path=path, out=out)
        return

    if isinstance(node, ast.ClassDef):
        for child in ast.iter_child_nodes(node):
            _scan(child, in_function=False, future=future, path=path, out=out)
        return

    if isinstance(node, ast.AnnAssign):
        if not future and not in_function:
            _flag(out, path, node.annotation, "variable annotation")
        for child in ast.iter_child_nodes(node):
            if child is not node.annotation:
                _scan(child, in_function=in_function, future=future, path=path, out=out)
        return

    if isinstance(node, ast.Call):
        name = _called_name(node.func)
        if name in _RUNTIME_TYPE_CHECKS and len(node.args) >= 2:
            _flag(out, path, node.args[1], "{} argument".format(name))

    for child in ast.iter_child_nodes(node):
        _scan(child, in_function=in_function, future=future, path=path, out=out)


def _dedupe(found: Sequence[Violation]) -> List[Violation]:
    seen = set()
    unique = []
    for violation in found:
        key = (violation.path, violation.lineno, violation.reason)
        if key in seen:
            continue
        seen.add(key)
        unique.append(violation)
    return unique


def scan_source(source: str, path: Path) -> List[Violation]:
    """Violations in one file's source. A file that does not parse is
    reported, not swallowed — silence about a file we could not read is the
    same lie as silence about a file that was clean."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 0, "unparseable", str(exc.msg))]

    out: List[Violation] = []
    future = _has_future_annotations(tree)
    for node in tree.body:
        _scan(node, in_function=False, future=future, path=path, out=out)
    return _dedupe(out)


# ── The guard ─────────────────────────────────────────────────────────────

def run_guard(root: Path) -> GuardReport:
    floor = declared_floor(root)
    if floor is None:
        return GuardReport(
            STATUS_SKIPPED,
            "could not read the interpreter floor from "
            ".github/workflows/tests.yml — the guard has nothing to check against",
            None, 0, (),
        )
    if floor >= PEP604_NATIVE:
        return GuardReport(
            STATUS_SKIPPED,
            "floor is {}.{} — PEP 604 unions are native from 3.10, "
            "so there is nothing left for this guard to catch".format(*floor),
            floor, 0, (),
        )

    files = repo_python_files(root)
    if not files:
        return GuardReport(
            STATUS_SKIPPED,
            "walked {} and found no python files to scan — the guard did not "
            "run, which is not the same as finding nothing wrong".format(root),
            floor, 0, (),
        )

    violations: List[Violation] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            violations.append(Violation(path, 0, "unreadable", str(exc)))
            continue
        violations.extend(scan_source(source, path))

    if violations:
        return GuardReport(
            STATUS_VIOLATIONS,
            "{} construct(s) the {}.{} floor cannot evaluate".format(len(violations), *floor),
            floor, len(files), tuple(violations),
        )
    return GuardReport(
        STATUS_OK,
        "{} files scanned, none carry syntax the {}.{} floor rejects".format(len(files), *floor),
        floor, len(files), (),
    )
