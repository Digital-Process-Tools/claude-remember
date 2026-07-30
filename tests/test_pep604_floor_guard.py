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
