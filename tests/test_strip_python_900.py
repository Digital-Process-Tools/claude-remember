"""#900 -- every shipped .py file loses its comments and docstrings at release-build time.

The directory's release-preview scanner reads Python comments and docstrings as code:
release-preview probe hD (claude-directory-publishing triggers.md) changed the
credential hold's citation on pipeline/haiku.py from "an environment variable named at
run time" to "the whole environment object" by doing nothing but removing them. So
`.github/scripts/strip_python.py` strips both from every shipped .py, and proves --
per file, failing the build otherwise -- that the result parses as Python 3.9 and has
exactly the AST of the source with its docstrings removed.

Every "must not survive" assertion is paired with a "must survive" one (a `#` inside a
string, an f-string, the shebang, the coding cookie, `from __future__`), and the
equivalence guard is shown failing on a deliberately broken stripper.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import os
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / ".github" / "scripts"
STRIP = SCRIPTS / "strip_python.py"
BUILD = SCRIPTS / "build_release_tree.py"
CONFIG = REPO_ROOT / ".github" / "release-branch.json"


def _load(path: Path, name: str):
    assert path.exists(), f"{path} does not exist (#900)"
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _strip_mod():
    return _load(STRIP, "strip_python_900")


def _comments(text: str) -> list:
    return [t.string for t in tokenize.generate_tokens(io.StringIO(text).readline)
            if t.type == tokenize.COMMENT]


SAMPLE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Module docstring that names an environment variable at run time."""

from __future__ import annotations

import os  # trailing comment

# a whole-line comment
HASH = "a # inside a string"
SINGLE = '# starts with a hash'
BLOCK = """line one
# not a comment, inside a triple-quoted string

    indented, after a blank line
"""


def f(x):
    """Function docstring."""
    # comment in a body
    return f"{x}#{x!r} # still a string"


class C:
    """Class docstring."""

    def m(self):
        """Only a docstring: the body must become pass."""


async def g():
    """Async docstring."""
    return 1


def one_line(): """one-line docstring only"""


class Empty:
    """Only a docstring."""
'''


def test_comments_are_gone_but_shebang_and_coding_stay():
    out = _strip_mod().strip_source(SAMPLE)
    assert _comments(out) == ["#!/usr/bin/env python3", "# -*- coding: utf-8 -*-"]
    lines = out.split("\n")
    assert lines[0] == "#!/usr/bin/env python3"
    assert lines[1] == "# -*- coding: utf-8 -*-"
    assert "whole-line comment" not in out
    assert "trailing comment" not in out
    assert "comment in a body" not in out


def test_docstrings_are_gone():
    out = _strip_mod().strip_source(SAMPLE)
    for word in ("Module docstring", "Function docstring", "Class docstring",
                 "Async docstring", "Only a docstring", "one-line docstring"):
        assert word not in out, word
    tree = ast.parse(out)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            assert ast.get_docstring(node) is None, getattr(node, "name", "module")


def test_string_literals_that_are_not_docstrings_stay_byte_identical():
    out = _strip_mod().strip_source(SAMPLE)
    assert 'HASH = "a # inside a string"' in out
    assert "SINGLE = '# starts with a hash'" in out
    block = SAMPLE[SAMPLE.index('BLOCK = """'):SAMPLE.index('"""\n\n\ndef f')]
    assert block in out, "a multi-line string (blank line and # line included) changed"
    assert 'return f"{x}#{x!r} # still a string"' in out


def test_from_future_stays_the_first_statement():
    out = _strip_mod().strip_source(SAMPLE)
    first = ast.parse(out).body[0]
    assert isinstance(first, ast.ImportFrom) and first.module == "__future__"


def test_a_body_left_empty_gets_pass():
    out = _strip_mod().strip_source(SAMPLE)
    tree = ast.parse(out)
    bodies = {n.name: n.body for n in ast.walk(tree)
              if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
    assert [type(s) for s in bodies["m"]] == [ast.Pass]
    assert [type(s) for s in bodies["one_line"]] == [ast.Pass]
    assert [type(s) for s in bodies["Empty"]] == [ast.Pass]
    # control: a body that still has code after its docstring does NOT get a pass
    assert ast.Pass not in [type(s) for s in bodies["g"]]


def test_the_stripped_sample_is_ast_equivalent_and_parses_as_3_9():
    mod = _strip_mod()
    out = mod.strip_source(SAMPLE)
    ast.parse(out, feature_version=(3, 9))
    assert ast.dump(ast.parse(out)) == mod.docstring_free_dump(SAMPLE)
    mod.verify(SAMPLE, out, "sample.py")  # does not raise


def test_a_docstring_followed_by_a_semicolon_statement_keeps_the_statement():
    mod = _strip_mod()
    src = 'def f():\n    "doc"; x = 1\n    return x\n'
    out = mod.strip_python(src, "semi.py")
    assert "doc" not in out
    assert "x = 1" in out


def test_the_equivalence_guard_fails_on_a_broken_stripper(monkeypatch):
    # positive control for the guard: a stripper that drops a real statement
    mod = _strip_mod()
    real = mod.strip_source

    def broken(src: str) -> str:
        return real(src).replace("    return 1\n", "    pass\n")

    monkeypatch.setattr(mod, "strip_source", broken)
    with pytest.raises(mod.StripError, match="AST"):
        mod.strip_python(SAMPLE, "sample.py")


def test_the_equivalence_guard_passes_the_real_stripper():
    # the must-pass half of the pair above: same input, real stripper, no error
    mod = _strip_mod()
    out = mod.strip_python(SAMPLE, "sample.py")
    assert "Function docstring" not in out


def test_the_guard_fails_when_a_comment_survives(monkeypatch):
    mod = _strip_mod()
    real = mod.strip_source
    monkeypatch.setattr(mod, "strip_source", lambda src: real(src) + "# left behind\n")
    with pytest.raises(mod.StripError, match="comment"):
        mod.strip_python(SAMPLE, "sample.py")


def test_a_file_that_does_not_parse_as_3_9_fails():
    mod = _strip_mod()
    src = "def f(x):\n    match x:\n        case 1:\n            return 1\n"
    with pytest.raises(mod.StripError, match="3.9"):
        mod.strip_python(src, "match.py")
    # control: the same shape written for 3.9 passes
    mod.strip_python("def f(x):\n    if x == 1:\n        return 1\n", "if.py")


def test_a_file_that_reads_dunder_doc_is_refused():
    # stripping its docstring would change what it prints
    mod = _strip_mod()
    with pytest.raises(mod.StripError, match="__doc__"):
        mod.strip_python('"""Help text."""\nprint(__doc__)\n', "doc.py")
    mod.strip_python('"""Help text."""\nprint("help")\n', "nodoc.py")


def test_leftovers_reports_comments_and_docstrings_and_not_a_hash_in_a_string():
    mod = _strip_mod()
    assert mod.leftovers('x = "# not a comment"\ny = f"{x}#"\n') == []
    assert mod.leftovers("#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\nx = 1\n") == []
    found = mod.leftovers('"""doc"""\nx = 1  # c\n')
    assert [(n, kind) for n, kind, _ in found] == [(1, "docstring"), (2, "comment")]


# -- the real tree ------------------------------------------------------------------

@pytest.fixture(scope="module")
def built_tree(tmp_path_factory):
    build = _load(BUILD, "build_release_tree_strip_900")
    out = tmp_path_factory.mktemp("strip900") / "out"
    report = build.build(REPO_ROOT, "HEAD", out, build.load_config(CONFIG))
    return out, report


def _shipped_py(root: Path) -> list:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def test_every_shipped_py_has_no_comment_and_no_docstring(built_tree):
    out, _ = built_tree
    mod = _strip_mod()
    files = _shipped_py(out)
    assert files, "no .py shipped -- the check below would pass vacuously"
    assert out / "pipeline" / "haiku.py" in files
    for p in files:
        assert mod.leftovers(p.read_text(encoding="utf-8")) == [], p


def test_every_shipped_py_is_ast_equivalent_to_its_source(built_tree):
    out, _ = built_tree
    mod = _strip_mod()
    for p in _shipped_py(out):
        rel = p.relative_to(out).as_posix()
        src = subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"HEAD:{rel}"],
                             capture_output=True, check=True).stdout.decode("utf-8")
        shipped = p.read_text(encoding="utf-8")
        ast.parse(shipped, feature_version=(3, 9))
        assert ast.dump(ast.parse(shipped)) == mod.docstring_free_dump(src), rel
        if mod.leftovers(src):
            assert len(shipped) < len(src), rel


def test_the_build_reports_shipped_py_sizes(built_tree):
    _, report = built_tree
    before, after = report["py_bytes"]
    assert before > after > 0


def test_the_stripped_pipeline_imports_and_runs(built_tree):
    out, _ = built_tree
    names = sorted(p.stem for p in (out / "pipeline").glob("*.py")
                   if p.stem not in ("__init__", "__main__"))
    code = ("import importlib, sys\n"
            f"for n in {names!r}:\n"
            "    importlib.import_module('pipeline.' + n)\n"
            "print('ok')\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, "-c", code], cwd=str(out), env=env,
                       capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"


def test_the_source_tree_keeps_its_docstrings():
    # the build reads git blobs; source pipeline modules (what every other test imports)
    # still carry their docstrings and comments
    text = (REPO_ROOT / "pipeline" / "haiku.py").read_text(encoding="utf-8")
    assert _strip_mod().leftovers(text), "source haiku.py lost its comments/docstrings"
