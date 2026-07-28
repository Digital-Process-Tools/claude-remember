"""NDC compression must not erase entries written while it was running (#142).

`save-session.sh` snapshots `now.md`, hands it to a Haiku call that may take up
to 180s, and then truncated the file with `: >`. By that point the parent has
released the save lock and exited, so a *newer* save can legitimately have
appended an entry — and `: >` erased it. The position for that entry had already
been advanced, so it was unrecoverable, and nothing was logged.

The fix drops exactly the bytes that were compressed (everything up to the
snapshot offset) and keeps the tail.
"""

import json
import time
from pathlib import Path

import pytest

from .test_save_session_gates import _make_env, _run  # shared harness
from .subprocess_helpers import subprocess_failure_detail

pytestmark = pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

APPENDED = "\n## 11:30 | main\n\n- an entry written while NDC was compressing\n"


def _wait_for_background_ndc(memory_file: Path, timeout: float = 30) -> str:
    """The NDC subshell is disowned, so poll until now.md stops changing."""
    deadline = time.monotonic() + timeout
    last = None
    stable_since = None
    while time.monotonic() < deadline:
        current = memory_file.read_text() if memory_file.exists() else ""
        if current == last:
            if stable_since and time.monotonic() - stable_since > 1.0:
                return current
            stable_since = stable_since or time.monotonic()
        else:
            last, stable_since = current, None
        time.sleep(0.2)
    raise TimeoutError("now.md never settled — NDC subshell still running?")


def _ndc_env(tmp_path: Path, **kwargs):
    """Harness env with NDC enabled and a real (non-SKIP) entry from the summarizer."""
    env, project, plugin, calls, sid = _make_env(
        tmp_path, exchanges=6, humans=5, **kwargs
    )
    env["STUB_HAIKU_SKIP"] = "0"
    # The shared harness parks NDC behind a long cooldown; this suite needs it to run.
    for cfg_path in (Path(env["REMEMBER_CONFIG"]), plugin / "config.json"):
        cfg = json.loads(cfg_path.read_text())
        cfg["cooldowns"]["ndc_seconds"] = 0
        cfg_path.write_text(json.dumps(cfg))
    return env, project, plugin, calls, sid


class TestNdcTruncateRace:

    def test_entry_appended_during_compression_survives(self, tmp_path):
        """The #142 window: an entry written mid-compression must not be erased."""
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        env["STUB_APPEND_DURING_NDC"] = APPENDED
        memory_file = project / ".remember" / "now.md"

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

        remaining = _wait_for_background_ndc(memory_file)
        assert "an entry written while NDC was compressing" in remaining, (
            "now.md was truncated wholesale, erasing an entry appended after the "
            "snapshot — its position was already advanced, so it is unrecoverable"
        )

    def test_compressed_content_is_still_removed(self, tmp_path):
        """The fix must not turn into a no-op: compressed bytes still go away."""
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        memory_file = project / ".remember" / "now.md"

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

        remaining = _wait_for_background_ndc(memory_file)
        assert "did some work" not in remaining, (
            "the span that was compressed into today-*.md must be dropped from now.md"
        )
        today = list((project / ".remember").glob("today-*.md"))
        assert today, "compression should have produced a today-*.md"
        assert "compressed summary" in today[0].read_text()
