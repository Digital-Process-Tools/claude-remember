"""Sandbox hardening for the nested summarizer (#724).

Claude Security scan findings F8/F9/F10 clustered under #724: the nested
summarizer is documented (and comment-claimed) as tool-less/non-acting, but
in practice
    * the Claude route's ``--allowedTools ""`` only empties the auto-approve
      list -- built-in tools that need no approval (Read, Glob, Grep, Task,
      ...) still run (F8),
    * both routes were spawned with ``cwd=tempfile.gettempdir()``, the same
      shared directory another concurrent save's tempfiles -- and the
      merged config, which can carry a live oauth token -- live in (F8),
    * the Codex route's ``--sandbox read-only`` still allows command
      execution (only writes/network are denied) and its child inherited
      the full parent environment minus a short deny-list (F9/F10).

Each fix below is paired with a positive control -- a well-formed call must
still work exactly as before -- so a summarizer that stopped producing
anything, or stopped resolving its own credentials, cannot pass as "fixed"
(see CLAUDE.md: a negative assertion needs a positive control).
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.haiku import (
    _build_cmd,
    _build_codex_cmd,
    _call_codex,
    _codex_child_env,
    call_haiku,
)


def _mock_claude_stdout(text: str) -> str:
    return json.dumps({
        "result": text,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
    })


def _write_codex_output(cmd, **kwargs):
    out_path = cmd[cmd.index("-o") + 1]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(_write_codex_output.next_text)
    return MagicMock(returncode=0, stdout="", stderr="")


# ── F8: built-in tools must actually be disabled ───────────────────────────


def test_build_cmd_disables_all_tools_when_none_requested():
    """No tools requested (the summarizer's only real call shape today) must
    disable every built-in tool via `--tools ""` -- the CLI's own
    "disable all tools" primitive, not a hand-maintained deny-list that can
    only ever be as complete as whoever last updated it against the CLI's
    actual tool inventory (#724, F8)."""
    cmd = _build_cmd(tools=None, isolate_hooks=True)
    assert "--tools" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""


def test_build_cmd_still_allows_explicitly_requested_tools():
    """Positive control: a caller that DOES ask for tools still gets exactly
    those, both as the available set and as pre-approved."""
    cmd = _build_cmd(tools=["Read", "Write"], isolate_hooks=True)
    assert cmd[cmd.index("--tools") + 1] == "Read,Write"
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Write"


# ── F8: isolated cwd, not the shared tempdir ────────────────────────────────


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_does_not_spawn_in_the_shared_tempdir(mock_run, monkeypatch):
    """The cwd handed to subprocess.run must both differ from the shared
    tempdir AND exist AT THE MOMENT the call runs -- checked from inside a
    side_effect while the isolated directory is still open, since by the
    time this test function resumes control _isolated_summarizer_cwd's own
    `finally: shutil.rmtree(...)` has already removed it."""
    seen = {}

    def _capture_cwd_liveness(*args, **kwargs):
        seen["cwd"] = kwargs["cwd"]
        seen["existed_during_call"] = os.path.isdir(kwargs["cwd"])
        return MagicMock(returncode=0, stdout=_mock_claude_stdout("done"), stderr="")

    mock_run.side_effect = _capture_cwd_liveness
    call_haiku("prompt")
    assert seen["cwd"] != tempfile.gettempdir()
    assert seen["existed_during_call"] is True
    # Positive control on the OTHER half: cleanup actually ran, so nothing
    # is left behind once the call has returned.
    assert not os.path.isdir(seen["cwd"])


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_still_produces_a_result_from_the_isolated_cwd(mock_run):
    """Positive control: the call still works end to end."""
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_stdout("did a thing"), stderr="")
    result = call_haiku("prompt")
    assert result.text == "did a thing"


@patch("pipeline.haiku.subprocess.run")
def test_call_codex_does_not_spawn_in_the_shared_tempdir(mock_run, monkeypatch):
    _write_codex_output.next_text = "## codex thing"
    mock_run.side_effect = _write_codex_output
    _call_codex("prompt")
    cmd = mock_run.call_args[0][0]
    cwd = mock_run.call_args[1]["cwd"]
    assert cwd != tempfile.gettempdir()
    assert cmd[cmd.index("-C") + 1] == cwd


# ── F9/F10: codex child env is an allow-list, not a deny-list ──────────────


def test_codex_child_env_excludes_unrelated_secrets(monkeypatch):
    """A secret that has nothing to do with running the CLI must not reach
    a child whose sandbox still permits command execution."""
    monkeypatch.setenv("SOME_UNRELATED_TOKEN", "sk-super-secret")
    env = _codex_child_env()
    assert "SOME_UNRELATED_TOKEN" not in env


def test_codex_child_env_keeps_what_the_cli_needs(monkeypatch):
    """Positive control: PATH and HOME -- what the CLI needs to run and
    resolve its own filesystem auth -- are still passed through."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/example")
    env = _codex_child_env()
    assert env.get("PATH") == "/usr/bin:/bin"
    assert env.get("HOME") == "/home/example"


# ── #751: the allow-list must not drop Windows vars (proxies: round 18) ────


def test_codex_child_env_keeps_windows_process_vars(monkeypatch):
    """Reasoned in #751, checked here without needing an actual Windows
    runner: SYSTEMROOT/USERPROFILE/APPDATA/PATHEXT must pass through when
    they are set, whatever the host platform actually is -- the allow-list
    itself carries no platform branch to get wrong."""
    monkeypatch.setenv("SYSTEMROOT", "C:_SEP_Windows".replace("_SEP_", chr(92)))
    monkeypatch.setenv("USERPROFILE", "C:_SEP_Users_SEP_example".replace("_SEP_", chr(92)))
    monkeypatch.setenv("APPDATA", "C:_SEP_Users_SEP_example_SEP_AppData_SEP_Roaming".replace("_SEP_", chr(92)))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
    env = _codex_child_env()
    assert env.get("SYSTEMROOT") == os.environ["SYSTEMROOT"]
    assert env.get("USERPROFILE") == os.environ["USERPROFILE"]
    assert env.get("APPDATA") == os.environ["APPDATA"]
    assert env.get("PATHEXT") == ".COM;.EXE;.BAT"


# #898 rounds 16-17: the allow-list names no credential, and it is literal
# code (round 17), not config: no config layer -- trusted or not -- can add
# Codex's own env-var credential to it.
_CODEX_CREDENTIAL = "CODEX_API_KEY"


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """No user config, no merged config: only the plugin's bundled default
    (the repo's own config.json) answers, whatever the developer's own
    ~/.remember/config.json says."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("REMEMBER_DIR", str(tmp_path / "remember"))
    monkeypatch.delenv("REMEMBER_CONFIG", raising=False)
    monkeypatch.delenv("MEMORY_PROJECT_DIR", raising=False)
    return home


def _with_credential_listed() -> list:
    """What a round-16 operator config would have listed: the old shipped
    list plus the Codex credential's name."""
    return ["PATH", "HOME", "CODEX_HOME", "HTTPS_PROXY", _CODEX_CREDENTIAL]


def test_codex_child_env_drops_the_codex_api_key_by_default(monkeypatch, isolated_config):
    """#898 rounds 16-17: the allow-list names no credential, so an env-var
    Codex credential does NOT reach the child. Positive control in the same
    run: PATH, on the list, still does -- an env that passed nothing would
    not pass this test."""
    monkeypatch.setenv(_CODEX_CREDENTIAL, "sk-codex-example")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = _codex_child_env()
    assert _CODEX_CREDENTIAL not in env
    assert env.get("PATH") == "/usr/bin:/bin"


def test_codex_child_env_ignores_the_operators_own_allow_list(monkeypatch, isolated_config):
    """#898 round 17: round 16 let an operator add the Codex credential's
    name in ~/.remember/config.json; the list is literal code now, so that
    config no longer widens it. Positive control: PATH still passes."""
    d = isolated_config / ".remember"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"haiku": {
        "codex_env_allow": _with_credential_listed(),
    }}), encoding="utf-8")
    monkeypatch.setenv(_CODEX_CREDENTIAL, "sk-codex-example")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = _codex_child_env()
    assert _CODEX_CREDENTIAL not in env
    assert env.get("PATH") == "/usr/bin:/bin"


def test_codex_child_env_ignores_a_cloned_repos_own_allow_list(monkeypatch, isolated_config, tmp_path):
    """#726: a cloned repository's own .remember/config.json cannot widen
    the allow-list -- not even by one credential name."""
    project = tmp_path / "project"
    remember = project / ".remember"
    remember.mkdir(parents=True)
    (remember / "config.json").write_text(json.dumps({"haiku": {
        "codex_env_allow": _with_credential_listed(),
    }}), encoding="utf-8")
    monkeypatch.setenv("MEMORY_PROJECT_DIR", str(project))
    monkeypatch.setenv("REMEMBER_DIR", str(remember))
    monkeypatch.setenv(_CODEX_CREDENTIAL, "sk-codex-example")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = _codex_child_env()
    assert _CODEX_CREDENTIAL not in env
    assert env.get("PATH") == "/usr/bin:/bin"


def _env_value_ci(env: dict, name: str):
    """Case-insensitive lookup into the plain dict `_codex_child_env()`
    returns (#792). CPython's `os.environ` on Windows folds every key to
    ONE case internally (`os.py`'s `_Environ` uses `str.upper` as both
    `encodekey` and `decodekey` on `nt`), so `os.environ.items()` there
    only ever yields uppercase keys, regardless of which case a caller
    used to set the variable -- confirmed by reading `os.py`'s own
    `_Environ.__setitem__`/`__iter__`, not merely asserted. A test that
    sets both `HTTPS_PROXY` and `https_proxy` in the SAME run and then
    asserts on the plain dict `_codex_child_env()` built from
    `os.environ.items()` collapses to one real variable there, and the
    OTHER case's literal key is simply absent from the result -- not a
    production bug (the value still reaches the child correctly, under
    whichever case Windows folded it to, and Windows' own environment
    block is itself case-insensitive), but a test written against
    POSIX's case-preserving `os.environ` failing on Windows for a reason
    that has nothing to do with the allow-list itself. Checking
    case-insensitively here is what makes the assertion platform-
    independent instead of platform-lucky."""
    name_upper = name.upper()
    for k, v in env.items():
        if k.upper() == name_upper:
            return v
    return None


# #898 round 18 (maintainer decision): the proxy and CA-bundle variables #751
# added are off the allow-list, in either casing. No user ever asked for proxy
# support (#751 was a reasoned audit finding) and #798 had flagged the same
# names as a leak risk. Behind a proxy, REMEMBER_SUMMARIZER=claude inherits the
# full environment. Each test below pairs the "does not reach" assertion with
# PATH reaching the child in the same run.


def test_codex_child_env_drops_proxy_and_ca_vars(monkeypatch):
    """Round 18: the upper-case proxy names and both CA-bundle names do not
    reach the child. One case per variable here; the lowercase case is its
    own test below, so the two are never set in the same run (#792: they are
    one variable on Windows, where os.environ folds casing)."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/custom-ca.pem")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/ssl/custom-ca.pem")
    env = _codex_child_env()
    assert env.get("PATH") == "/usr/bin:/bin", "positive control"
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                 "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS"):
        assert _env_value_ci(env, name) is None, name


def test_codex_child_env_drops_lowercase_proxy_vars_too(monkeypatch):
    """Round 18: the lowercase spellings (#751/#792 read them on POSIX) do
    not reach the child either."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("https_proxy", "http://proxy.example:9090")
    monkeypatch.setenv("http_proxy", "http://proxy.example:9090")
    monkeypatch.setenv("no_proxy", "localhost")
    env = _codex_child_env()
    assert env.get("PATH") == "/usr/bin:/bin", "positive control"
    for name in ("https_proxy", "http_proxy", "no_proxy"):
        assert _env_value_ci(env, name) is None, name


def test_codex_child_env_on_a_windows_style_folded_environ(monkeypatch):
    """#792's shape, exercised directly: real Windows `os.environ` folds
    EVERY key to uppercase (CPython `os.py`, `_createenviron`'s `nt`
    branch), simulated by replacing `pipeline.haiku.os.environ` with a plain
    dict whose keys are already uppercase. Round 18: the folded proxy key
    is dropped; PATH, folded the same way, still passes (positive control)."""
    fake_windows_environ = {"PATH": "C:/bin", "HTTPS_PROXY": "http://proxy.example:7070"}
    monkeypatch.setattr("pipeline.haiku.os.environ", fake_windows_environ)
    env = _codex_child_env()
    assert _env_value_ci(env, "PATH") == "C:/bin", "positive control"
    assert _env_value_ci(env, "https_proxy") is None
    assert _env_value_ci(env, "HTTPS_PROXY") is None


def test_codex_child_env_still_excludes_unrelated_secrets_after_widening(monkeypatch):
    """Negative assertion's own positive control, restated after the
    widening: an unrelated secret must still not reach the child even now
    that the allow-list has grown."""
    monkeypatch.setenv("SOME_UNRELATED_TOKEN", "sk-super-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-example")
    env = _codex_child_env()
    assert "SOME_UNRELATED_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env


@patch("pipeline.haiku.subprocess.run")
def test_call_codex_uses_the_allowlisted_env(mock_run, monkeypatch):
    monkeypatch.setenv("SOME_UNRELATED_TOKEN", "sk-super-secret")
    _write_codex_output.next_text = "## codex thing"
    mock_run.side_effect = _write_codex_output
    _call_codex("prompt")
    env = mock_run.call_args[1]["env"]
    assert "SOME_UNRELATED_TOKEN" not in env


# ── #798: the allow-list only bounds CODEX's OWN process env -- a command ──
# the model spawns inside the sandbox does not automatically inherit that
# same allow-listed dict; Codex's own `shell_environment_policy` is the
# mechanism that governs what environment SPAWNED commands receive, and it
# must be set independently of `_codex_child_env` (the allow-list stays
# necessary for Codex's own process; it is not sufficient for what a
# model-issued shell command can read).


def test_build_codex_cmd_denies_spawned_commands_the_allowlisted_env():
    """The argv Codex's own process receives must carry a `-c` override
    telling Codex's OWN shell_environment_policy to hand spawned commands an
    empty environment (`inherit=none`) -- confirmed against codex-cli
    0.153.2's own `--help` and the official Codex manual (fetched
    2026-09-26): `shell_environment_policy` is a real, documented dotted-path
    config key, settable via `-c` independently of whether config.toml is
    loaded (this call already passes `--ignore-user-config`). Without this,
    whatever `_codex_child_env` passes (at the time of #798: an
    operator-added Codex credential, the proxy vars, the CA bundle; since
    #898 rounds 16-18, none of those) is exactly as reachable by a
    transcript-injected shell command as it is by Codex's own process,
    because nothing here would distinguish the two (#798, gate-3 audit)."""
    cmd = _build_codex_cmd("/tmp/out.txt", "/tmp/cwd")
    assert "-c" in cmd
    override_index = cmd.index("-c") + 1
    assert cmd[override_index] == "shell_environment_policy.inherit=none"


@patch("pipeline.haiku.subprocess.run")
def test_call_codex_passes_path_but_no_proxy_while_denying_spawned_commands(
    mock_run, monkeypatch, isolated_config
):
    """Positive control for #798: Codex's OWN process (the `env=` kwarg
    subprocess.run receives) still gets PATH, in the SAME call whose argv
    also carries the `shell_environment_policy.inherit=none` override.
    #898 rounds 16-17: the Codex credential reaches neither. Round 18
    (maintainer decision): neither does the proxy -- behind one, use
    REMEMBER_SUMMARIZER=claude."""
    monkeypatch.setenv(_CODEX_CREDENTIAL, "sk-codex-example")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    _write_codex_output.next_text = "## codex thing"
    mock_run.side_effect = _write_codex_output
    _call_codex("prompt")
    env = mock_run.call_args[1]["env"]
    cmd = mock_run.call_args[0][0]
    assert _env_value_ci(env, "PATH") == "/usr/bin:/bin", "positive control"
    assert _env_value_ci(env, "HTTPS_PROXY") is None
    assert _CODEX_CREDENTIAL not in env
    assert "shell_environment_policy.inherit=none" in cmd
