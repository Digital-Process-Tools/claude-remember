"""#1002: Windows Terminal flashes a visible console for ~1s because a plain
nohup-and-background launch still allocates a console on Windows regardless --
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
import shlex
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
# #1002 CI round 7: .as_posix(), not a bare Path -- every use below
# interpolates this into a bash script string via an f-string, which
# calls str() implicitly. On Windows that is backslash-separated, and
# `_remember_detach_windows`'s own `lib_dir="${BASH_SOURCE[0]%/*}"` is a
# pure string pattern-match on "/" with no filesystem call behind it: a
# backslash-only BASH_SOURCE[0] has no "/" to strip at, so it silently
# falls through to lib-detach.sh's own `pwd` fallback (this pytest
# process's cwd -- the real repo root, not any tmp dir) and every
# subsequent `$lib_dir/windows-hidden-run.vbs` lookup inside the function
# resolves to the wrong directory, failing the `[ -f "$vbs" ]` guard and
# returning 1 -- confirmed via CI diagnostics, not reasoned (#1002 CI
# round 7).
LIB = (REPO_ROOT / "scripts" / "lib-detach.sh").as_posix()
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
    # #1002 CI round 5: Path.write_text() does universal-newline
    # translation on write -- on a real Windows runner it turns every
    # "\n" into "\r\n", corrupting the shebang line to "#!/bin/sh\r".
    # MSYS/Git Bash's own exec-by-shebang lookup then either fails to
    # resolve "/bin/sh\r" as an interpreter or does not recognise the
    # file as a valid script at all, so `command -v` silently skips this
    # stub and PATH resolution falls through to whatever real binary of
    # the same name sits later on PATH -- exactly the "stub never found,
    # real one collides" symptom this suite's own stubs hit on Windows
    # CI. This repo hit the identical CRLF-via-write_text() class once
    # already in scripts/ code (#994); `newline=""` here is the same fix,
    # applied to this test file's own stub writer.
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _to_msys_path(value) -> str:
    """Convert a native Windows path (``C:\\Users\\X``) to its Git Bash/MSYS
    mount form (``/c/Users/X``) -- no embedded colon, so it can be safely
    ':'-joined with other PATH entries and re-asserted at bash runtime
    without the drive-letter-vs-separator ambiguity a raw Windows path
    creates (#1002 CI rounds 5-8: a naive ':'-join split "C:\\Users\\X"
    into "C" and "\\Users\\X" at the drive letter's own colon, silently
    dropping the entry from PATH). A value with no drive letter (already
    POSIX, or empty) passes through with backslashes normalised and
    nothing else changed."""
    s = str(value)
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        return f"/{s[0].lower()}{s[2:].replace(chr(92), '/')}"
    return s.replace(chr(92), "/")


def _run(script: str, env: dict, prepend: str = "") -> subprocess.CompletedProcess:
    # #1002 CI round 7: Git Bash/MSYS unconditionally prepends its own
    # standard directories (observed on CI: /mingw64/bin, /usr/bin, a
    # user bin dir) to PATH at process STARTUP, regardless of what PATH
    # value is supplied via the subprocess env -- confirmed by printing
    # the actual $PATH a sourced script saw on real Windows CI: the real
    # /usr/bin (with the real uname/cygpath) came ahead of a test's own
    # stub directory even though the env dict's PATH value put the stub
    # first. Re-asserting PATH via an explicit `export` INSIDE the
    # script, which runs AFTER that startup injection, is the only way a
    # test's own directory can actually win.
    #
    # #1002 CI round 8: that `export` must PREPEND onto the already-
    # correct `$PATH` bash has at that point, never REPLACE it -- a
    # wholesale replacement (round 7's own first attempt) discarded
    # /mingw64/bin, where this image's `sleep`/`tr`/etc. actually live
    # (not /usr/bin), breaking plain utility calls the function and its
    # stubs both still need. And the prepended entry must go through
    # _to_msys_path() first: a manual runtime `export PATH=...` is parsed
    # by bash using pure POSIX ':' semantics, with none of MSYS's own
    # semicolon-based startup conversion applied to it -- unlike the
    # subprocess env's own PATH value, which IS converted once at
    # startup. Confirmed on CI: round 7's os.pathsep(';')-joined value,
    # re-exported this way, was parsed as one single useless PATH
    # component by the drive letter's own colon.
    prefix = f'export PATH={shlex.quote(_to_msys_path(prepend))}:"$PATH"; ' if prepend else ""
    return subprocess.run([BASH, "-c", prefix + script], env=env, capture_output=True, text=True, timeout=10, check=False)


def _run_real_script(path: Path, env: dict, *args: str, prepend: str = "", input_text: str = "") -> subprocess.CompletedProcess:
    """Run a REAL script FILE (not an inline -c string) with the same
    post-startup PATH prepend _run() does for inline scripts, and with
    the path itself passed as .as_posix() for the same
    BASH_SOURCE[0]/backslash reason LIB above is (#1002 CI round 7)."""
    prefix = f'export PATH={shlex.quote(_to_msys_path(prepend))}:"$PATH"; ' if prepend else ""
    quoted_args = " ".join(shlex.quote(a) for a in args)
    script = f'{prefix}exec "{path.as_posix()}" {quoted_args}'.rstrip()
    return subprocess.run([BASH, "-c", script], env=env, input=input_text,
                           capture_output=True, text=True, timeout=15, check=False)


def _controlled_env(path=None, **overrides) -> dict:
    """Inherit the REAL environment (SystemRoot, TEMP, ComSpec,
    USERPROFILE, PATH, etc.) rather than replacing it wholesale --
    #1002 CI round 6 found that starting from `{}` dropped vars
    MSYS/Git Bash's own fork+exec emulation can need just to function at
    all. `path`, when given, still fully REPLACES PATH for the rare test
    that needs a real system directory (like System32, with its own real
    wscript.exe) to be genuinely absent rather than merely outranked --
    every other test controls precedence instead, via _run()'s/
    _run_real_script()'s own `prepend` argument, which preserves the
    rest of the real PATH (see their own comments for why that is a
    separate mechanism from this dict's PATH value)."""
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    env.update(overrides)
    return env


@_NO_BASH
def test_is_windows_true_via_os_env_var():
    r = _run(f'. "{LIB}"; _remember_is_windows && echo YES || echo NO', _controlled_env(OS="Windows_NT"))
    assert r.stdout.strip() == "YES", r.stderr


@_NO_BASH
def test_is_windows_true_via_uname_mingw(monkeypatch, tmp_path):
    fake_uname = tmp_path / "uname"
    _stub(fake_uname, 'echo MINGW64_NT-10.0')
    env = _controlled_env(OS="")
    r = _run(f'. "{LIB}"; _remember_is_windows && echo YES || echo NO', env, prepend=str(tmp_path))
    assert r.stdout.strip() == "YES", r.stderr


@_NO_BASH
def test_is_windows_false_on_plain_linux_env(tmp_path):
    """Positive control for the two tests above: a NON-Windows environment
    must NOT be detected as Windows, or every one of this plugin's four
    nohup fallbacks would be dead code nothing ever exercises. This stubs
    `uname` to a deterministic non-Windows answer rather than trusting the
    host's own real `uname` -- on an actual Windows CI runner, the real
    `uname` genuinely reports MINGW*/MSYS*/CYGWIN* regardless of the OS
    env var, which made this exact assertion fail on the one platform it
    exists to guard (#1002 CI round 5: the premise was wrong, not the
    product)."""
    fake_uname = tmp_path / "uname"
    _stub(fake_uname, 'echo Linux')
    env = _controlled_env(OS="")
    r = _run(f'. "{LIB}"; _remember_is_windows && echo YES || echo NO', env, prepend=str(tmp_path))
    assert r.stdout.strip() == "NO", r.stderr


@_NO_BASH
def test_detach_windows_returns_1_when_wscript_missing(tmp_path):
    """Hidden launcher unavailable (the common case on this CI, which has no
    wscript.exe at all) -- must return non-zero so the caller falls back to
    its own nohup line, never to silently skipping the launch. PATH is
    fully replaced here (via _controlled_env's `path=`), not merely
    outranked, so a real wscript.exe elsewhere on PATH cannot accidentally
    make this test's own premise false."""
    out = tmp_path / "out.log"
    pid = tmp_path / "pid"
    env = _controlled_env(path="/usr/bin:/bin")
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
    env = _controlled_env()
    out = tmp_path / "out.log"
    pid = tmp_path / "pid"
    r = _run(
        f'. "{LIB}"; _remember_detach_windows "{out}" "{pid}" echo hi; echo "RC=$?"; sleep 0.2',
        env,
        prepend=str(bindir),
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
    env = _controlled_env()
    out = tmp_path / "out.log"
    pid = tmp_path / "pid"
    r = _run(
        f'. "{LIB}"; _remember_detach_windows "{out}" "{pid}" bash /some/script.sh arg1; echo "RC=$?"; sleep 0.2',
        env,
        prepend=str(bindir),
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
    env = _controlled_env()
    out = tmp_path / "out.log"
    pid = tmp_path / "pid"
    r = _run(
        f'. "{LIB}"; _remember_detach_windows "{out}" "{pid}" /some/script.sh arg1; echo "RC=$?"; sleep 0.2',
        env,
        prepend=str(bindir),
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


def _post_tool_env(tmp_path, root):
    import json as _json

    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    (session_dir / "sess-1.jsonl").write_text(ONE_MESSAGE_LINE * 60)
    (remember / "config.json").write_text(_json.dumps({"thresholds": {"delta_lines_trigger": 50}}))
    env = _controlled_env(
        HOME=str(home), CLAUDE_PROJECT_DIR=str(project),
        CLAUDE_PLUGIN_ROOT=str(root), REMEMBER_DIR=str(remember),
        _LIB_MEMORY_DIR_LOADED="1",
    )
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
    env, _remember = _post_tool_env(tmp_path, root)
    env["OS"] = "Windows_NT"
    # #1002 CI round 5/7: _run_real_script passes the script's own path as
    # POSIX-forward-slash (see LIB's own comment above for why -- the
    # same BASH_SOURCE[0]/backslash mechanism) and prepends PATH after
    # MSYS's own startup injection (see _run's own comment).
    r = _run_real_script(root / "scripts" / "post-tool-hook.sh", env, prepend=str(bindir))
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
    # #1002 CI round 7: OS="" alone does not prove "not Windows" to
    # _remember_is_windows on a REAL Windows runner -- it falls through to
    # `uname -s`, and the real uname genuinely answers MINGW*/MSYS*/
    # CYGWIN* there regardless of OS. Without this stub, the hidden route
    # was actually being attempted (and from _remember_detach_windows's
    # own perspective, successfully dispatched -- it does not wait for or
    # verify the backgrounded wscript.exe launch), so post-tool-hook.sh
    # skipped its nohup fallback entirely: neither PID_FILE nor
    # captured.txt was ever written, both failing silently (#1002 CI
    # round 7, confirmed by CI diagnostics on the sibling
    # _remember_is_windows unit test).
    _stub(bindir / "uname", 'echo Linux')
    root = _post_tool_plugin_root(tmp_path, "#!/usr/bin/env bash\nexit 0\n")
    env, remember = _post_tool_env(tmp_path, root)
    env["OS"] = ""
    r = _run_real_script(root / "scripts" / "post-tool-hook.sh", env, prepend=str(bindir))
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
    #
    # session-end-hook.sh and agy-stop-hook.sh are ALSO deliberately not in
    # this list, as of #1002 CI round 5's own scope narrowing: those two
    # sites are reverted byte-for-byte to main and tracked in #1006
    # instead, because wscript-launching session-end-hook.sh's own
    # self-redetach loses the hook's stdin JSON payload (a wscript-launched
    # child does not inherit it the way `nohup ... &` does), a real product
    # regression caught by the pre-existing test_windows_native_hook_cwd_448.py
    # test going red on this PR's own Windows CI legs. This PR keeps the
    # hidden launcher to post-tool-hook.sh's save only -- the one site the
    # reporter actually measured flashing a console every few minutes.
    sites = [
        REPO_ROOT / "scripts" / "post-tool-hook.sh",
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
