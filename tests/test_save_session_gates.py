"""save-session.sh extraction gates: what skips, what saves, what advances (#147).

The saved position (``last-save.json``) is written only after a successful save,
while the cooldown marker is written before every gate. So any early exit that
leaves the position untouched makes the *next* run re-extract the identical span
and exit identically — once per cooldown window, forever. Two gates sit there:

* ``EXCHANGE_COUNT == 0`` — nothing to summarize, so the position must advance.
* ``HUMAN_COUNT < min_human_messages`` — real content, just not enough yet, so
  the position must NOT advance (those turns get summarized with the ones that
  follow). But an *agentic* session — many tool calls, few human turns — never
  clears this gate at all, so a large-enough span saves anyway.

These tests drive the real script with a stubbed ``pipeline.shell`` so the gates
are exercised without a Haiku call.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytest

# sys.path insert + bare import (not `.subprocess_helpers`) so this module
# keeps working when test_ndc_day_boundary.py / test_reject_gate_visibility.py
# import it as a bare top-level module via their own sys.path hack, where a
# relative import would fail with "no known parent package".
sys.path.insert(0, os.path.dirname(__file__))
from subprocess_helpers import subprocess_failure_detail

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Stub for `python -m pipeline.shell <cmd>`. Covers only the commands the script
# reaches before/around the gates; every call is appended to calls.log so a test
# can assert what ran. call-haiku returns a SKIP so no summary is ever written.
STUB_SHELL = '''\
import os, sys, tempfile

CALLS = os.environ["STUB_CALLS_LOG"]
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
with open(CALLS, "a") as f:
    f.write(" ".join([cmd] + sys.argv[2:]) + "\\n")

if cmd == "extract":
    fd, path = tempfile.mkstemp(suffix="-extract")
    with os.fdopen(fd, "w") as f:
        f.write("Human: something\\nAssistant: something else\\n")
    print(f"POSITION={os.environ['STUB_POSITION']}")
    print(f"HUMAN_COUNT={os.environ['STUB_HUMAN_COUNT']}")
    print("ASSISTANT_COUNT=1")
    print(f"EXCHANGE_COUNT={os.environ['STUB_EXCHANGE_COUNT']}")
    print(f"EXTRACT_FILE={path}")
elif cmd == "save-position":
    last_save_file, session_id, position = sys.argv[2], sys.argv[3], sys.argv[4]
    import json
    with open(last_save_file, "w") as f:
        json.dump({"session": session_id, "line": int(position)}, f)
elif cmd == "build-prompt":
    # argv: build-prompt <extract> <last_entry> <time> <branch> <out> <max_bytes>
    with open(sys.argv[6], "w") as f:
        f.write("a prompt with no placeholders\\n")
elif cmd == "call-haiku":
    if os.environ.get("STUB_HAIKU_FAIL") == "1":
        sys.stderr.write("stub: simulated haiku failure\\n")
        sys.exit(1)
    # The NDC call passes extra args ("" and a timeout); the main save call
    # passes only the prompt. That is how we tell the two apart here.
    is_ndc = len(sys.argv) > 3
    if is_ndc and os.environ.get("STUB_APPEND_DURING_NDC"):
        # Stand in for a *newer* save appending to now.md while this
        # compression is in flight — the #142 window.
        with open(os.environ["STUB_MEMORY_FILE"], "a") as f:
            f.write(os.environ["STUB_APPEND_DURING_NDC"])
    fd, path = tempfile.mkstemp(suffix="-haiku")
    with os.fdopen(fd, "w") as f:
        if is_ndc:
            f.write("## 2026-07-25\\n\\n- compressed summary\\n")
        elif os.environ.get("STUB_HAIKU_TEXT"):
            f.write(os.environ["STUB_HAIKU_TEXT"])
        elif os.environ.get("STUB_HAIKU_SKIP", "1") == "1":
            f.write("SKIP\\n")
        else:
            f.write("## 10:00 | main\\n\\n- did some work\\n")
    rejected = os.environ.get("STUB_HAIKU_REJECTED") == "1" and not is_ndc
    skipping = (os.environ.get("STUB_HAIKU_SKIP", "1") == "1"
                and not os.environ.get("STUB_HAIKU_TEXT"))
    print("IS_SKIP=" + ("true" if (rejected or (not is_ndc and skipping)) else "false"))
    print("IS_REJECTED=" + ("true" if rejected else "false"))
    print(f"HAIKU_TEXT_FILE={path}")
    print("TK_IN=0"); print("TK_OUT=0"); print("TK_CACHE=0"); print("TK_COST=0")
elif cmd == "build-ndc-prompt":
    # Must be non-empty: save-session.sh gates the NDC run on `[ -s ... ]`.
    with open(sys.argv[3], "w") as f:
        f.write("compress this now.md\\n")
'''


def _make_env(tmp_path: Path, *, exchanges: int, humans: int, position: int = 500,
              config: Optional[dict] = None):
    """Build a project + stub plugin and return the env for running save-session.sh."""
    project = tmp_path / "project"
    (project / ".remember" / "tmp").mkdir(parents=True)
    (project / ".remember" / "logs").mkdir(parents=True)

    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "pipeline").mkdir(parents=True)
    (plugin / "pipeline" / "__init__.py").write_text("")
    (plugin / "pipeline" / "haiku.py").write_text("# marker\n")
    (plugin / "pipeline" / "shell.py").write_text(STUB_SHELL)
    for script in ("save-session.sh", "resolve-paths.sh", "detect-tools.sh",
                   "bootstrap-dirs.sh", "log.sh", "lib-memory-dir.sh",
                   "lib-lock.sh", "lib-slug.sh"):
        (plugin / "scripts" / script).write_text((REPO_ROOT / "scripts" / script).read_text())

    cfg = {"cooldowns": {"save_seconds": 0, "ndc_seconds": 999999},
           "thresholds": {"min_human_messages": 3, "delta_lines_trigger": 50},
           # True, matching the shipped default. It was False here while the
           # flag was ignored (#159), so every test that exercises compression
           # was relying on the option not working.
           "features": {"ndc_compression": True}}
    if config:
        cfg["thresholds"].update(config)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    (plugin / "config.json").write_text(json.dumps(cfg))

    # A session transcript must exist for the script's session-id discovery.
    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    slug = str(project).replace("/", "-").replace(".", "-").replace("_", "-")
    session_dir = tmp_path / "home" / ".claude" / "projects" / slug
    session_dir.mkdir(parents=True)
    (session_dir / f"{session_id}.jsonl").write_text('{"type":"user"}\n' * 10)

    calls_log = tmp_path / "calls.log"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "REMEMBER_CONFIG": str(cfg_path),
        "STUB_CALLS_LOG": str(calls_log),
        "STUB_POSITION": str(position),
        "STUB_HUMAN_COUNT": str(humans),
        "STUB_EXCHANGE_COUNT": str(exchanges),
        "STUB_MEMORY_FILE": str(project / ".remember" / "now.md"),
    }
    return env, project, plugin, calls_log, session_id


def _run(plugin: Path, env: dict, session_id: str, *args: str):
    return subprocess.run(
        ["bash", str(plugin / "scripts" / "save-session.sh"), session_id, *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _saved_position(project: Path):
    f = project / ".remember" / "tmp" / "last-save.json"
    return json.loads(f.read_text())["line"] if f.is_file() else None


def _suppress_ndc(project: Path):
    """Stop the background NDC run from draining now.md mid-assertion.

    features.ndc_compression is documented but read nowhere, so the only
    working brake is the cooldown marker (ndc_seconds is 999999 here).
    """
    import time
    (project / ".remember" / "tmp" / "last-ndc.ts").write_text(str(int(time.time())))


class TestNoWorkSessionAdvancesPosition:

    def test_zero_exchanges_advances_position(self, tmp_path):
        """An empty span has nothing to summarize — the cursor must still move (#147)."""
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=0, humans=0,
                                                     position=812)
        result = _run(plugin, env, sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert _saved_position(project) == 812, (
            "position must advance past an empty span, else the next run re-extracts "
            "the same lines and exits identically, once per cooldown, forever"
        )
        assert "call-haiku" not in calls.read_text(), "no summary should be attempted"


class TestMinHumanGate:

    def test_below_threshold_skips_without_advancing(self, tmp_path):
        """Too few human turns: skip, but keep the cursor so the content survives."""
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        result = _run(plugin, env, sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert _saved_position(project) is None, (
            "these exchanges are real content — advancing would drop them from every "
            "future extract, so they must stay pending"
        )
        assert "call-haiku" not in calls.read_text()

    def test_agentic_session_saves_despite_low_human_count(self, tmp_path):
        """Many exchanges, few human turns: this is work, and it must reach memory."""
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=45, humans=1)
        result = _run(plugin, env, sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert "call-haiku" in calls.read_text(), (
            "an agentic session (many tool calls, few human turns) never clears the "
            "min-human gate — without the exchange-count fallback the plugin's core "
            "function silently never runs (#147/#125)"
        )

    def test_fallback_threshold_is_configurable(self, tmp_path):
        """A higher min_exchanges_without_human keeps the same span gated out."""
        env, project, plugin, calls, sid = _make_env(
            tmp_path, exchanges=45, humans=1,
            config={"min_exchanges_without_human": 100},
        )
        result = _run(plugin, env, sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert "call-haiku" not in calls.read_text()

    def test_fallback_can_be_disabled_with_zero(self, tmp_path):
        """0 restores the strict gate: human turns are the only way through."""
        env, project, plugin, calls, sid = _make_env(
            tmp_path, exchanges=500, humans=1,
            config={"min_exchanges_without_human": 0},
        )
        result = _run(plugin, env, sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert "call-haiku" not in calls.read_text()

    def test_enough_human_turns_still_saves(self, tmp_path):
        """The ordinary path is untouched by the fallback."""
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
        result = _run(plugin, env, sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert "call-haiku" in calls.read_text()


class TestDryRun:

    def test_dry_run_does_not_advance_position_on_empty_span(self, tmp_path):
        """--dry inspects; it must never move the cursor, empty span included."""
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=0, humans=0,
                                                     position=812)
        result = _run(plugin, env, sid, "--dry")

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert _saved_position(project) is None, (
            "a dry run must leave last-save.json untouched"
        )


class TestSummaryFailureLoop:
    """A failing summarizer must be retried, then given up on — never looped forever.

    Keeping the position on failure is right for a transient error: the span is
    retried next run. But a *persistent* failure on the same span then retries
    once per cooldown window forever and memory never advances again — the
    month-long macOS repro in #147. After max_summary_failures the span is
    dropped, loudly, so later spans can still be saved.
    """

    def test_failure_keeps_position_and_retries(self, tmp_path):
        """First failures leave the cursor alone so the span is tried again."""
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
        env["STUB_HAIKU_FAIL"] = "1"

        result = _run(plugin, env, sid)

        assert result.returncode == 1
        assert _saved_position(project) is None, (
            "a transient failure must not drop the span — it is retried next run"
        )

    def test_gives_up_after_max_failures(self, tmp_path):
        """The Nth consecutive failure on the same span advances past it."""
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5,
                                                     position=904)
        env["STUB_HAIKU_FAIL"] = "1"

        _run(plugin, env, sid)
        assert _saved_position(project) is None
        _run(plugin, env, sid)
        assert _saved_position(project) is None, "still retrying at failure 2 of 3"

        _run(plugin, env, sid)
        assert _saved_position(project) == 904, (
            "after max_summary_failures the span must be dropped and the position "
            "advanced, or every later span is lost too (#147)"
        )

    def test_max_summary_failures_zero_retries_forever(self, tmp_path):
        """0 restores the old behaviour: never give up on a span."""
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5,
                                                     config={"max_summary_failures": 0})
        env["STUB_HAIKU_FAIL"] = "1"

        for _ in range(4):
            _run(plugin, env, sid)

        assert _saved_position(project) is None

    def test_success_clears_the_failure_count(self, tmp_path):
        """Two failures then a success must not leave a primed counter behind."""
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
        env["STUB_HAIKU_FAIL"] = "1"
        _run(plugin, env, sid)
        _run(plugin, env, sid)

        env["STUB_HAIKU_FAIL"] = "0"
        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

        marker = project / ".remember" / "tmp" / "last-summary-failure"
        assert not marker.exists(), "a successful save must reset the failure count"


class TestHeaderTimeIsTakenBackOffTheModel:
    """The prompt injects {{TIME}} and says to copy it, but the model reads a
    transcript full of other timestamps and sometimes stamps one of those. The
    format check cannot see it — a wrong time is a well-formed one — so entries
    landed hours out of order with nothing logged (#139)."""

    def test_a_wrong_time_is_overwritten_with_the_scripts_own(self, tmp_path):
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"
        _suppress_ndc(project)
        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

        header = (project / ".remember" / "now.md").read_text().strip().splitlines()[0]
        logs = "".join(p.read_text() for p in (project / ".remember" / "logs").glob("*.log"))
        assert "header time corrected" in logs, f"no correction logged:\n{logs}"

        corrected = re.search(r"header time corrected to (\S+)", logs).group(1)
        assert header == f"## {corrected} | main", (
            f"header kept the model's time instead of the script's: {header!r}"
        )

    def test_the_branch_and_body_survive_the_rewrite(self, tmp_path):
        """Only the time is the pipeline's to know — the rest is the model's."""
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | feature/some-branch\n\n- fixed the thing\n"
        _suppress_ndc(project)
        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

        written = (project / ".remember" / "now.md").read_text()
        assert "| feature/some-branch" in written, f"branch lost: {written!r}"
        assert "- fixed the thing" in written, f"body lost: {written!r}"

    def test_a_correct_time_is_left_alone(self, tmp_path):
        """The rewrite is a no-op when the model copied the time properly."""
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
        now = datetime.now().strftime("%H:%M")
        env["STUB_HAIKU_TEXT"] = f"## {now} | main\n\n- did some work\n"
        _suppress_ndc(project)
        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

        logs = "".join(p.read_text() for p in (project / ".remember" / "logs").glob("*.log"))
        header = (project / ".remember" / "now.md").read_text().strip().splitlines()[0]
        # Tolerate the minute rolling over between the two clock reads.
        if "header time corrected" in logs:
            corrected = re.search(r"header time corrected to (\S+)", logs).group(1)
            assert header == f"## {corrected} | main"
        else:
            assert header == f"## {now} | main"


class TestMalformedOutputNeverReachesMemory:
    """A response that is not an entry header is not an entry. It used to be
    logged as a warning and appended anyway, which put a permission prompt in
    one reporter's now.md — and memory is a summary of summaries, so it does
    not fade, it gets compressed downstream as though it were work (#136)."""

    MALFORMED = "Bash needs approval to SSH into preface-vps and check. Shall I proceed?\n"

    def test_malformed_output_is_not_appended(self, tmp_path):
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
        env["STUB_HAIKU_TEXT"] = self.MALFORMED
        _suppress_ndc(project)
        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

        # Check every memory file, not just now.md: compression moves entries
        # on, so asserting only on now.md passes whether the junk was rejected
        # or merely relayed into today-*.md.
        remember = project / ".remember"
        written = "".join(
            f.read_text() for f in remember.glob("*.md") if f.is_file()
        )
        assert "needs approval" not in written, f"junk reached memory: {written!r}"

    def test_malformed_output_is_kept_for_inspection(self, tmp_path):
        """Dropped from memory, but never destroyed."""
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
        env["STUB_HAIKU_TEXT"] = self.MALFORMED
        _run(plugin, env, sid)

        rejects = list((project / ".remember" / "tmp").glob("rejected-*.md"))
        assert len(rejects) == 1, f"nothing quarantined: {rejects}"
        assert "needs approval" in rejects[0].read_text()

    def test_malformed_output_still_advances_the_position(self, tmp_path):
        """Otherwise the same span is re-summarized on every run, forever."""
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5,
                                                     position=904)
        env["STUB_HAIKU_TEXT"] = self.MALFORMED
        _run(plugin, env, sid)

        assert _saved_position(project) == 904

    def test_rejected_files_do_not_accumulate(self, tmp_path):
        """They are diagnostic, not memory — keep the last 20."""
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
        tmpdir = project / ".remember" / "tmp"
        for i in range(25):
            (tmpdir / f"rejected-2026010{i // 10}-{i:06d}.md").write_text("old\n")
        env["STUB_HAIKU_TEXT"] = self.MALFORMED
        _run(plugin, env, sid)

        rejects = list(tmpdir.glob("rejected-*.md"))
        assert len(rejects) <= 20, f"{len(rejects)} rejected files kept"


class TestNoWorkSkipRule:

    def test_prompt_tells_the_model_to_skip_a_no_work_session(self):
        """#119's cause is prompt-side: the only SKIP was for repeated work, so
        a greeting or handoff got faithfully summarized as the work done."""
        prompt = (REPO_ROOT / "prompts" / "save-session.prompt.txt").read_text()
        assert "no substantive work" in prompt, "the no-work SKIP rule is gone"
        assert "SKIP" in prompt

    def test_a_header_missing_the_space_after_the_pipe_is_not_corrupted(self):
        """The format check only demands a space BEFORE the pipe.

        So "## 18:30 |main" reaches the rewrite, and splitting on a literal
        " | " found nothing to strip — the whole original line became the
        "rest" and the entry was written as "## 00:00 | ## 18:30 |main". A
        wrong-but-readable time turned into an unreadable header, which is
        worse than the bug the rewrite exists to fix.
        """
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        env, project, plugin, calls, sid = _make_env(tmp, exchanges=6, humans=5)
        env["STUB_HAIKU_TEXT"] = "## 18:30 |main\n\n- did some work\n"
        _suppress_ndc(project)
        _run(plugin, env, sid)

        header = (project / ".remember" / "now.md").read_text().strip().splitlines()[0]
        assert header.count("##") == 1, f"header duplicated: {header!r}"
        assert header.endswith("| main"), f"branch mangled: {header!r}"

    def test_a_pipe_inside_the_branch_name_survives(self):
        """Only the FIRST pipe separates time from the rest."""
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        env, project, plugin, calls, sid = _make_env(tmp, exchanges=6, humans=5)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | feature/a|b\n\n- did some work\n"
        _suppress_ndc(project)
        _run(plugin, env, sid)

        header = (project / ".remember" / "now.md").read_text().strip().splitlines()[0]
        assert header.endswith("| feature/a|b"), f"branch truncated: {header!r}"
