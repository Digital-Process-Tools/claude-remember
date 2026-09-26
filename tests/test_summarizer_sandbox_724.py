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
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.haiku import (
    _build_cmd,
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


# ── #751: the allow-list must not drop Windows or credential/proxy vars ────


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


def test_codex_child_env_keeps_the_codex_api_key(monkeypatch):
    """#751: dropping CODEX_API_KEY breaks anyone who authenticates Codex
    via env var rather than a filesystem auth.json."""
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-example")
    env = _codex_child_env()
    assert env.get("CODEX_API_KEY") == "sk-codex-example"


def test_codex_child_env_keeps_proxy_and_ca_vars(monkeypatch):
    """#751: dropping these breaks anyone behind a proxy or a custom CA
    bundle, on every platform -- both cases, since some HTTP client
    libraries only ever check one."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("https_proxy", "http://proxy.example:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/custom-ca.pem")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/ssl/custom-ca.pem")
    env = _codex_child_env()
    assert env.get("HTTPS_PROXY") == "http://proxy.example:8080"
    assert env.get("https_proxy") == "http://proxy.example:8080"
    assert env.get("HTTP_PROXY") == "http://proxy.example:8080"
    assert env.get("NO_PROXY") == "localhost"
    assert env.get("SSL_CERT_FILE") == "/etc/ssl/custom-ca.pem"
    assert env.get("NODE_EXTRA_CA_CERTS") == "/etc/ssl/custom-ca.pem"


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
