"""``safe_eval`` (scripts/log.sh) is the only bridge between the Python
pipeline and the shell: every value the extract step computes --
``EXTRACT_FILE``, ``POSITION``, ``SKIP_LINES``, the counts -- crosses into
save-session.sh through it, as ``KEY=VALUE`` lines matched by

    ^([A-Z_][A-Z0-9_]*)=(.*)$

``[A-Z]`` is a POSIX bracket RANGE, and a range is matched by the locale's
COLLATION order, not by byte value. Turkish collation (tr_TR, and az_AZ the
same way) orders dotless-i and dotted-I around the Latin letters such that
``I`` does not fall inside ``A``..``Z``. Under such a locale every bridge
variable whose name contains an ``I`` is silently skipped -- ``EXTRACT_FILE``
and ``POSITION`` among them, while ``HUMAN_COUNT`` and ``EXCHANGE_COUNT``
(no ``I``) still arrive. The counts passing the "0 exchanges" gate with an
empty ``EXTRACT_FILE`` is what takes the run all the way to build-prompt and
a ``FileNotFoundError: ''`` -- no save ever completes, thousands of times
over, on a host whose only unusual property is its language. (#695)

The fix is one line, ``local LC_ALL=C``, the same guard
``_remember_cfg_flatten_cache_valid_value`` and ``config()`` already carry in
this very file for the same reason.

Observed vs reasoned: the Turkish-collation widening is NOT reproducible on
macOS's libc (``[[ "I" =~ ^[A-Z]+$ ]]`` still matches under tr_TR.UTF-8
there -- checked), it is a glibc behaviour. So on a macOS runner every leg
below skips loudly rather than passing vacuously, and the CI Linux legs are
where this is actually observed. The locale probe refuses to pick a locale
by NAME: it accepts one only after watching this machine's own bash fail to
match ``I`` under it, so a runner that cannot reproduce the bug can never
report a pass about it.
"""

import functools
import os
import subprocess
import tempfile
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="glibc locale collation under a bash subprocess -- the behaviour "
           "under test does not exist on Windows runners (#695)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECT_SCRIPT = REPO_ROOT / "scripts" / "detect-tools.sh"
LIB_SCRIPT = REPO_ROOT / "scripts" / "lib-memory-dir.sh"
LOG_SH = REPO_ROOT / "scripts" / "log.sh"

# Straight from the extract step's real output shape: two names carrying an
# "I" (the ones that actually break), one without (the control that keeps
# arriving even while the bug is live, which is precisely why the failure is
# silent rather than loud).
_BRIDGE_INPUT = "EXTRACT_FILE=/tmp/x-695.txt\nPOSITION=270\nHUMAN_COUNT=36\n"


from ._locale_probe import SKIP_REASON as _SKIP_REASON, collation_locale as _collation_locale


def _run_safe_eval(tmp_path: Path, payload: str,
                   env_extra: "dict | None" = None) -> dict:
    """Source log.sh, push `payload` through safe_eval, and report what each
    bridge variable ended up holding. Every name is printed with a sentinel
    delimiter so "assigned empty" and "never assigned" stay distinguishable
    -- the distinction the whole bug turns on. The payload goes in through a
    file rather than a heredoc so its bytes reach safe_eval untouched by the
    harness's own quoting."""
    payload_file = tmp_path / "bridge.txt"
    payload_file.write_text(payload, encoding="utf-8")
    script = f"""
    set -u
    export PIPELINE_DIR={REPO_ROOT}
    export PROJECT_DIR={tmp_path}
    source {DETECT_SCRIPT} >/dev/null 2>&1
    source {LIB_SCRIPT} >/dev/null 2>&1
    source {LOG_SH} >/dev/null 2>&1
    EXTRACT_FILE=""; POSITION=""; HUMAN_COUNT=""; lower_key=""
    safe_eval < '{payload_file}'
    printf 'EXTRACT_FILE=[%s]\\n' "$EXTRACT_FILE"
    printf 'POSITION=[%s]\\n' "$POSITION"
    printf 'HUMAN_COUNT=[%s]\\n' "$HUMAN_COUNT"
    printf 'lower_key=[%s]\\n' "$lower_key"
    printf 'LC_ALL_AFTER=[%s]\\n' "${{LC_ALL:-}}"
    """
    env = {**os.environ, **(env_extra or {})}
    result = subprocess.run(["bash", "-c", script], env=env, check=False,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (
        f"safe_eval harness failed (exit {result.returncode}):\n{result.stderr}")
    parsed = {}
    for line in result.stdout.splitlines():
        if "=[" in line and line.endswith("]"):
            key, _, rest = line.partition("=[")
            parsed[key] = rest[:-1]
    return parsed


class TestSafeEvalUnderTurkishCollation:

    def test_names_containing_i_still_assigned(self, tmp_path):
        """The bug, stated directly: under a locale whose collation puts "I"
        outside [A-Z], EXTRACT_FILE and POSITION must still cross the
        bridge."""
        found = _collation_locale()
        if found is None:
            pytest.skip(_SKIP_REASON)
        name, overlay = found
        got = _run_safe_eval(tmp_path, _BRIDGE_INPUT,
                             env_extra={**overlay, "LC_ALL": name,
                                        "LANG": name})
        assert got["EXTRACT_FILE"] == "/tmp/x-695.txt", (
            f"safe_eval dropped EXTRACT_FILE under {name!r}: its "
            f"^[A-Z_][A-Z0-9_]*= range is being matched by that locale's "
            f"collation, which does not place 'I' inside A-Z. Downstream "
            f"this reaches build-prompt with an empty path and no save "
            f"ever completes (#695). Fix: `local LC_ALL=C` in safe_eval."
        )
        assert got["POSITION"] == "270", (
            f"safe_eval dropped POSITION under {name!r} -- the same "
            f"collation range; downstream this is int('') (#695)"
        )

    def test_positive_control_name_without_i_arrives(self, tmp_path):
        """Paired positive control. HUMAN_COUNT has no "I" and arrives even
        with the bug live -- so if it were ALSO missing here, the harness
        itself is broken and the assertions above would be proving nothing
        about the locale."""
        found = _collation_locale()
        if found is None:
            pytest.skip(_SKIP_REASON)
        name, overlay = found
        got = _run_safe_eval(tmp_path, _BRIDGE_INPUT,
                             env_extra={**overlay, "LC_ALL": name,
                                        "LANG": name})
        assert got["HUMAN_COUNT"] == "36", (
            "HUMAN_COUNT contains no 'I' and crosses the bridge even with "
            "#695 unfixed -- losing it means the harness never ran "
            "safe_eval at all, not that the locale did anything"
        )

    def test_lc_all_does_not_leak_to_caller(self, tmp_path):
        """`local LC_ALL=C` is scoped to the function. A caller that ran
        under its own locale must still be under it afterwards -- log
        timestamps and every later `[[ =~ ]]` in save-session.sh read the
        same variable."""
        found = _collation_locale()
        if found is None:
            pytest.skip(_SKIP_REASON)
        name, overlay = found
        got = _run_safe_eval(tmp_path, _BRIDGE_INPUT,
                             env_extra={**overlay, "LC_ALL": name,
                                        "LANG": name})
        assert got["LC_ALL_AFTER"] == name, (
            f"safe_eval left LC_ALL as {got['LC_ALL_AFTER']!r} instead of "
            f"restoring the caller's {name!r} -- the C locale must not "
            f"outlive the function"
        )


class TestSafeEvalStillRejectsNonAssignments:
    """The C locale must not be bought by widening what safe_eval accepts.
    These run in the default locale (where they have always held) so a
    regression shows up on every runner, not only the ones that can
    reproduce #695."""

    def test_lowercase_key_still_rejected(self, tmp_path):
        got = _run_safe_eval(tmp_path, "lower_key=oops\nHUMAN_COUNT=36\n")
        assert got["lower_key"] == "", (
            "safe_eval assigned a lowercase name -- the allowlist is "
            "supposed to be upper-case only"
        )
        assert got["HUMAN_COUNT"] == "36", (
            "positive control: a legitimate assignment on the same input "
            "must still be applied, or the rejection above proves nothing"
        )

    def test_command_line_is_not_executed(self, tmp_path):
        marker = tmp_path / "should-not-exist"
        got = _run_safe_eval(tmp_path,
                             f"touch {marker}\nHUMAN_COUNT=36\n")
        assert not marker.exists(), (
            "safe_eval executed a non-assignment line from the pipeline's "
            "stdout"
        )
        assert got["HUMAN_COUNT"] == "36", (
            "positive control: the assignment on the next line must still "
            "have been applied"
        )
