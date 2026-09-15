"""save-session.sh must fail CLOSED when the extract bridge loses a variable (#695).

#695's root cause is `safe_eval`'s locale-sensitive `[A-Z]` range, fixed in
scripts/log.sh. This module is about the second half of the same report: what
happened downstream once the bridge had lost `EXTRACT_FILE`.

Nothing noticed. `EXCHANGE_COUNT` (no "I" in its name) still arrived, so the
"0 exchanges" gate passed, and the run walked all the way to `build-prompt`
with an empty path -- where Python, not the shell, finally said something:

    FileNotFoundError: [Errno 2] No such file or directory: ''

5,071 times in one reporter's hook-errors.log. A stack trace from three layers
down, naming neither the bridge nor the locale, is the worst possible place
for this to surface: the save is already past its cooldown write, and the line
that would have named the real defect was never printed because nothing was
looking.

So the guard here is deliberately NOT a second fix for the locale bug -- it is
the statement that the bridge is load-bearing. Any future reason `EXTRACT_FILE`
comes back empty (a pipeline change, a truncated stdout, a Python crash whose
partial output still parses) must stop the run at the bridge and say so, rather
than being discovered as a traceback from `open('')`.

The dropped variable is reproduced by removing the line that prints it from the
stub pipeline -- which is exactly what tr_TR did to the real one: the name is
never assigned at all. That makes these legs platform-independent, unlike
tests/test_safe_eval_locale_695.py, whose collation legs can only be observed
on glibc.
"""

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

from .test_save_session_gates import _make_env, _run, _suppress_ndc

_EXTRACT_FILE_LINE = '    print(f"EXTRACT_FILE={path}")\n'


def _drop_extract_file(plugin: Path) -> None:
    """Make the stub pipeline print every extract variable EXCEPT
    EXTRACT_FILE -- the state tr_TR's collation produced for real."""
    shell = plugin / "pipeline" / "shell.py"
    text = shell.read_text(encoding="utf-8")
    assert _EXTRACT_FILE_LINE in text, (
        "the stub pipeline no longer prints EXTRACT_FILE on the line this "
        "test removes -- tests/test_save_session_gates.py's STUB_SHELL has "
        "changed shape and this extraction is stale"
    )
    shell.write_text(text.replace(_EXTRACT_FILE_LINE, ""), encoding="utf-8")


def _calls(env: dict) -> str:
    log = Path(env["STUB_CALLS_LOG"])
    return log.read_text(encoding="utf-8") if log.is_file() else ""


def _hook_errors(project: Path) -> str:
    log = project / ".remember" / "logs" / "hook-errors.log"
    return log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""


class TestExtractBridgeFailsClosed:

    def test_missing_extract_file_stops_before_build_prompt(self, tmp_path):
        """MUST NOT happen: build-prompt called with an empty path."""
        env, project, plugin, calls, session_id = _make_env(
            tmp_path, exchanges=40, humans=5)
        _suppress_ndc(project)
        _drop_extract_file(plugin)

        result = _run(plugin, env, session_id)

        assert "build-prompt" not in _calls(env), (
            "save-session.sh reached build-prompt with no EXTRACT_FILE -- "
            "the run continues into Python and dies there on "
            "FileNotFoundError: '' instead of stopping at the bridge (#695)"
        )
        assert result.returncode != 0, (
            "the save exited 0 having produced nothing -- a silent failure "
            "is indistinguishable from a quiet session, which is the whole "
            "defect class #443 and #695 are about"
        )

    def test_missing_extract_file_is_reported_by_name(self, tmp_path):
        """A stop with no explanation is the same silence one layer up. The
        report must name EXTRACT_FILE, so the next person reads "the bridge
        lost a variable" rather than guessing at a Python path bug."""
        env, project, plugin, calls, session_id = _make_env(
            tmp_path, exchanges=40, humans=5)
        _suppress_ndc(project)
        _drop_extract_file(plugin)

        _run(plugin, env, session_id)

        errors = _hook_errors(project)
        assert "EXTRACT_FILE" in errors, (
            f"nothing in hook-errors.log names EXTRACT_FILE after the "
            f"bridge dropped it; log holds:\n{errors!r}"
        )

    def test_positive_control_intact_bridge_still_reaches_build_prompt(self, tmp_path):
        """The must-fire half. With the stub left alone, the same save must
        run all the way through -- otherwise the two assertions above would
        hold for a save that never does anything at all, on any input."""
        env, project, plugin, calls, session_id = _make_env(
            tmp_path, exchanges=40, humans=5)
        _suppress_ndc(project)

        result = _run(plugin, env, session_id)

        assert "build-prompt" in _calls(env), (
            f"the unmodified stub did not reach build-prompt either -- the "
            f"guard assertions above prove nothing about the missing "
            f"variable. stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "EXTRACT_FILE" not in _hook_errors(project), (
            "a healthy save reported the bridge error -- the guard fires on "
            "input it should not, and the assertion above would be "
            "meaningless"
        )
