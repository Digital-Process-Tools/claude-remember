"""#898 round 10: every environment read in shipped Python names its
variable literally.

The directory portal's MCP_FORWARDS_CREDENTIAL_ENV hold cited
"reads the installer's ANTHROPIC_API_KEY_ENV (file pipeline/haiku.py)" --
`os.environ.get(SOME_CONSTANT)`, "an environment variable named at run
time". claude-directory-publishing triggers.md (the Python table) lists
the shapes the portal cites and the rewrite for each, and notes the read
walks file by file, one per scan, so the whole shipped set is swept at
once:

- `os.environ.get(CONST)` / `os.environ[CONST]` / `os.getenv(var)`
  -> the literal name;
- an alias, `env = os.environ if env is None else env` -> no alias;
- `os.environ` passed wholesale to a function -> the function reads it;
- a parameter named `env` -> `overrides`;
- a comparison or store against a name-holding constant
  (`k == ANTHROPIC_API_KEY_ENV`, `env[NESTED_SUMMARIZER_ENV] = "1"`)
  -> the literal name.

Each negative assertion over the shipped files is paired with a positive
control on a synthetic source that carries the shape.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# What the release build ships: pipeline/ whole, and scripts/*.py less the
# developer-only reports the build leaves out.
NOT_SHIPPED = {"report_test_durations.py", "report_windows_skip_floor.py",
               "windows_skip_triage_497.py"}
SHIPPED_PY = sorted(
    list((REPO_ROOT / "pipeline").glob("*.py"))
    + [p for p in (REPO_ROOT / "scripts").glob("*.py") if p.name not in NOT_SHIPPED]
)

_ENV_RECEIVERS = {"env", "environ"}
_NAME_CONSTANT = re.compile(r"^_?[A-Z][A-Z0-9_]*_(ENV|VAR|NAME)$")


def _is_os_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _is_env_receiver(node: ast.AST) -> bool:
    return _is_os_environ(node) or (isinstance(node, ast.Name) and node.id in _ENV_RECEIVERS)


def _literal(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def non_literal_env_reads(source: str) -> list:
    hits = []
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr in ("get", "pop", "setdefault")
                    and _is_env_receiver(f.value)
                    and not (node.args and _literal(node.args[0]))):
                hits.append((line, "env read by a non-literal name"))
            if (isinstance(f, ast.Attribute) and f.attr == "getenv"
                    and isinstance(f.value, ast.Name) and f.value.id == "os"
                    and not (node.args and _literal(node.args[0]))):
                hits.append((line, "os.getenv by a non-literal name"))
            if any(_is_os_environ(a) for a in node.args) or any(
                    _is_os_environ(k.value) for k in node.keywords):
                hits.append((line, "os.environ passed wholesale"))
            if (isinstance(f, ast.Attribute) and f.attr in ("items", "keys", "values", "copy")
                    and _is_os_environ(f.value)):
                hits.append((line, f"os.environ.{f.attr}() walks or copies the environment"))
        elif isinstance(node, ast.Subscript) and _is_env_receiver(node.value):
            key = node.slice
            if not isinstance(key, ast.Constant) and hasattr(key, "value"):
                key = key.value  # ast.Index on Python 3.8
            if not _literal(key):
                hits.append((line, "env subscript by a non-literal name"))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and _is_os_environ(node.iter):
            hits.append((line, "iteration over os.environ"))
        elif isinstance(node, ast.Dict) and any(
                k is None and _is_os_environ(v) for k, v in zip(node.keys, node.values)):
            hits.append((line, "os.environ unpacked into a dict"))
        elif isinstance(node, ast.IfExp):
            if _is_os_environ(node.body) or _is_os_environ(node.orelse):
                hits.append((line, "an alias of os.environ"))
        elif isinstance(node, ast.Assign) and _is_os_environ(node.value):
            hits.append((line, "an alias of os.environ"))
        elif isinstance(node, ast.arg) and node.arg == "env":
            hits.append((line, "a parameter named env"))
        elif isinstance(node, ast.Compare):
            for side in [node.left, *node.comparators]:
                if isinstance(side, ast.Name) and _NAME_CONSTANT.match(side.id):
                    hits.append((line, f"compared against the name constant {side.id}"))
    return hits


POSITIVE = {
    "get by constant": "import os\nX_ENV = 'A'\nv = os.environ.get(X_ENV)\n",
    "subscript by constant": "import os\nX_ENV = 'A'\nv = os.environ[X_ENV]\n",
    "getenv by variable": "import os\nn = 'A'\nv = os.getenv(n)\n",
    "alias": "import os\ndef f(o=None):\n    e = os.environ if o is None else o\n",
    "wholesale": "import os\nf(os.environ)\n",
    "env parameter": "def f(env):\n    return 1\n",
    "store by constant": "X_ENV = 'A'\nenv = {}\nenv[X_ENV] = '1'\n",
    "compare to constant": "X_ENV = 'A'\nok = [k for k in d if k == X_ENV]\n",
    # #898 round 15: the environment is never walked or copied either.
    "items walk": "import os\nd = {k: v for k, v in os.environ.items()}\n",
    "keys walk": "import os\nd = list(os.environ.keys())\n",
    "copy": "import os\nd = os.environ.copy()\n",
    "dict copy": "import os\nd = dict(os.environ)\n",
    "for loop": "import os\nfor k in os.environ:\n    pass\n",
    "comprehension": "import os\nd = [k for k in os.environ]\n",
    "unpacked": "import os\nd = {**os.environ}\n",
    "pop by variable": "import os\nn = 'A'\nos.environ.pop(n, None)\n",
    "store by variable": "import os\nn = 'A'\nos.environ[n] = '1'\n",
}

NEGATIVE = (
    "import os\n"
    "v = os.environ.get('HOME', '')\n"
    "w = os.environ['HOME']\n"
    "def f(overrides=None):\n"
    "    child = {'PATH': os.environ.get('PATH')}\n"
    "    os.environ.pop('CLAUDECODE', None)\n"
    "    os.environ['REMEMBER_NESTED_SUMMARIZER'] = '1'\n"
    "    return child\n"
)

# The only sites allowed to touch the environment by a name they did not
# write out, each with the exact number of such accesses it makes:
#   _without_session_env -- removing the parent session's variables, named by
#     config (`haiku.strip_session_env`, #95), so a future Claude Code session
#     variable can be listed without a code release (maintainer decision, #898
#     round 15): one read to save the value, one store to restore it.
#   _codex_child_env -- copying the Codex child's allow-listed variables,
#     named by config (`haiku.codex_env_allow`, #724), so the shipped list
#     names no credential and an operator adds one in their own config
#     (maintainer decision, #898 round 16): one read per configured name.
# Both are run-time-named accesses by design, disclosed here rather than
# disguised -- the directory scan may still cite them.
EXEMPT = {
    ("haiku.py", "_without_session_env"): 2,
    ("haiku.py", "_codex_child_env"): 1,
}
_EXEMPT_KINDS = {"env read by a non-literal name", "env subscript by a non-literal name"}


def _function_spans(source: str) -> dict:
    spans = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans[node.name] = (node.lineno, max(
                getattr(n, "lineno", node.lineno) for n in ast.walk(node)))
    return spans


def _split_exempt(path: Path, hits: list) -> tuple:
    spans = _function_spans(path.read_text(encoding="utf-8"))
    exempt, rest = [], []
    for line, kind in hits:
        inside = any(
            fname == path.name and fn in spans and spans[fn][0] <= line <= spans[fn][1]
            for fname, fn in EXEMPT)
        (exempt if inside and kind in _EXEMPT_KINDS else rest).append((line, kind))
    return exempt, rest


@pytest.mark.parametrize("shape", sorted(POSITIVE))
def test_each_shape_is_flagged(shape):
    """Positive control: the scan reaches the node and fires."""
    assert non_literal_env_reads(POSITIVE[shape])


def test_literal_reads_are_not_flagged():
    assert non_literal_env_reads(NEGATIVE) == []


def test_shipped_python_set_is_not_empty():
    names = {p.name for p in SHIPPED_PY}
    assert {"haiku.py", "host.py", "spawn_guard.py"} <= names


def test_host_reads_every_name_its_registry_declares(monkeypatch):
    """host.py now reads the process environment through one literal table;
    a registry name missing from it would silently read as unset. Every
    declared name, set to a marker, must come back through the table."""
    from pipeline import host as _host

    declared = set(_host.PLUGIN_ROOT_VARS) | {_host.TRANSCRIPT_PATH_VAR}
    for h in (*_host.REGISTRY, _host.GEMINI, _host.UNKNOWN):
        declared |= set(h.plugin_root_vars) | set(h.project_dir_vars) | set(h.signature_vars)
    assert len(declared) >= 9  # positive control: the registry was read
    for name in declared:
        monkeypatch.setenv(name, f"marker-{name}")
    values = _host._environment_values()
    assert {n: values.get(n) for n in declared} == {n: f"marker-{n}" for n in declared}


@pytest.mark.parametrize("path", SHIPPED_PY, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_shipped_python_reads_the_environment_by_literal_name(path):
    hits = non_literal_env_reads(path.read_text(encoding="utf-8"))
    _, rest = _split_exempt(path, hits)
    assert rest == [], f"{path.relative_to(REPO_ROOT)}: {rest}"


def test_the_exempt_sites_are_real_and_exactly_counted():
    """The exemption is not vacuous and not a blanket: each exempt function
    in haiku.py accesses the environment by a configured name (so the scan
    reaches it) exactly as many times as EXEMPT says -- one more access in
    either function fails here -- and nothing else in the shipped set relies
    on the exemption."""
    haiku_py = REPO_ROOT / "pipeline" / "haiku.py"
    source = haiku_py.read_text(encoding="utf-8")
    spans = _function_spans(source)
    exempt, _ = _split_exempt(haiku_py, non_literal_env_reads(source))
    assert exempt, "positive control: the exempt sites carry the shape"
    per_function = {}
    for line, _kind in exempt:
        for (fname, fn) in EXEMPT:
            if fn in spans and spans[fn][0] <= line <= spans[fn][1]:
                per_function[(fname, fn)] = per_function.get((fname, fn), 0) + 1
    assert per_function == EXEMPT, per_function
    for path in SHIPPED_PY:
        if path != haiku_py:
            used, _ = _split_exempt(path, non_literal_env_reads(path.read_text(encoding="utf-8")))
            assert used == [], path
