"""#860: /remember:doctor must surface a deprecated-credential-path notice.

pipeline/haiku.py now logs a "DEPRECATED:" line to the daily log every time
the legacy REMEMBER_OAUTH_TOKEN env var or the haiku.oauth_token config.json
key is actually used to authenticate the nested `claude -p` -- the plugin's
own userConfig `oauth_token` option is the preferred path now (see
plugin.json, pipeline/haiku.py's _configured_oauth_token()). This pins that
doctor.sh surfaces that line rather than only logging it where nobody looks,
and the positive/negative-control pair that makes the check meaningful: a
healthy install with no deprecated usage must NOT show the notice.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.test_doctor_session_end_370 import _backdate, _project, _run


def _healthy_baseline(tmp_path):
    home, project, remember, session_dir = _project(tmp_path)
    for name in ("aaaa-old-a.jsonl", "bbbb-old-b.jsonl", "cccc-old-c.jsonl"):
        f = session_dir / name
        f.write_text("{}\n", encoding="utf-8")
        _backdate(f, 3 * 24 * 3600)
    (remember / "tmp" / "capture-alive").write_text("sess-1", encoding="utf-8")
    (remember / "tmp" / "last-save.json").write_text(
        '{"session": "sess-1", "line": 500}', encoding="utf-8")
    return home, project, remember, session_dir


def test_positive_control_no_deprecated_usage_shows_no_notice(tmp_path):
    """Must-not-fire twin: a healthy install that never used the deprecated
    fallback must not be told it did."""
    home, project, remember, _session_dir = _healthy_baseline(tmp_path)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "DEPRECATED" not in result.stdout


def test_no_log_at_all_is_distinguished_from_logs_with_no_deprecated_line(tmp_path):
    """Review finding (auditor): the new section must not collapse "no daily
    log exists yet" (fresh install, or logging unwritable) and "logs exist,
    were genuinely scanned, and no DEPRECATED line was ever written" into the
    same "OK" sentence -- the sibling SessionStart-duration section three
    blocks above already keeps these two cases apart for exactly this reason,
    and the new section must follow the same convention."""
    home, project, remember, _session_dir = _healthy_baseline(tmp_path)
    no_log_result = _run(home, project, remember)

    import shutil
    remember2 = remember.parent / "remember2"
    shutil.copytree(remember, remember2)
    (remember2 / "logs").mkdir(parents=True, exist_ok=True)
    (remember2 / "logs" / "memory-2026-10-03.log").write_text(
        "09:00:00 [haiku] everything fine, nothing deprecated here\n",
        encoding="utf-8",
    )
    clean_log_result = _run(home, project, remember2)

    def _verdict_line(stdout: str) -> str:
        return next(
            line for line in stdout.splitlines()
            if "recovery-token config" in line.lower()
            and not line.rstrip().endswith("--"))

    no_log_line = _verdict_line(no_log_result.stdout)
    clean_log_line = _verdict_line(clean_log_result.stdout)
    assert no_log_line != clean_log_line, (
        "a fresh install with no daily log at all must not print the exact "
        "same line as an install whose logs were genuinely scanned and "
        "found clean:\n  no-log:    " + no_log_line
        + "\n  logs-clean: " + clean_log_line
    )


def test_deprecated_remember_oauth_token_usage_is_surfaced(tmp_path):
    """Must-fire: once pipeline/haiku.py has logged a DEPRECATED line for
    REMEMBER_OAUTH_TOKEN, doctor.sh must surface it rather than leave it
    buried in a daily log nobody reads."""
    home, project, remember, _session_dir = _healthy_baseline(tmp_path)
    logs = remember / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "memory-2026-10-03.log").write_text(
        "09:15:02 [haiku] DEPRECATED: REMEMBER_OAUTH_TOKEN is still read as "
        "a fallback but will be removed in a future release -- configure "
        "the recovery token through the plugin's userConfig option "
        "instead; see #860\n",
        encoding="utf-8",
    )

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "DEPRECATED" in result.stdout and "REMEMBER_OAUTH_TOKEN" in result.stdout, (
        "a logged deprecation line did not reach doctor.sh's output:\n"
        + result.stdout
    )
    assert "userConfig" in result.stdout, (
        "the doctor notice must point at the replacement, not just name "
        "what is deprecated:\n" + result.stdout
    )
