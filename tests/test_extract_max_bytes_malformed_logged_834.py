"""thresholds.extract_max_bytes lacked #816's malformed-value guard (#834).

Same defect class as consolidate_max_bytes (this issue's other site) and the
already-fixed consolidate_timeout_seconds/ndc_timeout_seconds pair: a typo'd
`thresholds.extract_max_bytes` in save-session.sh silently substituted the
300000 default with nothing logged, so an operator sees exactly the same
behaviour as a deliberate default.

Reuses test_save_session_gates's `_make_env`/`_run` harness -- the same
stubbed pipeline.shell every other save-session.sh gate test drives.
"""

from __future__ import annotations

import sys

import pytest

from .test_save_session_gates import _make_env, _memory_log_text, _run

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout -- not portable to Windows runners (#79)",
)


def _build_prompt_max_bytes_arg(calls_log) -> str:
    """argv: build-prompt <extract> <last_entry> <time> <branch> <out> <max_bytes>."""
    lines = [line for line in calls_log.read_text().splitlines() if line.startswith("build-prompt")]
    assert lines, f"no build-prompt invocation found in the calls log: {calls_log.read_text()!r}"
    return lines[-1].split(" ")[-1]


class TestExtractMaxBytesMalformedIsLogged:
    """#834: the fallback branch must name what it discarded, not stay silent."""

    def test_malformed_value_is_logged(self, tmp_path):
        env, project, plugin, calls, sid = _make_env(
            tmp_path, exchanges=4, humans=5, config={"extract_max_bytes": "3OOOOO"},
        )
        result = _run(plugin, env, sid)
        assert result.returncode == 0, result.stderr

        log_text = _memory_log_text(project)
        assert "3OOOOO" in log_text, (
            f"the malformed thresholds.extract_max_bytes value was discarded "
            f"with no trace of what it was: {log_text!r}"
        )
        assert "extract_max_bytes" in log_text, (
            f"the log line does not even name the config key that failed to parse: {log_text!r}"
        )
        assert _build_prompt_max_bytes_arg(calls) == "300000", (
            "a malformed value did not fall back to the 300000 default"
        )

    def test_valid_value_is_not_logged_as_malformed(self, tmp_path):
        """Positive control: a genuinely valid override must not trip the
        new warning."""
        env, project, plugin, calls, sid = _make_env(
            tmp_path, exchanges=4, humans=5, config={"extract_max_bytes": 54321},
        )
        result = _run(plugin, env, sid)
        assert result.returncode == 0, result.stderr

        log_text = _memory_log_text(project)
        assert "extract_max_bytes" not in log_text, (
            f"a valid configured cap was reported as malformed: {log_text!r}"
        )
        assert _build_prompt_max_bytes_arg(calls) == "54321"
