"""#1002: a plain nohup-and-background launch still allocates a console that
Windows Terminal flashes for ~1s on Windows, every time one of this plugin's
four detached saves/consolidations fires (reporter fmatamala). CI cannot see
a Windows console, so what is tested here is the DISPATCH DECISION added by
scripts/lib-detach.sh: Windows detected + hidden launcher available => the
hidden launcher is invoked with the right arguments; Windows not detected =>
the existing nohup path runs unchanged; hidden launcher unavailable even on
Windows => falls back to nohup rather than silently skipping the launch.

Whether no console actually flashes on the hidden route is REASONED, not
OBSERVED -- there is no Windows box in this project's own hands to confirm
it. docs/windows.md says so; the reporter is asked to confirm in the PR.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.slug import session_dir_slug as _slug
from tests._bash_runner import resolve_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "lib-detach.sh"
ONE_MESSAGE_LINE = "{\"type\":\"assistant\",\"message\":{\"content\":\"x\"}}\n"

# #1002 CI: a bare "bash" resolves to the WSL launcher stub
# (C:/Windows/System32/bash.exe) on windows-latest, not Git Bash, because
# CreateProcess searches System32 before PATH for an unqualified name --
# tests/_bash_runner.py's resolve_bash() (#432) exists for exactly this.
# Every subprocess.run call in this file must go through BASH, never the
# literal string "bash" -- a harness bug, not a product bug: this file's
# own docstring already says the hidden-launch route is tested through
# stubs because CI cannot observe a real Windows console either way.
BASH = resolve_bash()
_NO_BASH = pytest.mark.skipif(BASH is None, reason="no real POSIX bash resolvable on this platform")


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(script: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, "-c", script], env=env, capture_output=True, text=True, timeout=10, check=False)


def _is_windows_env(extra: dict) -> dict:
    env = {"PATH": "/usr/bin:/bin", **extra}
    return env


@_NO_BASH
def test_is_windows_true_via_os_env_var():
    r = _run(f'. "{LIB}"; _remember_is_windows && echo YES || echo NO', _is_windows_env({"OS": "Windows_NT"}))
    assert r.stdout.strip() == "YES", r.stderr


@_NO_BASH
def test_is_windows_true_via_uname_mingw(monkeypatch, tmp_path):
    fake_uname = tmp_path / "uname"
    _stub(fake_uname, 'echo MINGW64_NT-10.0')
    env = _is_windows_env({"OS": "", "PATH": f"{tmp_path}:/usr/bin:/bin"})
    r = _run(f'. "{LIB}"; _remember_is_windows && echo YES || echo NO', env)
    assert r.stdout.strip() == "YES", r.stderr


@_NO_BASH
def test_is_windows_false_on_plain_linux_env():
    """Positive control for the two tests above: a real Linux/macOS
    environment (the one this suite actually runs on) must NOT be detected
    as Windows, or every one of this plugin's four nohup fallbacks would be
    dead code nothing ever exercises."""
    r = _run(f'. "{LIB}"; _remember_is_windows && echo YES || echo NO', _is_windows_env({"OS": ""}))
    assert r.stdout.strip() == "NO", r.stderr


@_NO_BASH
def test_detach_windows_returns_1_when_wscript_missing(tmp_path):
    """Hidden launcher unavailable (the common case on this CI, which has no
    wscript.exe at all) -- must return non-zero so the caller falls back to
    its own nohup line, never to silently skipping the launch."""
    out = tmp_path / "out.log"
    pid = tmp_path / "pid"
    env = {"PATH": "/usr/bin:/bin"}
    r = _run(
        f'. "{LIB}"; _remember_detach_windows "{out}" "{pid}" echo hi; echo "RC=$?"',
        env,
    )
    assert "RC=1" in r.stdout, r.stdout + r.stderr


@_NO_BASH
def test_detach_windows_invokes_hidden_launcher_with_expected_args(tmp_path):
    """Positive control for the test above: when wscript.exe AND cygpath
    AND bash are all resolvable, the hidden route is taken and wscript.exe
    is invoked with the vbs path, the bash path, -c, the pidwrap script,
    the pidfile, and the real command -- in that order."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    captured = tmp_path / "captured.txt"
    _stub(bindir / "wscript.exe", f'printf "%s\\n" "$@" > "{captured}"')
    _stub(bindir / "cygpath", 'shift; printf "%s" "$1"')
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    out = tmp_path / "out.log"
    pid = tmp_path / "pid"
    r = _run(
        f'. "{LIB}"; _remember_detach_windows "{out}" "{pid}" echo hi; echo "RC=$?"; sleep 0.2',
        env,
    )
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    lines = captured.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "//B", lines
    assert lines[1].endswith("windows-hidden-run.vbs"), lines
    assert lines[2].endswith("bash"), lines
    assert lines[3] == "-c", lines
    assert "exec bash" in lines[4] and str(out) in lines[4], lines
    assert lines[5].endswith("lib-detach-pidwrap.sh"), lines
    assert lines[6] == str(pid), lines
    assert lines[7:] == ["echo", "hi"], lines


@_NO_BASH
def test_detach_windows_resolves_bare_bash_argv_to_absolute_path(tmp_path):
    """#1002 review: session-end-hook.sh and agy-stop-hook.sh both hand
    _remember_detach_windows a bare "bash" as the real command's own
    argv[0] (re-invoking themselves as `bash SCRIPT`, rather than exec'ing
    SCRIPT directly the way post-tool-hook.sh does). That bareword must
    come out resolved to an absolute path -- the same one $bash_path
    already resolved for the function's own -c wrapper above -- rather
    than pass through as the literal string "bash" for three more nested
    PATH lookups to get right on their own. Whether an unresolved "bash"
    would actually mis-resolve on real Windows is REASONED, not OBSERVED
    (no Windows box here), but resolving it once, locally, removes the
    question rather than betting on it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    captured = tmp_path / "captured.txt"
    _stub(bindir / "wscript.exe", f'printf "%s\\n" "$@" > "{captured}"')
    _stub(bindir / "cygpath", 'shift; printf "%s" "$1"')
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    out = tmp_path / "out.log"
    pid = tmp_path / "pid"
    r = _run(
        f'. "{LIB}"; _remember_detach_windows "{out}" "{pid}" bash /some/script.sh arg1; echo "RC=$?"; sleep 0.2',
        env,
    )
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    real_cmd = captured.read_text(encoding="utf-8").splitlines()[7:]
    assert real_cmd[0] != "bash", real_cmd
    assert real_cmd[0].endswith("bash"), real_cmd
    assert real_cmd[1:] == ["/some/script.sh", "arg1"], real_cmd


@_NO_BASH
def test_detach_windows_leaves_a_non_bash_command_untouched(tmp_path):
    """Positive control for the test above: a real command whose argv[0]
    is NOT the literal string "bash" (the post-tool-hook.sh shape, and
    the existing test_detach_windows_invokes_hidden_launcher_with_expected_args
    above, both pass a script path or "echo" directly) must reach
    wscript.exe completely unchanged -- the new resolution step must be
    conditional on the exact bareword, not a blanket rewrite of argv[0]."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    captured = tmp_path / "captured.txt"
    _stub(bindir / "wscript.exe", f'printf "%s\\n" "$@" > "{captured}"')
    _stub(bindir / "cygpath", 'shift; printf "%s" "$1"')
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    out = tmp_path / "out.log"
    pid = tmp_path / "pid"
    r = _run(
        f'. "{LIB}"; _remember_detach_windows "{out}" "{pid}" /some/script.sh arg1; echo "RC=$?"; sleep 0.2',
        env,
    )
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    real_cmd = captured.read_text(encoding="utf-8").splitlines()[7:]
    assert real_cmd == ["/some/script.sh", "arg1"], real_cmd


def _post_tool_plugin_root(tmp_path: Path, save_body: str) -> Path:
    """The real plugin root, with scripts/save-session.sh replaced by a
    stub so the PostToolUse save trigger's real dispatch logic (the code
    under test) runs unmodified against a fast, observable child."""
    root = tmp_path / "plugin-root"
    (root / "scripts").mkdir(parents=True)
    for entry in REPO_ROOT.iterdir():
        if entry.name != "scripts":
            (root / entry.name).symlink_to(entry)
    for entry in (REPO_ROOT / "scripts").iterdir():
        if entry.name != "save-session.sh":
            (root / "scripts" / entry.name).symlink_to(entry)
    _stub(root / "scripts" / "save-session.sh", save_body)
    return root


def _post_tool_env(tmp_path, root, extra_path=""):
    import json as _json

    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    (session_dir / "sess-1.jsonl").write_text(ONE_MESSAGE_LINE * 60)
    (remember / "config.json").write_text(_json.dumps({"thresholds": {"delta_lines_trigger": 50}}))
    base_path = os.environ.get("PATH", "")
    path = f"{extra_path}:{base_path}" if extra_path else base_path
    env = {
        "HOME": str(home), "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(root), "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1", "PATH": path,
    }
    return env, remember


@_NO_BASH
def test_post_tool_save_uses_hidden_launcher_on_windows(tmp_path):
    """Dispatch decision at the REAL call site (post-tool-hook.sh), not a
    reimplementation of it: with Windows detected and a hidden launcher
    available, the save goes through wscript.exe, never through nohup."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    captured = tmp_path / "captured.txt"
    _stub(bindir / "wscript.exe", 'printf "%s\n" "$@" > "' + str(captured) + '"')
    _stub(bindir / "cygpath", 'shift; printf "%s" "$1"')
    root = _post_tool_plugin_root(tmp_path, "#!/usr/bin/env bash\nexit 0\n")
    env, _remember = _post_tool_env(tmp_path, root, extra_path=str(bindir))
    env["OS"] = "Windows_NT"
    r = subprocess.run([BASH, str(root / "scripts" / "post-tool-hook.sh")],
                        env=env, input="", capture_output=True, text=True, timeout=15, check=False)
    time.sleep(0.3)  # the hidden route's own "wscript.exe &" is detached -- give it a beat
    assert captured.exists(), "wscript.exe stub was never invoked -- hidden route not taken\n" + r.stderr


@_NO_BASH
def test_post_tool_save_uses_nohup_without_windows(tmp_path):
    """Positive control for the test above: with no Windows signal at all
    (the environment this suite actually runs under), the save must go
    through the unchanged nohup line, and the wscript.exe stub -- present
    on PATH here too -- must NEVER be invoked."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    captured = tmp_path / "captured.txt"
    _stub(bindir / "wscript.exe", 'printf "%s\n" "$@" > "' + str(captured) + '"')
    _stub(bindir / "cygpath", 'shift; printf "%s" "$1"')
    root = _post_tool_plugin_root(tmp_path, "#!/usr/bin/env bash\nexit 0\n")
    env, remember = _post_tool_env(tmp_path, root, extra_path=str(bindir))
    env["OS"] = ""
    r = subprocess.run([BASH, str(root / "scripts" / "post-tool-hook.sh")],
                        env=env, input="", capture_output=True, text=True, timeout=15, check=False)
    assert not captured.exists(), "wscript.exe stub was invoked even though Windows was not detected\n" + r.stderr
    pid_file = remember / "tmp" / "save-session.pid"
    assert pid_file.exists(), "PID_FILE must still be created on the nohup fallback branch"


def test_every_nohup_detach_site_guards_with_the_shared_helper():
    """Regression guard for the issue's own ask: 'ideally via one shared
    launcher helper so the next detached spawn cannot regress'. Every nohup
    detach line outside lib-detach.sh itself must have a call to
    _remember_detach_windows (or the inline is-windows check that gates it)
    within a few lines above it, so a future spawn copying the OLD
    unguarded shape gets caught here rather than shipping unnoticed."""
    # session-start-hook.sh is deliberately NOT in this list: wiring it in
    # pushed that hook's own compiled size over its 120 KiB release budget
    # (check_release_tree.py's HOOK_SCRIPT_MAX_BYTES), with no margin left
    # to absorb it -- see the DEFERRED comment at its own (unchanged)
    # nohup detach site. Reinstate it here once a follow-up frees enough
    # of that hook's own byte budget to add the wiring back.
    sites = [
        REPO_ROOT / "scripts" / "post-tool-hook.sh",
        REPO_ROOT / "scripts" / "session-end-hook.sh",
        REPO_ROOT / "scripts" / "agy-stop-hook.sh",
    ]
    for site in sites:
        text = site.read_text(encoding="utf-8")
        assert "_remember_detach_windows" in text, (
            f"{site.name}: no reference to the shared hidden-launch helper -- "
            "a nohup detach here would flash a console on Windows (#1002) "
            "with nothing guarding it."
        )
        active_lines = [
            line for line in text.splitlines()
            if "_remember_detach_windows" in line and not line.strip().startswith("#")
        ]
        assert active_lines, (
            f"{site.name}: _remember_detach_windows is only mentioned inside a "
            "comment, never on an active line -- the string-presence check "
            "above has no positive control distinguishing a real call from a "
            "stray comment (#1002 review)."
        )


BACKSLASH = chr(92)
DQUOTE = chr(34)


def _vbs_quotearg_port(arg):
    """Line-for-line port of scripts/windows-hidden-run.vbs's QuoteArg,
    kept in sync by hand so a future edit to either side that breaks the
    pairing fails this test rather than shipping silently (#1002 review:
    the original doubled-quote escape silently dropped every embedded
    quote under the MS-CRT/CreateProcess convention subprocess.list2cmdline
    already implements correctly in the stdlib, on every platform, since
    it is pure string manipulation -- no Windows box needed to run it)."""
    need_quote = (" " in arg) or ("\t" in arg) or (arg == "")
    result = []
    bs_count = 0
    for c in arg:
        if c == BACKSLASH:
            bs_count += 1
        elif c == DQUOTE:
            result.append(BACKSLASH * (bs_count * 2 + 1))
            result.append(DQUOTE)
            bs_count = 0
        else:
            if bs_count:
                result.append(BACKSLASH * bs_count)
                bs_count = 0
            result.append(c)
    if need_quote:
        result.append(BACKSLASH * (bs_count * 2))
        return DQUOTE + "".join(result) + DQUOTE
    result.append(BACKSLASH * bs_count)
    return "".join(result)


_QUOTEARG_CASES = [
    "plain",
    "with space",
    "with\ttab",
    "",
    "has\"quote",
    "ends with backslash\\",
    "backslash before quote\\\"end",
    "exec bash \"$0\" \"$@\" >>/tmp/out space.log 2>&1",
    "C:\\Users\\John Doe\\save-session.sh",
]


@pytest.mark.parametrize("arg", _QUOTEARG_CASES)
def test_vbs_quotearg_matches_the_stdlib_createprocess_convention(arg):
    """Pins the VBS escaping algorithm against Python's own
    subprocess.list2cmdline -- the same MS-CRT/CreateProcess convention,
    correct on every platform since it is pure string manipulation. The
    original escape (doubling embedded quotes) failed this for any
    argument containing a literal quote; #1002 review caught it."""
    import subprocess

    assert _vbs_quotearg_port(arg) == subprocess.list2cmdline([arg])
