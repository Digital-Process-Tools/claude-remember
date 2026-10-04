"""#898: the nested `claude -p` inherits this process's environment.

Round 15 (maintainer decision): the summarizer's environment is no longer a
dict built by walking `os.environ`. The child inherits the environment the
hook was started with -- including the user's Claude Code login, exactly like
any process a hook starts -- and only the parent SESSION's own variables are
removed first (#95). `REMEMBER_NESTED_SUMMARIZER` is set the same way (#204).

The Codex route keeps its allow-list (#724, a security control); the list
names no credential.

Round 17 (maintainer decision): both lists are written in code as literal
names, one `os.environ.pop("NAME", None)` / `os.environ.get("NAME")` per
name. Rounds 15-16 had moved them to config (`haiku.strip_session_env`,
`haiku.codex_env_allow`); reading the environment by a config-supplied name is
what the directory scanner reports as "an environment variable named at run
time", while a literal name is not. The names and every protection are the
same as round 16's shipped defaults; only the ability to change the lists in
config is gone. A new Claude Code session variable now needs a release.

Every "removed" case is paired with an "inherited" case in the same run, so a
harness that captured nothing, or code that stripped everything, fails.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline import haiku
from pipeline.haiku import call_haiku

HAIKU_PY = REPO_ROOT / "pipeline" / "haiku.py"

# The session variables Claude Code sets for the processes it starts, as
# observed in a live Claude Code 2.1.280 session (2026-10-04, names dumped
# from a tool subprocess), plus the three non-`CLAUDE_CODE_` names #95 and
# #204 already covered, and the IDE port (reasoned: set when an IDE is
# attached, not observed in that dump). Same names, same order as round 16's
# shipped `haiku.strip_session_env`. A future Claude Code session variable
# that is not here reaches the summarizer until a release adds it to
# `_without_session_env` AND here.
EXPECTED_SESSION_NAMES = (
    "CLAUDECODE",
    "CLAUDE_JOB_DIR",
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SSE_PORT",
)

# Round 16's shipped `haiku.codex_env_allow`, same names, same order: the
# round-15 literal table minus Codex's own API-key variable, plus the three
# lowercase proxy names (read on a case-preserving platform only, #792).
EXPECTED_CODEX_NAMES = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "CODEX_HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "USERPROFILE",
    "APPDATA",
    "PATHEXT",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
)
_LOWERCASE_PROXIES = ("https_proxy", "http_proxy", "no_proxy")

# A credential-shaped name: a `_`-separated part that names a secret.
_CREDENTIAL_PART = re.compile(
    r"(?:^|_)(?:API|KEY|KEYS|TOKEN|TOKENS|SECRET|SECRETS|PASSWORD|PASS|PWD|CRED|CREDS|AUTH)(?:_|$)",
    re.IGNORECASE,
)


def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(HAIKU_PY.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"positive control: {name} exists in pipeline/haiku.py")


def _is_os_environ(node) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _literal_environ_calls(func: ast.FunctionDef, method: str) -> list:
    """The literal first argument of every `os.environ.<method>(...)` call in
    `func`, in source order."""
    calls = [n for n in ast.walk(func)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == method and _is_os_environ(n.func.value)]
    calls.sort(key=lambda n: (n.lineno, n.col_offset))
    names = []
    for call in calls:
        arg = call.args[0]
        assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
            f"os.environ.{method} by a non-literal name at line {call.lineno}")
        names.append(arg.value)
    return names


def _literal_environ_stores(func: ast.FunctionDef) -> list:
    """The literal key of every `os.environ["NAME"] = ...` in `func`, in
    source order."""
    stores = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and _is_os_environ(target.value):
                    key = target.slice
                    if not isinstance(key, ast.Constant) and hasattr(key, "value"):
                        key = key.value  # ast.Index on Python 3.8
                    assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
                        f"os.environ store by a non-literal name at line {node.lineno}")
                    stores.append((node.lineno, key.value))
    return [name for _line, name in sorted(stores)]


def _ok():
    return MagicMock(
        returncode=0,
        stdout=json.dumps({"result": "x", "input_tokens": 1, "output_tokens": 1,
                           "cache_read_input_tokens": 0}),
        stderr="",
    )


def _capture(mock_run, result=None):
    """Record, per spawn, the environment the child would actually get: the
    `env=` mapping when one is passed, else this process's environment at the
    moment of the spawn (what a child inherits)."""
    seen = []

    def fake(cmd, **kwargs):
        env = kwargs.get("env")
        seen.append({"env_kwarg": env is not None,
                     "env": dict(os.environ) if env is None else dict(env)})
        return result if result is not None else _ok()

    mock_run.side_effect = fake
    return seen


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """No user config, no merged config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("REMEMBER_DIR", str(tmp_path / "remember"))
    monkeypatch.delenv("REMEMBER_CONFIG", raising=False)
    monkeypatch.delenv("MEMORY_PROJECT_DIR", raising=False)
    return home


def _user_config(home, haiku_block):
    d = home / ".remember"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({"haiku": haiku_block}), encoding="utf-8")


# ── the session list is literal code (round 17) ─────────────────────────────


def test_session_names_are_popped_literally_in_order():
    """One literal `os.environ.pop("NAME", None)` per name, nothing else."""
    assert _literal_environ_calls(_function("_without_session_env"), "pop") == list(
        EXPECTED_SESSION_NAMES)


def test_session_names_are_restored_literally_in_order():
    """One literal `os.environ["NAME"] = ...` restore per popped name."""
    assert _literal_environ_stores(_function("_without_session_env")) == list(
        EXPECTED_SESSION_NAMES)


def test_session_list_takes_no_parameter():
    """Round 15-16 passed the names in from config; nothing does now."""
    func = _function("_without_session_env")
    assert [a.arg for a in func.args.args] == []
    assert "names" not in {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}


def test_session_list_never_names_the_oauth_credential():
    """#131: the child's own credential shares the prefix by accident."""
    assert "CLAUDE_CODE_SESSION_ID" in EXPECTED_SESSION_NAMES, "positive control"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in EXPECTED_SESSION_NAMES
    func = _function("_without_session_env")
    names = {n.value for n in ast.walk(func) if isinstance(n, ast.Constant)}
    assert "CLAUDE_CODE_SESSION_ID" in names, "positive control: the scan reads constants"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in names


def test_the_config_keys_are_gone():
    """No config file ships or documents either list any more."""
    for rel in ("config.json", "config.example.json"):
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        assert "strip_session_env" not in text, rel
        assert "codex_env_allow" not in text, rel
    assert (REPO_ROOT / "config.example.json").exists(), "positive control"
    source = HAIKU_PY.read_text(encoding="utf-8")
    for gone in ("_configured_env_names", "_configured_strip_session_env",
                 "_configured_codex_env_allow", "_BUNDLED_CONFIG", "_SESSION_ENV_NAME"):
        assert gone not in source, gone
    assert "_without_session_env" in source, "positive control"


# ── inheritance on the claude route ─────────────────────────────────────────


@patch("pipeline.haiku.subprocess.run")
def test_claude_child_gets_no_env_mapping(mock_run, isolated_config):
    seen = _capture(mock_run)
    call_haiku("p")
    assert seen, "positive control: the spawn was reached"
    assert seen[-1]["env_kwarg"] is False, "the child must inherit, not get a built dict"


@patch("pipeline.haiku.subprocess.run")
def test_child_inherits_an_arbitrary_variable(mock_run, monkeypatch, isolated_config):
    monkeypatch.setenv("REMEMBER_TEST_ARBITRARY_898", "kept")
    seen = _capture(mock_run)
    call_haiku("p")
    assert seen[-1]["env"].get("REMEMBER_TEST_ARBITRARY_898") == "kept"


@pytest.mark.parametrize("name", EXPECTED_SESSION_NAMES)
@patch("pipeline.haiku.subprocess.run")
def test_each_listed_session_variable_is_removed(mock_run, name, monkeypatch, isolated_config):
    monkeypatch.setenv(name, "parent-session-value")
    monkeypatch.setenv("REMEMBER_TEST_ARBITRARY_898", "kept")
    seen = _capture(mock_run)
    call_haiku("p")
    env = seen[-1]["env"]
    assert env.get("REMEMBER_TEST_ARBITRARY_898") == "kept", "positive control"
    assert name not in env


@patch("pipeline.haiku.subprocess.run")
def test_all_listed_session_variables_are_removed_together(mock_run, monkeypatch, isolated_config):
    for name in EXPECTED_SESSION_NAMES:
        monkeypatch.setenv(name, f"parent-{name}")
    monkeypatch.setenv("REMEMBER_TEST_ARBITRARY_898", "kept")
    seen = _capture(mock_run)
    call_haiku("p")
    env = seen[-1]["env"]
    assert env.get("REMEMBER_TEST_ARBITRARY_898") == "kept", "positive control"
    assert [n for n in EXPECTED_SESSION_NAMES if n in env] == []
    for name in EXPECTED_SESSION_NAMES:
        assert os.environ.get(name) == f"parent-{name}", f"{name} restored"


@patch("pipeline.haiku.subprocess.run")
def test_nested_summarizer_marker_is_set(mock_run, monkeypatch, isolated_config):
    monkeypatch.delenv("REMEMBER_NESTED_SUMMARIZER", raising=False)
    seen = _capture(mock_run)
    call_haiku("p")
    assert seen[-1]["env"].get("REMEMBER_NESTED_SUMMARIZER") == "1"


@patch("pipeline.haiku.subprocess.run")
def test_oauth_credential_reaches_the_child_by_inheritance(mock_run, monkeypatch, isolated_config):
    """#131: never removed, never copied -- simply inherited."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-example-898")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    seen = _capture(mock_run)
    call_haiku("p")
    env = seen[-1]["env"]
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat-example-898"
    assert "CLAUDE_CODE_SESSION_ID" not in env, "positive control: the strip ran"


@patch("pipeline.haiku.subprocess.run")
def test_provider_selection_variables_are_now_inherited(mock_run, monkeypatch, isolated_config):
    """#316: the old prefix strip also removed provider selection (Bedrock),
    sending a proxy token to the wrong API. Not a session variable, so not
    listed, so inherited."""
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    seen = _capture(mock_run)
    call_haiku("p")
    env = seen[-1]["env"]
    assert env.get("CLAUDE_CODE_USE_BEDROCK") == "1"
    assert "CLAUDE_CODE_SESSION_ID" not in env


@patch("pipeline.haiku.subprocess.run")
def test_this_process_gets_its_environment_back(mock_run, monkeypatch, isolated_config):
    """The removal is scoped to the spawn: anything later in this process
    that reads a session variable (pipeline.host) still sees it."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/real/project")
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)
    monkeypatch.delenv("REMEMBER_NESTED_SUMMARIZER", raising=False)
    seen = _capture(mock_run)
    call_haiku("p")
    assert "CLAUDE_CODE_SESSION_ID" not in seen[-1]["env"], "positive control"
    assert os.environ.get("CLAUDE_CODE_SESSION_ID") == "abc-123"
    assert os.environ.get("CLAUDE_PROJECT_DIR") == "/real/project"
    assert "CLAUDE_JOB_DIR" not in os.environ, "a name that was unset stays unset"
    assert "REMEMBER_NESTED_SUMMARIZER" not in os.environ


@patch("pipeline.haiku.subprocess.run")
def test_environment_is_restored_when_the_call_fails(mock_run, monkeypatch, isolated_config):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "parent-handshake")
    monkeypatch.delenv("REMEMBER_NESTED_SUMMARIZER", raising=False)
    seen = _capture(mock_run, MagicMock(returncode=1, stdout="boom", stderr=""))
    with pytest.raises(RuntimeError):
        call_haiku("p")
    assert seen and "CLAUDE_CODE_SESSION_ID" not in seen[-1]["env"]
    assert "CLAUDE_CODE_MESSAGING_TOKEN" not in seen[-1]["env"]
    assert os.environ.get("CLAUDE_CODE_SESSION_ID") == "abc-123"
    assert os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN") == "parent-handshake"
    assert "REMEMBER_NESTED_SUMMARIZER" not in os.environ


def test_environment_is_restored_when_the_block_raises(monkeypatch):
    """The restore is in a `finally`: an exception inside the block still
    puts every removed name back."""
    for name in EXPECTED_SESSION_NAMES:
        monkeypatch.setenv(name, f"parent-{name}")
    with pytest.raises(ValueError), haiku._without_session_env():
        assert [n for n in EXPECTED_SESSION_NAMES if n in os.environ] == []
        raise ValueError("boom")
    for name in EXPECTED_SESSION_NAMES:
        assert os.environ.get(name) == f"parent-{name}", name


# ── config no longer changes the session list ───────────────────────────────


@patch("pipeline.haiku.subprocess.run")
def test_user_config_can_no_longer_empty_the_list(mock_run, monkeypatch, isolated_config):
    _user_config(isolated_config, {"strip_session_env": []})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    monkeypatch.setenv("REMEMBER_TEST_ARBITRARY_898", "kept")
    seen = _capture(mock_run)
    call_haiku("p")
    env = seen[-1]["env"]
    assert env.get("REMEMBER_TEST_ARBITRARY_898") == "kept", "positive control"
    assert "CLAUDE_CODE_SESSION_ID" not in env


@patch("pipeline.haiku.subprocess.run")
def test_user_config_can_no_longer_add_to_the_list(mock_run, monkeypatch, isolated_config):
    _user_config(isolated_config, {"strip_session_env": ["REMEMBER_TEST_ARBITRARY_898"]})
    monkeypatch.setenv("REMEMBER_TEST_ARBITRARY_898", "kept")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    seen = _capture(mock_run)
    call_haiku("p")
    env = seen[-1]["env"]
    assert "CLAUDE_CODE_SESSION_ID" not in env, "positive control: the strip ran"
    assert env.get("REMEMBER_TEST_ARBITRARY_898") == "kept"


@patch("pipeline.haiku.subprocess.run")
def test_drop_env_is_no_longer_read(mock_run, monkeypatch, isolated_config):
    _user_config(isolated_config, {"drop_env": ["REMEMBER_TEST_ARBITRARY_898"]})
    monkeypatch.setenv("REMEMBER_TEST_ARBITRARY_898", "kept")
    seen = _capture(mock_run)
    call_haiku("p")
    assert seen[-1]["env"].get("REMEMBER_TEST_ARBITRARY_898") == "kept"


def test_shipped_tree_no_longer_names_drop_env():
    # Shipped files only: README.md (main branch) records the removal by name.
    for rel in ("pipeline/haiku.py", "config.example.json", "config.json",
                "README.release.md", "scripts/lib-memory-dir.sh"):
        path = REPO_ROOT / rel
        if path.exists():
            assert "drop_env" not in path.read_text(encoding="utf-8"), rel
    assert "CLAUDE_CODE_SESSION_ID" in HAIKU_PY.read_text(encoding="utf-8"), "positive control"


# ── the codex route: a literal allow-list (round 17) ────────────────────────


def _upper_view(env: dict) -> dict:
    """#792: on Windows one variable may come back under either casing."""
    return {k.upper(): v for k, v in env.items()}


def test_codex_names_are_read_literally_in_order():
    """One literal `os.environ.get("NAME")` per allowed name, nothing else."""
    assert _literal_environ_calls(_function("_codex_child_env"), "get") == list(
        EXPECTED_CODEX_NAMES)


def test_codex_lowercase_proxies_are_read_only_off_windows():
    """The three lowercase reads sit under `if os.name != "nt":`, and nothing
    else does."""
    func = _function("_codex_child_env")
    guarded = []
    for node in ast.walk(func):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and ast.dump(node.test.left) == ast.dump(ast.parse("os.name", mode="eval").body)
                and isinstance(node.test.ops[0], ast.NotEq)
                and getattr(node.test.comparators[0], "value", None) == "nt"):
            guarded += [n.args[0].value for n in ast.walk(node)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "get" and _is_os_environ(n.func.value)]
    assert guarded == list(_LOWERCASE_PROXIES)


def test_codex_list_names_no_credential():
    assert _CREDENTIAL_PART.search("CODEX_API_KEY"), "positive control: the shape is caught"
    assert _CREDENTIAL_PART.search("CLAUDE_CODE_OAUTH_TOKEN"), "positive control"
    assert [n for n in EXPECTED_CODEX_NAMES if _CREDENTIAL_PART.search(n)] == []


def test_no_shipped_file_names_the_codex_credential():
    """The name lives only where an operator reads what is not passed."""
    name = "CODEX_" + "API_KEY"
    for rel in ("pipeline/haiku.py", "config.json", "config.example.json", "README.release.md"):
        path = REPO_ROOT / rel
        if path.exists():
            assert name not in path.read_text(encoding="utf-8"), rel
    assert name in (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8"), (
        "positive control: the operator docs say which name is not passed")


@pytest.mark.skipif(os.name == "nt", reason="lowercase twins are one variable on Windows (#792)")
def test_codex_env_passes_every_listed_name_and_nothing_else(monkeypatch, isolated_config):
    for name in EXPECTED_CODEX_NAMES:
        monkeypatch.setenv(name, f"v-{name}")
    monkeypatch.setenv("SOME_UNRELATED_SECRET_898", "nope")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "nope")
    monkeypatch.setenv("CODEX_" + "API_KEY", "nope")
    env = haiku._codex_child_env()
    assert env == {**{n: f"v-{n}" for n in EXPECTED_CODEX_NAMES},
                   "REMEMBER_NESTED_SUMMARIZER": "1"}


def test_codex_env_passes_every_upper_case_name_on_any_platform(monkeypatch, isolated_config):
    for name in EXPECTED_CODEX_NAMES:
        if name not in _LOWERCASE_PROXIES:
            monkeypatch.setenv(name, f"v-{name}")
    monkeypatch.setenv("SOME_UNRELATED_SECRET_898", "nope")
    env = haiku._codex_child_env()
    view = _upper_view(env)
    for name in EXPECTED_CODEX_NAMES:
        if name not in _LOWERCASE_PROXIES:
            assert view.get(name) == f"v-{name}", name
    assert "SOME_UNRELATED_SECRET_898" not in view


def test_codex_env_skips_missing_names(monkeypatch, isolated_config):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    env = haiku._codex_child_env()
    assert "CODEX_HOME" not in env
    assert env.get("PATH") == "/usr/bin"


def test_codex_env_sets_the_nested_marker(monkeypatch, isolated_config):
    monkeypatch.delenv("REMEMBER_NESTED_SUMMARIZER", raising=False)
    env = haiku._codex_child_env()
    assert env.get("REMEMBER_NESTED_SUMMARIZER") == "1"
    assert "REMEMBER_NESTED_SUMMARIZER" not in os.environ, "the parent is untouched"


class _WindowsEnviron(dict):
    """The lookup half of CPython's `nt` os.environ: keys stored upper-case,
    every lookup upper-cased (#792)."""

    def get(self, name, default=None):
        return super().get(name.upper(), default)


def test_codex_env_skips_the_lowercase_proxies_on_windows(monkeypatch):
    monkeypatch.setattr(haiku.os, "name", "nt")
    monkeypatch.setattr(haiku.os, "environ",
                        _WindowsEnviron({"PATH": "C:/bin", "HTTPS_PROXY": "http://p:1"}))
    env = haiku._codex_child_env()
    assert env == {"PATH": "C:/bin", "HTTPS_PROXY": "http://p:1",
                   "REMEMBER_NESTED_SUMMARIZER": "1"}


def test_codex_env_keeps_both_casings_where_they_differ(monkeypatch):
    """Positive control for the Windows skip: a case-preserving platform keeps
    the lowercase twin, because it is a different variable there."""
    monkeypatch.setattr(haiku.os, "name", "posix")
    monkeypatch.setattr(haiku.os, "environ",
                        {"PATH": "/bin", "HTTPS_PROXY": "http://p:1", "https_proxy": "http://p:2"})
    env = haiku._codex_child_env()
    assert env == {"PATH": "/bin", "HTTPS_PROXY": "http://p:1", "https_proxy": "http://p:2",
                   "REMEMBER_NESTED_SUMMARIZER": "1"}


def test_user_config_can_no_longer_widen_the_codex_list(monkeypatch, isolated_config):
    _user_config(isolated_config, {"codex_env_allow": [
        *EXPECTED_CODEX_NAMES, "REMEMBER_TEST_ARBITRARY_898"]})
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("REMEMBER_TEST_ARBITRARY_898", "nope")
    env = haiku._codex_child_env()
    assert env.get("PATH") == "/usr/bin", "positive control"
    assert "REMEMBER_TEST_ARBITRARY_898" not in env


def test_user_config_can_no_longer_narrow_the_codex_list(monkeypatch, isolated_config):
    _user_config(isolated_config, {"codex_env_allow": ["PATH"]})
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/ca.pem")
    env = haiku._codex_child_env()
    assert env.get("PATH") == "/usr/bin"
    assert env.get("NODE_EXTRA_CA_CERTS") == "/ca.pem"


def test_codex_spawned_commands_still_get_no_environment():
    """#798: untouched by round 17."""
    cmd = haiku._build_codex_cmd("/tmp/out.txt", "/tmp/cwd")
    assert cmd[cmd.index("-c") + 1] == "shell_environment_policy.inherit=none"


# ── the user-facing messages name no credential command ─────────────────────


def test_no_shipped_message_names_a_credential_command():
    for rel in ("pipeline/haiku.py", "scripts/doctor.sh", "README.md", "README.release.md"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "setup-token" not in text, rel
    assert "coding agent's own CLI" in HAIKU_PY.read_text(encoding="utf-8")


# ── disclosure ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rel", ["README.md", "README.release.md", "docs/configuration.md"])
def test_docs_disclose_that_the_nested_call_inherits_the_login(rel):
    text = " ".join((REPO_ROOT / rel).read_text(encoding="utf-8").split())
    assert "inherits your environment, including your Claude Code login" in text, rel
    assert "reads no credential" in text, rel


@pytest.mark.parametrize("rel", ["README.md", "docs/configuration.md"])
def test_docs_say_the_lists_are_code_not_config(rel):
    text = " ".join((REPO_ROOT / rel).read_text(encoding="utf-8").split())
    assert "strip_session_env" not in text, rel
    assert "codex_env_allow" not in text, rel
    assert "codex login" in text, rel
