"""Rotated archive slices must be reachable by recall (issue #124).

When archive.md is the oversized bulk of a consolidation prompt, #123 rotates
it to archive-YYYY-MM-DD.md and starts a fresh one. The bytes survive on disk —
but nothing in the read path ever named those siblings, so that slice sat in
cold storage no recall reached. "No memory lost" was true mechanically and
false in practice, and repeated rotations accumulate more of it.

They are NAMED at session start, not injected. These files were rotated
precisely because they were too large to fit in a prompt; pasting them back
into every session would rebuild the problem rotation exists to solve. The
session-history hint tells the agent they are greppable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "session-start-hook.sh"


def _run_session_start(tmp_path: Path) -> str:
    """Run the session-start hook against a project and return its stdout."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True, exist_ok=True)
    (remember / "logs").mkdir(parents=True, exist_ok=True)
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    result = subprocess.run(["bash", str(HOOK)], env=env, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _seed(tmp_path: Path) -> Path:
    remember = tmp_path / "project" / ".remember"
    (remember / "tmp").mkdir(parents=True, exist_ok=True)
    (remember / "logs").mkdir(parents=True, exist_ok=True)
    (remember / "archive.md").write_text("# Archive\n\ncurrent slice\n", encoding="utf-8")
    return remember


def test_rotated_archives_are_named_at_session_start(tmp_path):
    """The acceptance criterion: a rotated slice is reachable, not invisible."""
    remember = _seed(tmp_path)
    (remember / "archive-2026-06-29.md").write_text(
        "# Archive\n\n## Week of 2026-06-22\nthe thing that only lives here\n",
        encoding="utf-8")

    out = _run_session_start(tmp_path)

    assert "archive-2026-06-29.md" in out, (
        "the rotated slice is not named anywhere in the session surface, so no "
        "recall can reach it (#124)"
    )


def test_rotated_archives_are_not_pasted_into_context(tmp_path):
    """Naming them is the point; injecting them would undo #123.

    They were rotated because the archive was too large for a prompt.
    """
    remember = _seed(tmp_path)
    (remember / "archive-2026-06-29.md").write_text(
        "# Archive\n\nSENTINEL-ROTATED-CONTENT\n", encoding="utf-8")

    out = _run_session_start(tmp_path)

    assert "SENTINEL-ROTATED-CONTENT" not in out, (
        "rotated archive contents were injected — that rebuilds the oversized "
        "prompt rotation exists to avoid"
    )


def test_several_rotations_are_all_listed_in_order(tmp_path):
    """Repeated rotations accumulate; every slice stays reachable."""
    remember = _seed(tmp_path)
    for day in ("2026-04-01", "2026-05-15", "2026-06-29"):
        (remember / f"archive-{day}.md").write_text(f"# Archive\n\n{day}\n", encoding="utf-8")

    out = _run_session_start(tmp_path)

    listed = [Path(line.split(" (")[0]).name
              for line in out.splitlines() if "archive-2026-" in line]
    assert listed == ["archive-2026-04-01.md", "archive-2026-05-15.md",
                      "archive-2026-06-29.md"], f"wrong set or order: {listed}"


def test_the_current_archive_is_still_injected(tmp_path):
    """Only the rotated siblings are held back — archive.md still loads."""
    remember = _seed(tmp_path)
    (remember / "archive.md").write_text("# Archive\n\nSENTINEL-CURRENT\n", encoding="utf-8")

    out = _run_session_start(tmp_path)

    assert "SENTINEL-CURRENT" in out, "the live archive stopped being injected"


def test_nothing_is_announced_when_no_rotation_has_happened(tmp_path):
    """The common case must stay silent — no empty section, no noise."""
    _seed(tmp_path)

    out = _run_session_start(tmp_path)

    # Match the section header, not the phrase: the history hint mentions
    # rotated archives too, and asserting on that made this pass for the wrong
    # reason regardless of what the hook emitted.
    assert "--- rotated archives" not in out, f"announced an empty rotation set:\n{out}"


def test_the_history_hint_names_the_rotated_pattern():
    """Recall is the agent grepping what the hint names."""
    hint = (REPO_ROOT / "prompts" / "session-history-hint.txt").read_text()
    assert "archive-YYYY-MM-DD" in hint, "the hint does not mention rotated archives"


def test_rotated_slices_alone_are_still_announced(tmp_path):
    """A store can hold nothing but rotated slices.

    Rotate an oversized archive and the fresh archive.md stays empty until the
    next consolidation. The listing sat inside the HAS_MEMORY guard, which
    never looked at archive-*.md — so the one state this issue exists to fix
    printed nothing at all: no MEMORY section, no paths, no way to know the
    files were there.
    """
    remember = tmp_path / "project" / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (remember / "logs").mkdir(parents=True)
    (remember / "archive-2026-06-29.md").write_text(
        "# Archive\n\nonly this exists\n", encoding="utf-8")

    out = _run_session_start(tmp_path)

    assert "archive-2026-06-29.md" in out, (
        f"a store holding only rotated slices announced nothing:\n{out}"
    )


def test_a_long_rotation_history_is_capped(tmp_path):
    """Rotations accumulate for the life of a store; this prints every start.

    The newest ten are named and the rest are covered by the glob, so nothing
    becomes unreachable again while the listing stays a fixed size.
    """
    remember = _seed(tmp_path)
    for day in range(1, 26):
        (remember / f"archive-2026-03-{day:02d}.md").write_text("x\n", encoding="utf-8")

    out = _run_session_start(tmp_path)

    listed = [line for line in out.splitlines() if line.startswith(str(remember))]
    assert len(listed) == 10, f"expected the newest 10 named, got {len(listed)}"
    assert "archive-2026-03-25.md" in out, "the newest slice is missing"
    assert "archive-2026-03-01.md" not in out, "the oldest slice should be summarised"
    assert "and 15 older" in out, f"the remainder is not accounted for:\n{out}"
    assert "archive-*.md" in out, "no glob given for the unlisted slices"


def test_a_same_day_collision_does_not_invert_recency_at_the_cap(tmp_path):
    """A second rotation the same day is archive-DATE-2.md.

    '-' (0x2D) sorts before '.' (0x2E), so that later sibling sorts BEFORE the
    base file it followed. Lexicographic order stops meaning recency inside a
    same-day cluster — and the cap decides what to drop right there, so the
    NEWER slice was summarised away while the older base was listed in its
    place. Eleven files against a cap of ten put the boundary inside the
    cluster, which is the only place the inversion shows.
    """
    import os
    remember = _seed(tmp_path)
    # Oldest first by mtime: the base file, then its same-day sibling, then
    # nine later days. Lexicographically the sibling sorts FIRST of all eleven.
    ordered = (["archive-2026-07-01.md", "archive-2026-07-01-2.md"]
               + [f"archive-2026-07-{d:02d}.md" for d in range(2, 11)])
    for i, name in enumerate(ordered):
        f = remember / name
        f.write_text("x\n", encoding="utf-8")
        os.utime(f, (1_700_000_000 + i * 60, 1_700_000_000 + i * 60))

    out = _run_session_start(tmp_path)

    assert "archive-2026-07-01-2.md" in out, (
        "the same-day sibling was dropped despite being newer than the base "
        "file that was kept — lexicographic order is not recency here"
    )
    assert "archive-2026-07-01.md\n" not in out.replace(" (", "\n ("), (
        "the genuinely oldest slice should have been the one dropped"
    )


def test_order_survives_a_git_restore_of_the_backed_up_store(tmp_path):
    """Ordering must not depend on mtime, because a restore destroys it.

    hooks.d/after_save/50-git-backup.sh commits the store to a remote, so
    recovering it is a git clone — and git checkout writes files in
    byte-lexicographic order, handing archive-DATE-2.md an EARLIER mtime than
    archive-DATE.md. Ordering by mtime therefore inverts recency on every
    restore, for every file, which is worse than the same-day-only bug it was
    meant to fix. The name carries the truth; this pins that it is what is used.
    """
    import os
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=origin, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=origin, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=origin, check=True)
    # The base file is that day's FIRST rotation; -2 came after it.
    for name in ("archive-2026-07-01.md", "archive-2026-07-01-2.md"):
        (origin / name).write_text("x\n", encoding="utf-8")
    for day in range(2, 11):
        (origin / f"archive-2026-07-{day:02d}.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=origin, check=True)

    remember = tmp_path / "project" / ".remember"
    remember.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(remember)], check=True)
    (remember / "tmp").mkdir(exist_ok=True)
    (remember / "logs").mkdir(exist_ok=True)

    restored = sorted((remember / n).stat().st_mtime
                      for n in ("archive-2026-07-01.md", "archive-2026-07-01-2.md"))
    out = _run_session_start(tmp_path)

    assert "archive-2026-07-01-2.md" in out, (
        "the newer same-day sibling was dropped after a restore — ordering is "
        f"following checkout mtimes, not the names (mtimes: {restored})"
    )
