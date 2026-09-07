"""#619 skips the NDC commit when NDC_GEN_FILE is durably unreadable -- correct
for the transient race #619 targets, but the log line it added gave no hint
that the fix is to delete the marker. If the file is durably unreadable
(crashed mid-write, replaced by a directory, permissions never fixed), every
future round now skips forever with nothing telling the operator how to get
out of it (#626).

The fix: the same "SKIPPED commit" line #619 added now also names the
concrete remedy -- delete NDC_GEN_FILE to reset generation tracking to 0.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

from .test_ndc_commit_lock import _log_text
from .test_ndc_truncate_race import _ndc_env, _wait_for_background_ndc
from .test_save_session_gates import _run


def test_skipped_commit_names_the_marker_delete_as_the_remedy(tmp_path):
    """Same reproduction as #619's own unreadable-marker test: a directory in
    place of NDC_GEN_FILE, so every read of it fails and the commit is
    SKIPPED. The log line must now also say what to do about it.
    """
    env, project, plugin, _calls, sid = _ndc_env(tmp_path)
    memory_file = project / ".remember" / "now.md"
    gen_file = project / ".remember" / "tmp" / "ndc-generation"
    gen_file.mkdir()  # exists, but `cat` on a directory always fails

    result = _run(plugin, env, sid)
    assert result.returncode == 0

    _wait_for_background_ndc(memory_file)
    log_text = _log_text(project)
    assert "SKIPPED commit" in log_text, (
        "setup did not reach the #619 skip path -- nothing to add a remedy to"
    )
    assert str(gen_file) in log_text and "delete" in log_text.lower() and (
        "reset generation tracking to 0" in log_text
    ), (
        "the SKIPPED-commit log line must name the concrete remedy -- delete "
        f"the marker to reset generation tracking to 0 -- not just the fact "
        f"that the commit was skipped (#626).\n{log_text}"
    )
