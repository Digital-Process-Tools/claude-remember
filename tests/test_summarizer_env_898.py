"""#898 round 15: the nested `claude -p` inherits this process's environment.

Maintainer decision (round 15): the summarizer's environment is no longer a
dict built by walking `os.environ`. The child inherits the environment the
hook was started with -- including the user's Claude Code login, exactly like
any process a hook starts -- and only the parent SESSION's own variables are
removed first (#95), from a list that lives in config
(`haiku.strip_session_env`, shipped default in the plugin's bundled
`config.json`), so a future Claude Code session variable can be added without
a code release. `REMEMBER_NESTED_SUMMARIZER` is set the same way (#204).

The Codex route keeps its allow-list (#724, a security control), now built
from one literal read per allowed name.

Every "removed" case is paired with an "inherited" case in the same run, so a
harness that captured nothing, or code that stripped everything, fails.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline import haiku
from pipeline.haiku import call_haiku

BUNDLED_CONFIG = REPO_ROOT / "config.json"

# The session variables Claude Code sets for the processes it starts, as
# observed in a live Claude Code 2.1.280 session (2026-10-04, names dumped
# from a tool subprocess), plus the three non-`CLAUDE_CODE_` names #95 and
# #204 already covered, and the IDE port (reasoned: set when an IDE is
# attached, not observed in that dump). This set is the one place a reader
# can see the list; a future Claude Code session variable that is not in it
# reaches the summarizer until it is added to `haiku.strip_session_env` in
# config.json AND here.
EXPECTED_DEFAULT = {
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
}


def _shipped_default() -> list:
    return json.loads(BUNDLED_CONFIG.read_text(encoding="utf-8"))["haiku"]["strip_session_env"]


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
    """No user config, no merged config: only the plugin's bundled default
    (the repo's own config.json) can answer."""
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


# ── the shipped default list ────────────────────────────────────────────────


def test_bundled_config_ships_the_session_list():
    assert _shipped_default(), "positive control: the key exists and is non-empty"
    assert set(_shipped_default()) == EXPECTED_DEFAULT


def test_bundled_default_never_lists_the_oauth_credential():
    """#131: the child's own credential shares the prefix by accident."""
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in _shipped_default()
    assert "CLAUDE_CODE_SESSION_ID" in _shipped_default(), "positive control"


def test_example_config_mirrors_the_bundled_default():
    example = json.loads((REPO_ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert example["haiku"]["strip_session_env"] == _shipped_default()
    assert "drop_env" not in example["haiku"]


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


@pytest.mark.parametrize("name", sorted(EXPECTED_DEFAULT))
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
    monkeypatch.delenv("REMEMBER_NESTED_SUMMARIZER", raising=False)
    seen = _capture(mock_run)
    call_haiku("p")
    assert "CLAUDE_CODE_SESSION_ID" not in seen[-1]["env"], "positive control"
    assert os.environ.get("CLAUDE_CODE_SESSION_ID") == "abc-123"
    assert os.environ.get("CLAUDE_PROJECT_DIR") == "/real/project"
    assert "REMEMBER_NESTED_SUMMARIZER" not in os.environ


@patch("pipeline.haiku.subprocess.run")
def test_environment_is_restored_when_the_call_fails(mock_run, monkeypatch, isolated_config):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    monkeypatch.delenv("REMEMBER_NESTED_SUMMARIZER", raising=False)
    seen = _capture(mock_run, MagicMock(returncode=1, stdout="boom", stderr=""))
    with pytest.raises(RuntimeError):
        call_haiku("p")
    assert seen and "CLAUDE_CODE_SESSION_ID" not in seen[-1]["env"]
    assert os.environ.get("CLAUDE_CODE_SESSION_ID") == "abc-123"
    assert "REMEMBER_NESTED_SUMMARIZER" not in os.environ


# ── the list comes from config ──────────────────────────────────────────────


@patch("pipeline.haiku.subprocess.run")
def test_user_config_replaces_the_default_list(mock_run, monkeypatch, isolated_config):
    _user_config(isolated_config, {"strip_session_env": ["MY_SESSION_VAR"]})
    monkeypatch.setenv("MY_SESSION_VAR", "x")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    seen = _capture(mock_run)
    call_haiku("p")
    env = seen[-1]["env"]
    assert "MY_SESSION_VAR" not in env
    assert env.get("CLAUDE_CODE_SESSION_ID") == "abc-123", "a list replaces, it does not append"


@patch("pipeline.haiku.subprocess.run")
def test_invalid_entries_are_skipped_with_a_warning_that_never_echoes(
    mock_run, monkeypatch, isolated_config
):
    _user_config(isolated_config, {"strip_session_env": [
        "SECRETISH=sk-pasted-value-898", "", "BAD-NAME", "GLOB_*", "9DIGIT", 7, "VALID_ONE",
    ]})
    monkeypatch.setenv("VALID_ONE", "x")
    monkeypatch.setenv("REMEMBER_TEST_ARBITRARY_898", "kept")
    seen = _capture(mock_run)
    with patch("pipeline.haiku._warn") as warn:
        call_haiku("p")
    env = seen[-1]["env"]
    assert "VALID_ONE" not in env, "the valid entry beside the bad ones applies"
    assert env.get("REMEMBER_TEST_ARBITRARY_898") == "kept"
    text = " ".join(str(c.args[0]) for c in warn.call_args_list)
    assert "haiku.strip_session_env" in text
    assert "sk-pasted-value-898" not in text
    assert "BAD-NAME" not in text


@patch("pipeline.haiku.subprocess.run")
def test_a_non_list_value_falls_through_to_the_next_layer(mock_run, monkeypatch, isolated_config):
    """A broken user layer must not silently turn the #95 strip off."""
    _user_config(isolated_config, {"strip_session_env": "CLAUDECODE"})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    seen = _capture(mock_run)
    with patch("pipeline.haiku._warn") as warn:
        call_haiku("p")
    assert "CLAUDE_CODE_SESSION_ID" not in seen[-1]["env"]
    assert "haiku.strip_session_env" in " ".join(str(c.args[0]) for c in warn.call_args_list)


@patch("pipeline.haiku.subprocess.run")
def test_no_list_anywhere_is_said_out_loud(mock_run, monkeypatch, isolated_config, tmp_path):
    monkeypatch.setattr(haiku, "_BUNDLED_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    seen = _capture(mock_run)
    with patch("pipeline.haiku._warn") as warn:
        call_haiku("p")
    assert seen[-1]["env"].get("CLAUDE_CODE_SESSION_ID") == "abc-123"
    text = " ".join(str(c.args[0]) for c in warn.call_args_list)
    assert "haiku.strip_session_env" in text and "#95" in text


@patch("pipeline.haiku.subprocess.run")
def test_project_local_config_cannot_change_the_list(mock_run, monkeypatch, isolated_config, tmp_path):
    """#726: a cloned repo's `.remember/config.json` must not empty the list
    (raw fallback path; the shell merge strips the whole `haiku` block)."""
    project = tmp_path / "project"
    remember = project / ".remember"
    remember.mkdir(parents=True)
    (remember / "config.json").write_text(
        json.dumps({"haiku": {"strip_session_env": []}}), encoding="utf-8")
    monkeypatch.setenv("MEMORY_PROJECT_DIR", str(project))
    monkeypatch.setenv("REMEMBER_DIR", str(remember))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    seen = _capture(mock_run)
    call_haiku("p")
    assert "CLAUDE_CODE_SESSION_ID" not in seen[-1]["env"]


@patch("pipeline.haiku.subprocess.run")
def test_trusted_external_config_can_change_the_list(mock_run, monkeypatch, isolated_config, tmp_path):
    """Positive control for the test above: the same file outside the
    project checkout is the operator's own and is honoured."""
    external = tmp_path / "external-store"
    external.mkdir()
    (external / "config.json").write_text(
        json.dumps({"haiku": {"strip_session_env": []}}), encoding="utf-8")
    monkeypatch.setenv("MEMORY_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("REMEMBER_DIR", str(external))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    seen = _capture(mock_run)
    call_haiku("p")
    assert seen[-1]["env"].get("CLAUDE_CODE_SESSION_ID") == "abc-123"


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
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "drop_env" not in text, rel
    assert "strip_session_env" in (REPO_ROOT / "pipeline" / "haiku.py").read_text(encoding="utf-8")


# ── the codex route: allow-list from literal reads ──────────────────────────


def test_codex_env_has_only_allowed_names(monkeypatch):
    for name in haiku._CODEX_CHILD_ENV_ALLOW:
        monkeypatch.setenv(name, f"v-{name}")
    monkeypatch.setenv("SOME_UNRELATED_SECRET_898", "nope")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "nope")
    env = haiku._codex_child_env()
    allowed = {n.upper() for n in haiku._CODEX_CHILD_ENV_ALLOW} | {"REMEMBER_NESTED_SUMMARIZER"}
    assert {k.upper() for k in env} <= allowed
    for name in haiku._CODEX_CHILD_ENV_ALLOW:
        got = {k.upper(): v for k, v in env.items()}.get(name.upper())
        assert got == f"v-{name}", f"{name} must reach the codex child"
    assert env["REMEMBER_NESTED_SUMMARIZER"] == "1"


def test_codex_env_skips_missing_names(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    env = haiku._codex_child_env()
    assert "CODEX_HOME" not in env
    assert env.get("PATH") == "/usr/bin"


# ── the user-facing messages name no credential command ─────────────────────


def test_no_shipped_message_names_a_credential_command():
    for rel in ("pipeline/haiku.py", "scripts/doctor.sh", "README.md", "README.release.md"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "setup-token" not in text, rel
    assert "coding agent's own CLI" in (REPO_ROOT / "pipeline" / "haiku.py").read_text(encoding="utf-8")


# ── disclosure ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rel", ["README.md", "README.release.md", "docs/configuration.md"])
def test_docs_disclose_that_the_nested_call_inherits_the_login(rel):
    text = " ".join((REPO_ROOT / rel).read_text(encoding="utf-8").split())
    assert "inherits your environment, including your Claude Code login" in text, rel
    assert "reads no credential" in text, rel
