"""doctor.sh's store-size check must ask config.timezone, not the machine clock (#357).

The pipeline reaches "today" through config.timezone -> REMEMBER_TZ
(scripts/log.sh:366) -> pipeline/_tz.py's today_str(), which is what
_eligible_staging (pipeline/shell.py:393) excludes today by. doctor.sh
deliberately does not source log.sh, and until this fix REMEMBER_TZ appeared
nowhere in it -- TODAY was always the bare machine clock's date.

With a configured timezone ahead of the machine's, that diverges in BOTH
directions at once: doctor excludes the file the pipeline counts and counts
the file the pipeline excludes. When the counted-but-excluded file is the
larger of the two, staging can cross the cap on a store the pipeline is
about to rotate happily -- the "cannot heal itself" alarm on a store that is
fine.

A live-clock test cannot pin this deterministically without flaking at the
day boundary, so both tests here shim `date` on PATH: the shim looks at
whether TZ was set in its own environment (which is exactly what
`TZ="$tz" date ...` vs. plain `date ...` controls) and returns a fixed date
either way. That is the same PATH-shim technique
tests/test_ndc_day_boundary.py uses to move the clock deterministically.
"""

from __future__ import annotations

import sys

import pytest

from tests.test_doctor_oversized_store_348 import CAP, _fill, _project, _run

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)

# What the shim answers when doctor.sh asks +%Y-%m-%d with TZ set to
# EXACTLY the configured timezone (the fixed-code path, where doctor.sh
# itself constructs `TZ="$_doctor_tz" date ...`) versus anything else,
# including empty/unset AND an ambient TZ the test process happened to
# inherit. Checking for mere presence of $TZ, rather than its exact value,
# would make this test pass against unfixed doctor.sh in any environment
# that already exports TZ (some CI base images do) -- unfixed doctor.sh
# never sets TZ itself, but a pre-set ambient TZ would still be inherited
# by the shim subprocess and satisfy a presence-only check.
_CONFIGURED_TZ = "Pacific/Kiritimati"
_TZ_TODAY = "2099-01-02"
_NO_TZ_TODAY = "2099-01-01"


def _shim_date(bindir):
    # A PATH shim only intercepts a real `date` process -- see
    # tests/test_prompt_hook_spawns.py's own guard on this. doctor.sh calls
    # `date` directly rather than through lib-clock.sh's spawn-free
    # `printf '%(FMT)T'` builtin, so this shim is not subject to that seam
    # and REMEMBER_NO_PRINTF_T is not needed here -- named anyway so the
    # guard's file-level detector (which cannot tell the two shapes apart)
    # finds the acknowledgement it is looking for.
    bindir.mkdir(exist_ok=True)
    shim = bindir / "date"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "+%Y-%m-%d" ]; then\n'
        f'  if [ "$TZ" = "{_CONFIGURED_TZ}" ]; then\n'
        f'    echo {_TZ_TODAY}\n'
        "  else\n"
        f'    echo {_NO_TZ_TODAY}\n'
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'exec /bin/date "$@"\n'
    )
    shim.chmod(0o755)
    return shim


def test_a_staging_file_matching_the_configured_timezones_today_is_excluded(tmp_path):
    """The file the pipeline would also exclude must not be counted here.

    A configured timezone one day ahead of the machine's own turns
    "2099-01-02" into today. Before the fix, doctor.sh never looked at
    config.timezone and asked the bare machine clock, which this shim
    answers with 2099-01-01 -- so the 2099-01-02 staging file used to be
    counted as a past day and could invent a false "too large to
    consolidate" alarm on an otherwise healthy store.
    """
    home, project, remember = _project(tmp_path)
    bindir = tmp_path / "bin"
    _shim_date(bindir)

    cfg = tmp_path / "merged-config.json"
    cfg.write_text('{"timezone": "Pacific/Kiritimati"}', encoding="utf-8")
    _fill(remember / f"today-{_TZ_TODAY}.md", CAP + 1000)

    env_extra = {
        "REMEMBER_CONFIG": str(cfg),
        "PATH": f"{bindir}:{__import__('os').environ['PATH']}",
    }
    result = _run(home, project, remember, env_extra)

    assert result.returncode == 0, result.stderr
    assert "too large to consolidate" not in result.stdout.lower(), (
        "today's staging file, per the CONFIGURED timezone, was counted "
        "against the cap and invented an alarm on a healthy store:\n"
        + result.stdout
    )


def test_a_staging_file_not_matching_the_configured_timezones_today_is_counted(tmp_path):
    """Positive control, same fixture: yesterday (per config tz) must still count.

    Without this, a fix that excluded every staging file unconditionally
    would satisfy the test above and quietly stop counting real past-day
    staging at all.
    """
    home, project, remember = _project(tmp_path)
    bindir = tmp_path / "bin"
    _shim_date(bindir)

    cfg = tmp_path / "merged-config.json"
    cfg.write_text('{"timezone": "Pacific/Kiritimati"}', encoding="utf-8")
    _fill(remember / f"today-{_NO_TZ_TODAY}.md", CAP + 1000)

    env_extra = {
        "REMEMBER_CONFIG": str(cfg),
        "PATH": f"{bindir}:{__import__('os').environ['PATH']}",
    }
    result = _run(home, project, remember, env_extra)

    assert "too large to consolidate" in result.stdout.lower(), (
        "a staging file for a day that is NOT today, per the configured "
        "timezone, was excluded from the cap -- the check may be excluding "
        "every staging file rather than reading the configured zone:\n"
        + result.stdout
    )
