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
  PEP 563 does nothing for them. This is the case a hand-rolled check misses;
- the type arguments of `cast()`, `NewType()` and `TypeVar()` (constraints and
  `bound=`), for the same reason: those positions are types by the callable's
  own contract, so a `|` in them is always a union and never arithmetic;
- **bare assignments** at module or class level whose right-hand side the
  discriminator below identifies as a type — `Handler = str | None`. These are
  `ast.Assign`, not `ast.AnnAssign`, and the future import does not rescue
  them either.

## The discriminator, and what it costs (#239)

`Handler = str | None` and `MASK = READ | WRITE` are the same `ast.BinOp`, and
nothing in the AST separates them without type information. Flagging every
module-level `|` would fire on legitimate bitwise code, and a guard people
learn to ignore is worse than no guard (`claude-supertool` #577).

So the rule is not a naming convention and not a guess. A `|` chain is a type
union when **any** operand is something that cannot be bitwise-or'd on any
Python: the literal `None`, a builtin type name (`int`, `str`, …), a name
imported from `typing`, or a subscript of one (`Dict[str, int]`). `x | None`
and `int | str` have no valid bitwise meaning at all — the language decides
this, not a heuristic — so a flag here cannot be a false positive. A name the
module rebinds itself (`str = SomeFlag`) stops counting as a type.

The cost is paid on the other side, deliberately: an alias over names the
guard cannot resolve — `Ids = A | B` — is **not** flagged. It errs toward
missing a real violation rather than toward noise on legitimate code, because
the guard's usefulness depends entirely on people trusting what it says.

Those cases are not silent. They are reported as `GuardReport.undecided` and
counted in the report's reason: seen, not classified, not passed off as clean.

## What is deliberately not a violation

Annotations on **function-local** variables are never evaluated by any Python
(PEP 526), so `def f(): x: int | None = None` cannot break a 3.9 leg. It is
not flagged. A scanner's scope has to match the defect's; flagging things that
look like the defect but cannot cause it is how a guard loses the trust it
exists to provide (`claude-supertool` #577).

Function-local **assignments** (`def f(): H = str | None`) are out of scope
for the same reason at one remove: they are evaluated when the function runs,
not when the module imports, so they fail one test rather than taking out a
whole leg at collection. A real 3.9 bug, but a visible one.

The body of `if TYPE_CHECKING:` never executes, so nothing in it is flagged.
The `else:` branch does execute, and is.

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
from typing import FrozenSet, List, Optional, Sequence, Set, Tuple

STATUS_OK = "ok"
STATUS_VIOLATIONS = "violations"
STATUS_SKIPPED = "skipped"

PEP604_NATIVE = (3, 10)

_EXCLUDED_NAMES = frozenset({"venv", "node_modules", "build", "dist", "__pycache__"})
_RUNTIME_TYPE_CHECKS = frozenset({"isinstance", "issubclass"})
_MATRIX_RE = re.compile(r"python-version:\s*\[([^\]]*)\]")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)")

_TYPING_MODULES = frozenset({"typing", "typing_extensions"})

_BUILTIN_TYPE_NAMES = frozenset({
    "bool", "bytearray", "bytes", "complex", "dict", "float", "frozenset",
    "int", "list", "memoryview", "object", "set", "str", "tuple", "type",
})

VERDICT_TYPE = "type"
VERDICT_BITWISE = "bitwise"
VERDICT_UNKNOWN = "unknown"

_ALIAS_REASON = "type alias assignment"
_AMBIGUOUS_REASON = (
    "ambiguous `|` in an import-time assignment — not classifiable as a type "
    "union or as a bitwise operation without type information"
)


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
    undecided: Tuple[Violation, ...] = ()


@dataclass(frozen=True)
class ScanResult:
    """One file's two channels. `undecided` is the discriminator declining to
    answer out loud rather than passing quietly — see `_classify`."""

    violations: List[Violation]
    undecided: List[Violation]


@dataclass(frozen=True)
class _Symbols:
    """What a module says about its own names. Used to keep `str` meaning the
    builtin type unless the module itself rebound it."""

    typing_names: FrozenSet[str]
    typing_modules: FrozenSet[str]
    rebound: FrozenSet[str]


@dataclass(frozen=True)
class _Context:
    path: Path
    symbols: _Symbols
    future: bool
    violations: List[Violation]
    undecided: List[Violation]


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


# ── Which call arguments are type positions ───────────────────────────────

def _type_arguments(node: ast.Call, name: Optional[str]) -> List[ast.expr]:
    """Arguments a callable promises are types, so any `|` in them is a union
    attempt by the callable's own contract and needs no discriminator.

    `isinstance`/`issubclass` were already here. `cast`'s first argument,
    `NewType`'s second, and `TypeVar`'s constraints and `bound=` are the same
    shape: evaluated ordinarily, unrescued by `from __future__ import
    annotations`, and a `TypeError` on the floor."""
    if name in _RUNTIME_TYPE_CHECKS:
        return list(node.args[1:2])
    if name == "cast":
        return list(node.args[:1])
    if name == "NewType":
        return list(node.args[1:2])
    if name == "TypeVar":
        return list(node.args[1:]) + [kw.value for kw in node.keywords if kw.arg == "bound"]
    return []


# ── The discriminator ─────────────────────────────────────────────────────
#
# `Handler = str | None` and `MASK = READ | WRITE` are the same `ast.BinOp`.
# Naming conventions and `typing` proximity both guess; this does not. It asks
# whether any operand is a thing that *cannot* be bitwise-or'd on any Python:
# `None`, a builtin type, a name imported from `typing`. `x | None` and
# `int | str` have no valid bitwise meaning at all, so flagging them cannot be
# a false positive — the language decides, not a heuristic.
#
# The cost is on the other side: `Ids = A | B` over names it cannot resolve is
# left alone, and reported as undecided rather than passed silently.

def _bound_names(tree: ast.Module) -> Set[str]:
    """Every name the module binds itself. Collected from the whole tree, not
    just module level: over-collecting only ever moves a verdict toward
    `unknown`, which is the safe direction."""
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in _TYPING_MODULES:
                    names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module not in _TYPING_MODULES:
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _module_symbols(tree: ast.Module) -> _Symbols:
    typing_names: Set[str] = set()
    typing_modules: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _TYPING_MODULES:
            for alias in node.names:
                typing_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _TYPING_MODULES:
                    typing_modules.add(alias.asname or alias.name)
    return _Symbols(
        frozenset(typing_names), frozenset(typing_modules), frozenset(_bound_names(tree))
    )


def _union_chains(node: ast.AST) -> List[ast.BinOp]:
    """Every `|` chain in a subtree, each reported once at its outermost node,
    because `A | B | None` is one expression and one verdict."""
    found: List[ast.BinOp] = []

    def visit(current: ast.AST) -> None:
        if isinstance(current, ast.BinOp) and isinstance(current.op, ast.BitOr):
            found.append(current)
            return
        for child in ast.iter_child_nodes(current):
            visit(child)

    visit(node)
    return found


def _operands(node: ast.expr) -> List[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _operands(node.left) + _operands(node.right)
    return [node]


def _is_type_operand(node: ast.expr, symbols: _Symbols) -> bool:
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    if isinstance(node, ast.Name):
        if node.id in symbols.rebound:
            return False
        return node.id in _BUILTIN_TYPE_NAMES or node.id in symbols.typing_names
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id in symbols.typing_modules
    if isinstance(node, ast.Subscript):
        return _is_type_operand(node.value, symbols)
    return False


def _is_value_operand(node: ast.expr) -> bool:
    """A literal that is not `None` settles it the other way: `1 | 2` is
    arithmetic, and nothing about it needs reporting in either channel."""
    return isinstance(node, ast.Constant) and node.value is not None


def _classify(union: ast.BinOp, symbols: _Symbols) -> str:
    operands = _operands(union)
    if any(_is_type_operand(operand, symbols) for operand in operands):
        return VERDICT_TYPE
    if any(_is_value_operand(operand) for operand in operands):
        return VERDICT_BITWISE
    return VERDICT_UNKNOWN


def _covered_by_call(value: ast.expr) -> Set[int]:
    """Unions already accounted for by a type-position argument, so a
    `T = TypeVar("T", bound=int | None)` is not reported twice."""
    covered: Set[int] = set()
    for child in ast.walk(value):
        if isinstance(child, ast.Call):
            for argument in _type_arguments(child, _called_name(child.func)):
                covered.update(id(union) for union in _unions(argument))
    return covered


def _scan_assigned_value(value: ast.expr, ctx: _Context) -> None:
    """An assignment's right-hand side at module or class level is evaluated at
    import, exactly like the annotations the guard already covers."""
    covered = _covered_by_call(value)
    for union in _union_chains(value):
        if id(union) in covered:
            continue
        verdict = _classify(union, ctx.symbols)
        if verdict == VERDICT_TYPE:
            ctx.violations.append(
                Violation(ctx.path, union.lineno, _ALIAS_REASON, _snippet(union))
            )
        elif verdict == VERDICT_UNKNOWN:
            ctx.undecided.append(
                Violation(ctx.path, union.lineno, _AMBIGUOUS_REASON, _snippet(union))
            )


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _scan(node: ast.AST, ctx: _Context, *, in_function: bool) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if not ctx.future:
            for arg in _arguments(node.args):
                if arg.annotation is not None:
                    _flag(ctx.violations, ctx.path, arg.annotation, "parameter annotation")
            if node.returns is not None:
                _flag(ctx.violations, ctx.path, node.returns, "return annotation")
        for child in ast.iter_child_nodes(node):
            _scan(child, ctx, in_function=True)
        return

    if isinstance(node, ast.ClassDef):
        for child in ast.iter_child_nodes(node):
            _scan(child, ctx, in_function=False)
        return

    if isinstance(node, ast.If) and _is_type_checking(node.test):
        for child in node.orelse:
            _scan(child, ctx, in_function=in_function)
        return

    if isinstance(node, ast.AnnAssign):
        if not ctx.future and not in_function:
            _flag(ctx.violations, ctx.path, node.annotation, "variable annotation")
        if node.value is not None and not in_function:
            _scan_assigned_value(node.value, ctx)
        for child in ast.iter_child_nodes(node):
            if child is not node.annotation:
                _scan(child, ctx, in_function=in_function)
        return

    if isinstance(node, ast.Assign):
        if not in_function:
            _scan_assigned_value(node.value, ctx)
        for child in ast.iter_child_nodes(node):
            _scan(child, ctx, in_function=in_function)
        return

    if isinstance(node, ast.Call):
        name = _called_name(node.func)
        for argument in _type_arguments(node, name):
            _flag(ctx.violations, ctx.path, argument, "{} argument".format(name))

    for child in ast.iter_child_nodes(node):
        _scan(child, ctx, in_function=in_function)


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


def scan_module(source: str, path: Path) -> ScanResult:
    """One file, both channels. A file that does not parse is reported, not
    swallowed — silence about a file we could not read is the same lie as
    silence about a file that was clean."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return ScanResult([Violation(path, exc.lineno or 0, "unparseable", str(exc.msg))], [])

    ctx = _Context(
        path=path,
        symbols=_module_symbols(tree),
        future=_has_future_annotations(tree),
        violations=[],
        undecided=[],
    )
    for node in tree.body:
        _scan(node, ctx, in_function=False)
    return ScanResult(_dedupe(ctx.violations), _dedupe(ctx.undecided))


def scan_source(source: str, path: Path) -> List[Violation]:
    """Violations in one file's source, without the undecided channel."""
    return scan_module(source, path).violations


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
    undecided: List[Violation] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            violations.append(Violation(path, 0, "unreadable", str(exc)))
            continue
        result = scan_module(source, path)
        violations.extend(result.violations)
        undecided.extend(result.undecided)

    unresolved = ""
    if undecided:
        unresolved = (
            "; {} import-time `|` expression(s) were not classifiable as a type "
            "union or as a bitwise operation and were left alone".format(len(undecided))
        )

    if violations:
        return GuardReport(
            STATUS_VIOLATIONS,
            "{} construct(s) the {}.{} floor cannot evaluate{}".format(
                len(violations), floor[0], floor[1], unresolved
            ),
            floor, len(files), tuple(violations), tuple(undecided),
        )
    return GuardReport(
        STATUS_OK,
        "{} files scanned, none carry syntax the {}.{} floor rejects{}".format(
            len(files), floor[0], floor[1], unresolved
        ),
        floor, len(files), (), tuple(undecided),
    )
