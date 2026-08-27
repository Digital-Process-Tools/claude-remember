"""Two INTERACTIVE sessions sharing one project store must not clobber each
other's handoff (#363).

Distinct from #221/#222. Those protect a PENDING handoff from a session that
never writes one back (a scheduled task passing through, `claude -p`). This
is the opposite shape: two sessions that EACH run `/remember` and each write
a real, different note — to the SAME fixed path, `remember.md`
(`session-start-hook.sh:795`, pre-fix). Last-writer-wins: the second write
silently destroys the first, which then survives only in that session's own
transcript, nowhere on disk. `_resolve_memory_project_dir` (#56) makes the
store shared by design, including across worktrees, so this is not a rare
collision — it is the ordinary case of working the same project from two
panes.

`handoff_mode: "per_session"` gives each session's handoff its own file,
`remember.<session_id>.md`. `"single"` (the default) is unchanged — every
session still shares `remember.md`, byte-identical to pre-#363 behaviour.

Four properties:
  - two sessions writing under per_session mode do not clobber each other
  - a session's own injection reads ITS OWN handoff, never a sibling's —
    with the sibling's handoff verified present on disk as the positive
    control (a fixture where the harness cannot see either file would also
    "pass")
  - single mode is a true no-op: absent key and explicit "single" produce
    byte-identical output, and every session still shares one file
  - per_session mode requested but no usable session_id: the hint is
    WITHHELD rather than silently pointing at the shared remember.md — that
    fallback is the exact clobber this issue exists to fix, only quieter,
    because the user believes per_session is protecting them
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX session-start hook — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START_SCRIPT = REPO_ROOT / "scripts" / "session-start-hook.sh"

from pipeline.slug import session_dir_slug as _slug


def _sandbox(tmp_path: Path, *, handoff_mode: Optional[str] = None, external: bool = False):
    """A store: legacy ({project}/.remember) unless `external` asks for an
    out-of-project data_dir, which is what lets the HANDOFF hint fire even
    with no per-session path involved (mirrors test_external_data_dir.py)."""
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".remember").mkdir(parents=True)

    cfg: dict = {"features": {"recovery": False}}
    if external:
        cfg["data_dir"] = str(tmp_path / "ext-mem") + "/{slug}"
    else:
        cfg["data_dir"] = ".remember"
    if handoff_mode is not None:
        cfg["handoff_mode"] = handoff_mode
    (home / ".remember" / "config.json").write_text(json.dumps(cfg))

    slug = _slug(str(project))
    (home / ".claude" / "projects" / slug).mkdir(parents=True)

    if external:
        remember_dir = tmp_path / "ext-mem" / slug
    else:
        remember_dir = project / ".remember"
    remember_dir.mkdir(parents=True, exist_ok=True)
    return project, home, remember_dir


def _payload(session_id: str) -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": f"/does/not/matter/{session_id}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": "/does/not/matter",
    })


def _session_start(project: Path, home: Path, session_id: Optional[str]) -> str:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "HOME": str(home),
    }
    kwargs = {"env": env, "capture_output": True, "text": True, "timeout": 60}
    if session_id is not None:
        kwargs["input"] = _payload(session_id)
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    result = subprocess.run(
        ["bash", str(SESSION_START_SCRIPT)], check=False, **kwargs
    )
    assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr[:500]}"
    return result.stdout


def _handoff_path_from_hint(output: str) -> str:
    assert "Write next handoff to:" in output, f"no HANDOFF hint in output:\n{output}"
    line = output.split("Write next handoff to:", 1)[1].splitlines()[0]
    return line.strip()


class TestPerSessionHandoffDoesNotClobber:

    def test_two_sessions_writing_do_not_clobber_each_other(self, tmp_path):
        """The load-bearing case: two DIFFERENT sessions each write their own
        handoff. Under the bug both target remember.md and the second write
        destroys the first. Under the fix each gets its own file."""
        project, home, _remember_dir = _sandbox(tmp_path, handoff_mode="per_session")

        out_a = _session_start(project, home, "sess-aaa")
        path_a = _handoff_path_from_hint(out_a)
        Path(path_a).write_text("Session A: finish the migration guard, see #218.\n")

        out_b = _session_start(project, home, "sess-bbb")
        path_b = _handoff_path_from_hint(out_b)
        Path(path_b).write_text("Session B: the auth bug is in middleware.py.\n")

        assert path_a != path_b, (
            f"both sessions resolved to the same handoff path: {path_a!r}"
        )
        assert Path(path_a).exists() and "migration guard" in Path(path_a).read_text(), (
            "session A's handoff did not survive session B's write"
        )
        assert Path(path_b).exists() and "auth bug" in Path(path_b).read_text(), (
            "session B's own handoff was not written where expected"
        )

    def test_injection_reads_this_sessions_own_handoff_not_a_siblings(self, tmp_path):
        """A session must see ITS OWN prior handoff and never a sibling's.

        Positive control: B's handoff is written to disk and asserted absent
        from A's injection specifically, not merely "nothing was injected" —
        a fixture where the harness saw neither file would also pass the
        negative half for the wrong reason.
        """
        project, home, remember_dir = _sandbox(tmp_path, handoff_mode="per_session")
        (remember_dir / "remember.sess-aaa.md").write_text("A's handoff: alpha work.\n")
        (remember_dir / "remember.sess-bbb.md").write_text("B's handoff: beta work.\n")

        out_a = _session_start(project, home, "sess-aaa")
        assert "alpha work" in out_a, f"session A did not see its own handoff\n{out_a}"
        assert "beta work" not in out_a, f"session A saw session B's handoff\n{out_a}"

        out_b = _session_start(project, home, "sess-bbb")
        assert "beta work" in out_b, f"session B did not see its own handoff\n{out_b}"
        assert "alpha work" not in out_b, f"session B saw session A's handoff\n{out_b}"


class TestSingleModeUnchanged:

    def test_single_mode_is_byte_identical_to_no_config_at_all(self, tmp_path):
        """The default must be a true no-op, not merely close: absent
        `handoff_mode` and an explicit "single" must produce identical
        output for the same fixture."""
        project1, home1, rd1 = _sandbox(tmp_path / "a")
        (rd1 / "remember.md").write_text("Legacy handoff, unnamespaced.\n")
        out_default = _session_start(project1, home1, "sess-x")

        project2, home2, rd2 = _sandbox(tmp_path / "b", handoff_mode="single")
        (rd2 / "remember.md").write_text("Legacy handoff, unnamespaced.\n")
        out_explicit = _session_start(project2, home2, "sess-x")

        assert out_default == out_explicit, (
            "handoff_mode: \"single\" is not byte-identical to the unset default"
        )

    def test_single_mode_all_sessions_share_one_file_regardless_of_session_id(self, tmp_path):
        """Today's behaviour, pinned: two different session_ids in single
        mode still resolve to the one shared remember.md."""
        project, home, remember_dir = _sandbox(tmp_path)  # default: single
        (remember_dir / "remember.md").write_text("Shared note, single mode.\n")

        out_a = _session_start(project, home, "sess-aaa")
        out_b = _session_start(project, home, "sess-bbb")

        assert "Shared note" in out_a, f"session A did not see the shared handoff\n{out_a}"
        assert "Shared note" in out_b, f"session B did not see the shared handoff\n{out_b}"
        assert not (remember_dir / "remember.sess-aaa.md").exists(), (
            "single mode must never create a per-session file"
        )
        assert not (remember_dir / "remember.sess-bbb.md").exists()


class TestMissingSessionIdDoesNotSilentlyReintroduceTheClobber:

    def test_degraded_per_session_mode_still_gives_the_correct_external_path(self, tmp_path):
        """per_session requested, but no usable session_id reached the hook
        (payload absent, or #270's sanitizer rejected it) — REMEMBER_HANDOFF
        falls back to the shared file. In EXTERNAL mode that shared file is
        still the one real path, so the hint must still fire: an earlier
        version of this fix withheld it outright on degrade, which broke
        external mode by reintroducing the exact bug the hint exists to
        prevent (the /remember skill falling back to its own hardcoded,
        project-relative default instead of the true external location).
        """
        project, home, remember_dir = _sandbox(tmp_path, handoff_mode="per_session", external=True)

        out = _session_start(project, home, None)

        path = _handoff_path_from_hint(out)
        assert path == str(remember_dir / "remember.md"), (
            f"degraded per_session hint did not point at the real external "
            f"handoff file\n{out}"
        )

    def test_degraded_per_session_mode_says_so_out_loud(self, tmp_path):
        """Silence is what let the earlier bug (#221) hide; degrading to the
        shared file must be visible, not merely harmless, so a user who set
        per_session does not read isolation into a session that never got
        one."""
        project, home, _remember_dir = _sandbox(tmp_path, handoff_mode="per_session", external=True)

        out = _session_start(project, home, None)

        assert "no session_id reached this hook" in out, (
            f"degraded per_session mode said nothing about it\n{out}"
        )

    def test_resolved_per_session_mode_says_nothing_extra(self, tmp_path):
        """Positive control for the assertion above: with a usable
        session_id, per_session is NOT degraded, and the degradation notice
        must not appear — otherwise the notice text would be unconditional
        noise rather than a signal tied to the actual degraded state."""
        project, home, _remember_dir = _sandbox(tmp_path, handoff_mode="per_session")

        out = _session_start(project, home, "sess-ccc")

        assert "no session_id reached this hook" not in out, (
            f"degradation notice fired for a session that had a usable id\n{out}"
        )
