"""A store migrated from legacy to external mode, with git backup enabled,
can no longer permanently disable doctor.sh's SessionEnd liveness FAIL path
(#401).

hooks.d/after_save/50-git-backup.sh deletes $REMEMBER_DIR/.gitignore as a
one-time cleanup of the legacy bootstrap artifact once a backed-up save
lands ("removed per-slug .gitignore (legacy bootstrap artifact)"). Before
this fix, scripts/doctor.sh read that same file's mtime as its SessionEnd
install baseline (#392/#400) -- the one file under REMEMBER_DIR ordinary
hook activity never rewrites. The two behaviours are individually correct
and were written years apart; composed, the cleanup silently removed the
diagnostic's only baseline, and every quiet transcript afterwards read as
"predates the store" (the #392 false-positive shape) rather than as
evidence -- the SessionEnd check degraded to a permanent WARN and could
never reach FAIL again for that store.

Route 2 from the issue: a dedicated install marker under the store
($REMEMBER_DIR/.install-marker, written once by bootstrap-dirs.sh, gated on
it not already existing) that 50-git-backup.sh's .gitignore cleanup does not
touch. bootstrap-dirs.sh writes it unconditionally of storage mode -- unlike
.gitignore, which is only ever written for a store inside the project tree
-- so external-mode stores get a real baseline for the first time too, not
only migrated ones.
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

from tests.test_doctor_session_end_370 import (
    _backdate,
    _project,
    _run,
    _verdict,
)

INSTALL_MARKER_NAME = ".install-marker"


def _simulate_migrated_and_backed_up(remember: Path, seconds_ago: int) -> None:
    """Reproduce the exact shape a legacy-to-external migration with git
    backup enabled leaves behind: the install marker survives (that is the
    whole point of this fix), but the OLD baseline -- $REMEMBER_DIR/.gitignore
    -- is gone, exactly as 50-git-backup.sh's own cleanup removes it
    ("removed per-slug .gitignore (legacy bootstrap artifact)"). A store that
    was never migrated never had that file to begin with, so its absence
    alone is not distinctive -- what this fixture pins is that the NEW
    marker is what doctor.sh now reads, independent of the old file's fate.
    """
    marker = remember / INSTALL_MARKER_NAME
    marker.write_text("installed\n", encoding="utf-8")
    _backdate(marker, seconds_ago)
    gitignore = remember / ".gitignore"
    if gitignore.exists():
        gitignore.unlink()


def test_session_end_still_reaches_fail_after_the_gitignore_cleanup(tmp_path):
    """The must-fire case for a migrated-and-backed-up store: SessionEnd's
    own silence must still be reachable as FAIL, not permanently masked.

    Paired with test_session_end_still_warns_when_nothing_ever_ended below --
    the positive control for the SAME cleaned-up-marker shape, proving the
    fixture is not simply incapable of producing a FAIL at all.
    """
    home, project, remember, session_dir = _project(tmp_path)
    _simulate_migrated_and_backed_up(remember, 7200)
    stale = session_dir / "aaaa-quiet-session.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    _backdate(stale, 3600)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd has never fired" in result.stdout, (
        "a store whose .gitignore baseline was removed by the git-backup "
        "cleanup could not reach a genuine SessionEnd FAIL -- the check is "
        "permanently degraded to WARN for this store:\n" + result.stdout
    )
    assert "SessionEnd" in _verdict(result.stdout)


def test_session_end_still_warns_when_nothing_ever_ended(tmp_path):
    """Positive control: the same cleaned-up-marker shape, but nothing has
    demonstrably ended since install -- must stay the honest third state
    (WARN, no VERDICT line), not a manufactured FAIL out of an absent
    baseline.
    """
    home, project, remember, session_dir = _project(tmp_path)
    _simulate_migrated_and_backed_up(remember, 7200)
    fresh = session_dir / "aaaa-this-session.jsonl"
    fresh.write_text("{}\n", encoding="utf-8")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd" not in result.stdout, (
        "a fresh, un-backdated transcript was read as a session having "
        "ended, in a store whose .gitignore baseline was removed by the "
        "git-backup cleanup:\n" + result.stdout
    )
    assert "SessionEnd" not in _verdict(result.stdout)
