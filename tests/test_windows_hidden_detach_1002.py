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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.slug import session_dir_slug as _slug

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "lib-detach.sh"
ONE_MESSAGE_LINE = "{\"type\":\"assistant\",\"message\":{\"content\":\"x\"}}\n"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(script: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=10, check=False)


def _is_windows_env(extra: dict) -> dict:
    env = {"PATH": "/usr/bin:/bin", **extra}
    return env


def test_is_windows_true_via_os_env_var():
    r = _run(f'. "{LIB}"; _remember_is_windows && echo YES || echo NO', _is_windows_env({"OS": "Windows_NT"}))
    assert r.stdout.strip() == "YES", r.stderr


def test_is_windows_true_via_uname_mingw(monkeypatch, tmp_path):
    fake_uname = tmp_path / "uname"
    _stub(fake_uname, 'echo MINGW64_NT-10.0')
    env = _is_windows_env({"OS": "", "PATH": f"{tmp_path}:/usr/bin:/bin"})
    r = _run(f'. "{LIB}"; _remember_is_windows && echo YES || echo NO', env)
    assert r.stdout.strip() == "YES", r.stderr


def test_is_windows_false_on_plain_linux_env():
    """Positive control for the two tests above: a real Linux/macOS
    environment (the one this suite actually runs on) must NOT be detected
    as Windows, or every one of this plugin's four nohup fallbacks would be
    dead code nothing ever exercises."""
    r = _run(f'. "{LIB}"; _remember_is_windows && echo YES || echo NO', _is_windows_env({"OS": ""}))
    assert r.stdout.strip() == "NO", r.stderr


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
    r = subprocess.run(["bash", str(root / "scripts" / "post-tool-hook.sh")],
                        env=env, input="", capture_output=True, text=True, timeout=15, check=False)
    time.sleep(0.3)  # the hidden route's own "wscript.exe &" is detached -- give it a beat
    assert captured.exists(), "wscript.exe stub was never invoked -- hidden route not taken\n" + r.stderr


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
    r = subprocess.run(["bash", str(root / "scripts" / "post-tool-hook.sh")],
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
    sites = [
        REPO_ROOT / "scripts" / "post-tool-hook.sh",
        REPO_ROOT / "scripts" / "session-end-hook.sh",
        REPO_ROOT / "scripts" / "session-start-hook.sh",
        REPO_ROOT / "scripts" / "agy-stop-hook.sh",
    ]
    for site in sites:
        text = site.read_text(encoding="utf-8")
        assert "_remember_detach_windows" in text, (
            f"{site.name}: no reference to the shared hidden-launch helper -- "
            "a nohup detach here would flash a console on Windows (#1002) "
            "with nothing guarding it."
        )
