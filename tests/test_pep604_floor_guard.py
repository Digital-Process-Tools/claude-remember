"""The PEP 604 floor guard: no `X | None` where the floor interpreter evaluates it.

#236 shipped `project: str | None` in a test helper. PEP 604 unions are
*evaluated* in annotation position, so on Python 3.9 that is a `TypeError`
raised while pytest imports the module — a collection error, which takes out
the whole matrix leg before a single test runs. All three 3.9 legs (macOS,
ubuntu, Windows) failed identically; the other nine were green; nothing on the
failing legs ran at all. Locally it was invisible, because no floor
interpreter is installed on the machine that wrote it.

The guard is a static AST walk (`tests/pep604_floor.py`), so it answers on any
interpreter in milliseconds without needing 3.9 present.

Three states, per this repo's own contract (see `test_repo_mutation_guard.py`):
`ok`, `violations`, and `skipped` — a walk that found nothing to walk, or
could not read the floor, says so. It never reports an absence it produced
itself as an absence in the world.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.pep604_floor import (
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_VIOLATIONS,
    declared_floor,
    repo_python_files,
    run_guard,
    scan_module,
    scan_source,
)

_ROOT = Path(__file__).resolve().parent.parent


# ── The guard, run against this repository ────────────────────────────────

def test_no_pep604_below_the_floor_anywhere_in_the_repo() -> None:
    report = run_guard(_ROOT)

    if report.status == STATUS_SKIPPED:
        pytest.skip(report.reason)

    assert report.status == STATUS_OK, "\n".join(
        [f"{len(report.violations)} construct(s) the floor interpreter cannot evaluate:"]
        + [
            f"  {v.path.relative_to(_ROOT)}:{v.lineno}  {v.reason}: {v.snippet}"
            for v in report.violations
        ]
    )


def test_the_repo_walk_is_not_empty_and_covers_the_code_that_ships() -> None:
    """A guard that walked nothing would pass the test above vacuously."""
    files = repo_python_files(_ROOT)
    relative = {p.relative_to(_ROOT).as_posix() for p in files}

    assert Path("pipeline/extract.py").as_posix() in relative
    assert Path("tests/conftest.py").as_posix() in relative
    assert Path("tests/test_pep604_floor_guard.py").as_posix() in relative
    assert len(files) > 20, relative


def test_the_floor_comes_from_the_ci_matrix_and_is_currently_39() -> None:
    """`pyproject.toml` declares no `requires-python`; the matrix is the floor."""
    assert declared_floor(_ROOT) == (3, 9)


# ── Detection: the fixtures that must be flagged ──────────────────────────

def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_a_parameter_annotation_is_flagged(tmp_path: Path) -> None:
    """#236's exact line."""
    path = _write(tmp_path, "helper.py", "def make(project: str | None) -> None:\n    pass\n")
    found = scan_source(path.read_text(encoding="utf-8"), path)
    assert [v.lineno for v in found] == [1]
    assert "annotation" in found[0].reason


def test_a_return_annotation_is_flagged(tmp_path: Path) -> None:
    path = _write(tmp_path, "r.py", "def get():\n    pass\n\n\ndef g() -> int | None:\n    return None\n")
    assert [v.lineno for v in scan_source(path.read_text(encoding='utf-8'), path)] == [5]


def test_a_module_level_variable_annotation_is_flagged(tmp_path: Path) -> None:
    path = _write(tmp_path, "m.py", "DEFAULT: str | None = None\n")
    assert len(scan_source(path.read_text(encoding="utf-8"), path)) == 1


def test_a_class_level_annotation_is_flagged(tmp_path: Path) -> None:
    path = _write(tmp_path, "c.py", "class C:\n    field: int | None = None\n")
    assert [v.lineno for v in scan_source(path.read_text(encoding="utf-8"), path)] == [2]


def test_the_future_import_does_not_rescue_isinstance(tmp_path: Path) -> None:
    """The part a hand-rolled check misses: `isinstance` args are ordinary
    runtime expressions, so `from __future__ import annotations` does nothing
    for them. Still a TypeError on 3.9."""
    path = _write(
        tmp_path,
        "guarded.py",
        "from __future__ import annotations\n"
        "\n"
        "def f(x: str | None) -> bool:\n"
        "    return isinstance(x, int | str)\n",
    )
    found = scan_source(path.read_text(encoding="utf-8"), path)
    assert [v.lineno for v in found] == [4], [(v.lineno, v.reason) for v in found]
    assert "isinstance" in found[0].reason


def test_issubclass_is_flagged_too(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "s.py",
        "from __future__ import annotations\n\n\ndef f(c):\n    return issubclass(c, int | str)\n",
    )
    assert len(scan_source(path.read_text(encoding="utf-8"), path)) == 1


def test_a_union_nested_inside_a_subscript_is_flagged(tmp_path: Path) -> None:
    path = _write(tmp_path, "n.py", "from typing import Dict\n\n\ndef f(d: Dict[str, int | None]):\n    pass\n")
    assert [v.lineno for v in scan_source(path.read_text(encoding="utf-8"), path)] == [4]


# ── Detection: what must NOT be flagged ───────────────────────────────────

def test_annotations_are_rescued_by_the_future_import(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "ok.py",
        "from __future__ import annotations\n"
        "\n"
        "DEFAULT: str | None = None\n"
        "\n"
        "\n"
        "def f(x: int | None) -> str | None:\n"
        "    return None\n",
    )
    assert scan_source(path.read_text(encoding="utf-8"), path) == []


def test_ordinary_bitwise_or_is_not_a_union(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "bits.py",
        "import re\n\n\nFLAGS = re.I | re.M\n\n\ndef f(a, b):\n    return a | b\n",
    )
    assert scan_source(path.read_text(encoding="utf-8"), path) == []


def test_a_string_annotation_is_never_evaluated(tmp_path: Path) -> None:
    path = _write(tmp_path, "q.py", 'def f(x: "int | None") -> None:\n    pass\n')
    assert scan_source(path.read_text(encoding="utf-8"), path) == []


def test_a_function_local_annotation_is_not_evaluated(tmp_path: Path) -> None:
    """Deliberately out of scope: Python never evaluates annotations on local
    variables, so this cannot break a 3.9 leg. The guard's scope matches the
    defect's rather than everything that looks like it."""
    path = _write(tmp_path, "local.py", "def f():\n    x: int | None = None\n    return x\n")
    assert scan_source(path.read_text(encoding="utf-8"), path) == []


def test_a_file_that_does_not_parse_is_reported_not_swallowed(tmp_path: Path) -> None:
    path = _write(tmp_path, "broken.py", "def f(:\n")
    found = scan_source(path.read_text(encoding="utf-8"), path)
    assert len(found) == 1
    assert "unparseable" in found[0].reason


# ── Scope of the walk ─────────────────────────────────────────────────────

def _stage_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pipeline").mkdir(parents=True)
    (root / "pipeline" / "real.py").write_text("x = 1\n", encoding="utf-8")
    for junk in (".venv/lib/site.py", "venv/lib/site.py", "node_modules/m/i.py",
                 "build/lib/copy.py", "pipeline/__pycache__/real.cpython-39.py",
                 ".git/hooks/x.py", "remember.egg-info/e.py"):
        p = root / junk
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def f(x: str | None): pass\n", encoding="utf-8")
    return root


def test_the_walk_excludes_machine_state_that_sits_in_the_working_tree(tmp_path: Path) -> None:
    root = _stage_repo(tmp_path)
    walked = {p.relative_to(root).as_posix() for p in repo_python_files(root)}
    assert walked == {"pipeline/real.py"}, walked


def test_an_untracked_source_file_is_still_walked(tmp_path: Path) -> None:
    """The file being written right now is exactly when the guard earns its keep."""
    root = _stage_repo(tmp_path)
    (root / "scripts").mkdir()
    (root / "scripts" / "brand_new.py").write_text("x = 1\n", encoding="utf-8")
    walked = {p.relative_to(root).as_posix() for p in repo_python_files(root)}
    assert "scripts/brand_new.py" in walked


def test_a_gitignored_directory_is_excluded_even_under_an_unusual_name(tmp_path: Path) -> None:
    root = _stage_repo(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    (root / "scratch").mkdir()
    (root / "scratch" / "junk.py").write_text("def f(x: str | None): pass\n", encoding="utf-8")

    walked = {p.relative_to(root).as_posix() for p in repo_python_files(root)}
    assert "scratch/junk.py" not in walked
    assert "pipeline/real.py" in walked


# ── The floor, and what happens when it moves ─────────────────────────────

def test_the_floor_is_read_from_the_workflow_matrix(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yml").write_text(
        'jobs:\n  pytest:\n    strategy:\n      matrix:\n'
        '        python-version: ["3.11", "3.10", "3.13"]\n',
        encoding="utf-8",
    )
    assert declared_floor(root) == (3, 10)


def test_an_unreadable_matrix_yields_no_floor_rather_than_a_guess(tmp_path: Path) -> None:
    assert declared_floor(tmp_path) is None


def test_a_repo_whose_floor_is_310_skips_because_pep604_is_native(tmp_path: Path) -> None:
    root = _stage_repo(tmp_path)
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yml").write_text('        python-version: ["3.10", "3.12"]\n', encoding="utf-8")
    (root / "pipeline" / "real.py").write_text("def f(x: str | None): pass\n", encoding="utf-8")

    report = run_guard(root)
    assert report.status == STATUS_SKIPPED
    assert "3.10" in report.reason


# ── The third state ───────────────────────────────────────────────────────

def test_no_files_to_walk_is_skipped_not_passed(tmp_path: Path) -> None:
    """The defect class this repo keeps fixing: an absence the checker produced,
    read as an absence in the world."""
    root = tmp_path / "repo"
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yml").write_text('        python-version: ["3.9", "3.12"]\n', encoding="utf-8")

    report = run_guard(root)
    assert report.status == STATUS_SKIPPED
    assert report.files_scanned == 0
    assert "no python files" in report.reason.lower()


def test_an_unknown_floor_is_skipped_not_passed(tmp_path: Path) -> None:
    root = _stage_repo(tmp_path)
    report = run_guard(root)
    assert report.status == STATUS_SKIPPED
    assert "floor" in report.reason.lower()


def test_a_violating_repo_reports_violations(tmp_path: Path) -> None:
    root = _stage_repo(tmp_path)
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yml").write_text('        python-version: ["3.9", "3.12"]\n', encoding="utf-8")
    (root / "pipeline" / "real.py").write_text(
        "def make(project: str | None) -> None:\n    pass\n", encoding="utf-8"
    )

    report = run_guard(root)
    assert report.status == STATUS_VIOLATIONS
    assert report.files_scanned == 1
    assert [v.path.name for v in report.violations] == ["real.py"]


def test_a_clean_repo_reports_ok(tmp_path: Path) -> None:
    root = _stage_repo(tmp_path)
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yml").write_text('        python-version: ["3.9", "3.12"]\n', encoding="utf-8")

    report = run_guard(root)
    assert report.status == STATUS_OK
    assert report.files_scanned == 1
    assert report.violations == ()
    assert report.undecided == ()


# ── Bare assignments: the type-operand discriminator (#239) ───────────────
#
# `Handler = str | None` is an `ast.Assign`, not an `ast.AnnAssign`, and is
# evaluated at import exactly like the annotation cases. The discriminator is
# not a naming convention or a guess: `x | None` and `int | str` have no valid
# bitwise meaning on *any* Python, so a `|` chain with a definitely-a-type
# operand is a union attempt by the language's own rules.


def _violations(tmp_path: Path, name: str, body: str) -> list:
    path = _write(tmp_path, name, body)
    return scan_source(path.read_text(encoding="utf-8"), path)


def _undecided(tmp_path: Path, name: str, body: str) -> list:
    path = _write(tmp_path, name, body)
    return scan_module(path.read_text(encoding="utf-8"), path).undecided


def test_a_bare_module_level_type_alias_is_flagged(tmp_path: Path) -> None:
    """#239's headline case. Evaluated at import; TypeError on 3.9."""
    found = _violations(tmp_path, "alias.py", "Handler = str | None\n")
    assert [v.lineno for v in found] == [1], [(v.lineno, v.reason) for v in found]
    assert "assignment" in found[0].reason


def test_an_alias_of_two_builtin_types_is_flagged_without_a_none(tmp_path: Path) -> None:
    """`int | str` is never bitwise — classes have no `__or__` before 3.10."""
    assert len(_violations(tmp_path, "b.py", "Ids = int | str\n")) == 1


def test_a_class_level_alias_is_flagged(tmp_path: Path) -> None:
    """A class body is executed at import, same as module level."""
    found = _violations(tmp_path, "c2.py", "class C:\n    Alias = str | None\n")
    assert [v.lineno for v in found] == [2]


def test_an_alias_over_typing_names_is_flagged(tmp_path: Path) -> None:
    body = "from typing import Dict\n\n\nRows = Dict[str, int] | None\n"
    assert [v.lineno for v in _violations(tmp_path, "t.py", body)] == [4]


def test_the_future_import_does_not_rescue_a_bare_alias(tmp_path: Path) -> None:
    """An assignment is an ordinary runtime expression, not an annotation."""
    body = "from __future__ import annotations\n\nHandler = str | None\n"
    assert [v.lineno for v in _violations(tmp_path, "f.py", body)] == [3]


def test_a_typevar_bound_is_flagged(tmp_path: Path) -> None:
    body = "from typing import TypeVar\n\n\nT = TypeVar('T', bound=int | None)\n"
    found = _violations(tmp_path, "tv.py", body)
    assert [v.lineno for v in found] == [4], [(v.lineno, v.reason) for v in found]
    assert "TypeVar" in found[0].reason


def test_typevar_constraints_are_flagged(tmp_path: Path) -> None:
    body = "from typing import TypeVar\n\n\nT = TypeVar('T', int | None, str)\n"
    assert [v.lineno for v in _violations(tmp_path, "tv2.py", body)] == [4]


def test_a_cast_type_argument_is_flagged_even_inside_a_function(tmp_path: Path) -> None:
    """`cast`'s first argument is a type position by contract, and is
    evaluated whenever the line runs — the `isinstance` case's shape."""
    body = (
        "from __future__ import annotations\n"
        "from typing import cast\n"
        "\n"
        "\n"
        "def f(v):\n"
        "    return cast(int | None, v)\n"
    )
    found = _violations(tmp_path, "cs.py", body)
    assert [v.lineno for v in found] == [6], [(v.lineno, v.reason) for v in found]
    assert "cast" in found[0].reason


def test_a_newtype_supertype_is_flagged(tmp_path: Path) -> None:
    body = "from typing import NewType\n\n\nId = NewType('Id', int | None)\n"
    assert [v.lineno for v in _violations(tmp_path, "nt.py", body)] == [4]


# ── The false-positive floor: what must stay silent ───────────────────────
#
# These are load-bearing. A guard that flags `MASK = READ | WRITE` is a guard
# people learn to ignore, which is worse than no guard (`claude-supertool`
# #577). Each of these would fail if the discriminator flagged every `BitOr`.


def test_a_flags_constant_over_unknown_names_is_not_flagged(tmp_path: Path) -> None:
    assert _violations(tmp_path, "fl.py", "MASK = READ | WRITE\n") == []


def test_integer_literals_are_not_flagged(tmp_path: Path) -> None:
    assert _violations(tmp_path, "i.py", "MASK = 1 | 2\nPERMS = 0o644 | 0o111\n") == []


def test_module_level_bitwise_over_attributes_is_not_flagged(tmp_path: Path) -> None:
    assert _violations(tmp_path, "rf.py", "import re\n\n\nFLAGS = re.I | re.M\n") == []


def test_a_function_local_alias_is_out_of_scope(tmp_path: Path) -> None:
    """Evaluated at call, not at import, so it cannot take out collection.
    A documented limitation, not an oversight — see the module docstring."""
    assert _violations(tmp_path, "lo.py", "def f():\n    H = str | None\n    return H\n") == []


def test_a_type_checking_block_is_never_executed(tmp_path: Path) -> None:
    """`TYPE_CHECKING` is False at runtime, so nothing in the body evaluates.
    Flagging it would be a false positive the old walk would also have made."""
    body = (
        "from typing import TYPE_CHECKING\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    Handler = str | None\n"
        "    DEFAULT: str | None = None\n"
    )
    assert _violations(tmp_path, "tc.py", body) == []


def test_the_else_branch_of_a_type_checking_block_is_still_scanned(tmp_path: Path) -> None:
    body = (
        "from typing import TYPE_CHECKING\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    Handler = str\n"
        "else:\n"
        "    Handler = str | None\n"
    )
    assert [v.lineno for v in _violations(tmp_path, "tc2.py", body)] == [6]


def test_a_shadowed_builtin_is_not_treated_as_a_type(tmp_path: Path) -> None:
    """`str = SomeFlag` at module level means `str | other` is not a union."""
    shadowed = "str = SomeFlag\nMASK = str | other\n"
    assert _violations(tmp_path, "sh.py", shadowed) == []
    assert len(_violations(tmp_path, "nsh.py", "MASK = str | other\n")) == 1


# ── The third state: what the discriminator could not decide ──────────────


def test_an_ambiguous_assignment_is_recorded_as_undecided(tmp_path: Path) -> None:
    """Not a violation — but not silence either. The discriminator saying
    nothing about something it saw is this repo's recurring defect."""
    undecided = _undecided(tmp_path, "u.py", "MASK = READ | WRITE\n")
    assert [u.lineno for u in undecided] == [1], [(u.lineno, u.reason) for u in undecided]
    assert "ambiguous" in undecided[0].reason


def test_a_definitely_bitwise_expression_is_not_even_undecided(tmp_path: Path) -> None:
    """An integer operand settles it; nothing to report in either channel."""
    assert _undecided(tmp_path, "u2.py", "MASK = 1 | 2\n") == []


def test_a_real_violation_is_not_also_reported_as_undecided() -> None:
    result = scan_module("Handler = str | None\n", Path("x.py"))
    assert len(result.violations) == 1
    assert result.undecided == []


def test_a_typevar_is_reported_once_not_twice(tmp_path: Path) -> None:
    """The union sits in both an assignment value and a type argument. One
    construct, one line, one report."""
    body = "from typing import TypeVar\n\n\nT = TypeVar('T', bound=int | None)\n"
    found = _violations(tmp_path, "once.py", body)
    assert len(found) == 1, [(v.lineno, v.reason) for v in found]


def test_an_annotated_aliases_value_is_flagged_despite_the_future_import(tmp_path: Path) -> None:
    """`X: TypeAlias = str | None` — the annotation is rescued by the future
    import, the assigned value is not, because it is not an annotation."""
    body = (
        "from __future__ import annotations\n"
        "from typing import TypeAlias\n"
        "\n"
        "Handler: TypeAlias = str | None\n"
    )
    assert [v.lineno for v in _violations(tmp_path, "ta.py", body)] == [4]


def test_the_undecided_count_is_visible_in_an_ok_report(tmp_path: Path) -> None:
    """A clean `ok` that quietly swallowed an unclassifiable construct would be
    the guard's own uniform on the defect it exists to catch."""
    root = _stage_repo(tmp_path)
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yml").write_text('        python-version: ["3.9", "3.12"]\n', encoding="utf-8")
    (root / "pipeline" / "real.py").write_text("MASK = READ | WRITE\n", encoding="utf-8")

    report = run_guard(root)
    assert report.status == STATUS_OK
    assert [u.lineno for u in report.undecided] == [1]
    assert "not classifiable" in report.reason


def test_a_repo_with_a_bare_alias_reports_violations(tmp_path: Path) -> None:
    """The teeth check: plant a real one, watch the guard name the line."""
    root = _stage_repo(tmp_path)
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yml").write_text('        python-version: ["3.9", "3.12"]\n', encoding="utf-8")
    (root / "pipeline" / "real.py").write_text("Handler = str | None\n", encoding="utf-8")

    report = run_guard(root)
    assert report.status == STATUS_VIOLATIONS
    assert [(v.path.name, v.lineno) for v in report.violations] == [("real.py", 1)]


def test_the_repo_has_nothing_the_discriminator_could_not_decide() -> None:
    """Currently zero, verified by an independent walk. If this ever fails, the
    guard is not wrong — someone wrote a module-level `|` it cannot classify,
    and that construct needs a human's eye once."""
    report = run_guard(_ROOT)
    if report.status == STATUS_SKIPPED:
        pytest.skip(report.reason)
    assert report.undecided == (), [
        f"  {u.path.relative_to(_ROOT)}:{u.lineno}  {u.snippet}" for u in report.undecided
    ]
