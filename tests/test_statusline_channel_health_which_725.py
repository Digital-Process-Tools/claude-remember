"""#725 (F13/F17): `_run_channel_health` used to hand a bare `"supertool"` to
`subprocess.run`, bypassing `_safe_which` -- so a same-named
`supertool.exe`/`supertool.cmd` planted at the root of the repository this
statusline is reporting on could win over the real `PATH` entry on Windows
(the same `CreateProcess`-searches-cwd-first gap `_run`'s own docstring
already names and already guards against for `git`/`gh`).

Must-fire (a decoy on PATH is never preferred) paired with a must-not-fire
control (a real resolvable binary still gets used), per this repo's own
"pair every must-not-fire with a must-fire" convention (CLAUDE.md).

Cross-platform note: the Windows `CreateProcess`-searches-cwd-first behaviour
itself is REASONED, not observed from this machine -- this test only proves
`_safe_which` is actually consulted and its result actually used, which is
platform-independent and directly observable here.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".oss"))

import statusline


class _FakeCompletedProcess:
    def __init__(self, argv):
        self.argv = argv
        self.returncode = 0
        self.stdout = b"--- channel:health ---\nchannel: FORWARDING\n"


def test_run_channel_health_resolves_argv0_through_safe_which(monkeypatch):
    """Must-fire: `_run_channel_health` must ask `_safe_which("supertool")`
    for the resolved path and hand THAT to `subprocess.run`, never the bare
    name -- proving the resolution actually happens rather than merely
    existing unused elsewhere in the module."""
    calls = []

    def fake_safe_which(name):
        calls.append(name)
        return "/resolved/path/to/supertool"

    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return _FakeCompletedProcess(command)

    monkeypatch.setattr(statusline, "_safe_which", fake_safe_which)
    monkeypatch.setattr(statusline.subprocess, "run", fake_run)

    result = statusline._run_channel_health(timeout=5)

    assert calls == ["supertool"], calls
    assert captured["command"][0] == "/resolved/path/to/supertool", captured
    assert captured["command"][1:] == ["channel:health"]
    assert "FORWARDING" in result


def test_run_channel_health_positive_control_still_returns_text(monkeypatch):
    """Must-not-fire pairing: when resolution succeeds and the subprocess
    genuinely produces output, that output must still come back unchanged --
    proving the prior test passes because of the fix, not because nothing
    ever resolves or runs."""
    monkeypatch.setattr(
        statusline, "_safe_which", lambda name: "/usr/local/bin/supertool"
    )
    monkeypatch.setattr(
        statusline.subprocess,
        "run",
        lambda command, **kwargs: _FakeCompletedProcess(command),
    )

    result = statusline._run_channel_health(timeout=5)

    assert "channel: FORWARDING" in result


def test_run_channel_health_returns_none_when_unresolvable(monkeypatch):
    """`_safe_which` finding nothing must fold to the identical `None` a
    missing-binary `subprocess.run` exception used to produce -- the
    caller-visible contract this fix must not change."""
    monkeypatch.setattr(statusline, "_safe_which", lambda name: None)

    def fail_if_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("subprocess.run must not be called when unresolvable")

    monkeypatch.setattr(statusline.subprocess, "run", fail_if_called)

    assert statusline._run_channel_health(timeout=5) is None
