"""#686: the per_session degrade notice silently disappeared in default
(non-external) storage mode.

session-start-hook.sh sets HANDOFF_MODE_DEGRADED="true" whenever
handoff_mode is "per_session" and no usable session_id reached the hook,
regardless of storage mode. But the notice that reports it lived inside
`if [ "$REMEMBER_ROOT" != "$PROJECT_DIR" ] || [ -n "$PER_SESSION_HANDOFF" ]`
-- true in external mode (REMEMBER_ROOT != PROJECT_DIR) and true when a
session_id DID resolve (PER_SESSION_HANDOFF set), but false for the exact
case the notice exists to describe: default storage, per_session requested,
no session_id. REMEMBER_ROOT == PROJECT_DIR and PER_SESSION_HANDOFF is
empty, so the whole HANDOFF block -- hint and degrade notice together --
was skipped and the user silently got the shared remember.md with no
indication their per_session request was not honoured.

This mirrors TestMissingSessionIdDoesNotSilentlyReintroduceTheClobber in
test_handoff_per_session_363.py, but exercises default (legacy, in-project)
storage rather than external=True, since that is precisely the storage mode
those tests never exercised for the degrade notice.
"""

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX session-start hook -- not portable to Windows runners (#79)",
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_handoff_per_session_363 import _sandbox, _session_start  # noqa: E402


class TestDegradeNoticeFiresInDefaultStorageMode:

    def test_per_session_no_session_id_default_storage_says_so(self, tmp_path):
        """Must-fire: per_session requested, no session_id reaches the hook,
        DEFAULT (in-project) storage -- the notice must still print."""
        project, home, _remember_dir = _sandbox(tmp_path, handoff_mode="per_session")

        out = _session_start(project, home, None)

        assert "no session_id reached this hook" in out, (
            f"degrade notice did not fire in default storage mode\n{out}"
        )

    def test_single_mode_default_storage_no_session_id_says_nothing(self, tmp_path):
        """Must-not-fire control: handoff_mode is "single" (never degrades),
        default storage, no session_id -- no degrade notice."""
        project, home, _remember_dir = _sandbox(tmp_path, handoff_mode="single")

        out = _session_start(project, home, None)

        assert "no session_id reached this hook" not in out, (
            f"degrade notice fired for handoff_mode single\n{out}"
        )

    def test_per_session_with_session_id_default_storage_says_nothing(self, tmp_path):
        """Must-not-fire control: per_session WITH a usable session_id,
        default storage -- resolved, not degraded, so no notice."""
        project, home, _remember_dir = _sandbox(tmp_path, handoff_mode="per_session")

        out = _session_start(project, home, "sess-ddd")

        assert "no session_id reached this hook" not in out, (
            f"degrade notice fired despite a usable session_id\n{out}"
        )
