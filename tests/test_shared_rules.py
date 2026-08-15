"""Rules that exist in more than one place must not be spelled out twice (#177).

The slug drifted across five copies (#144/#156/#157/#158/#174). The position
readers drifted across five. The projects path across five. In each case the
copies agreed on the day they were written and the disagreement surfaced as
memory that silently stopped saving.

None of the remaining copies disagreed when #177 was filed — that is the point.
These tests aim to fail when a second definition appears rather than after it
has drifted; where a check can only recognise the copy-paste form of that, its
docstring says so.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.entry_header import (  # noqa: E402
    ANY_HEADER_ERE,
    ENTRY_HEADER_ERE,
    contains_header,
    is_entry_header,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAVE_SH = REPO_ROOT / "scripts" / "save-session.sh"
RUN_TESTS_SH = REPO_ROOT / "scripts" / "run-tests.sh"
CONSOLIDATE_PY = REPO_ROOT / "pipeline" / "consolidate.py"

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="bash subprocess assertions (#79)"
)


# ── The entry-header regex ───────────────────────────────────────────────────

HEADERS_THAT_ARE_ENTRIES = [
    "## 09:30 | main",
    "## 23:59 | feature/x",
    "## 9:30 AM | main",       # the #139 fallback can leave this in memory
    "## 11:05 PM | main",
    "## 18:30 |main",          # no space after the pipe: the check allows it
]

HEADERS_THAT_ARE_NOT_ENTRIES = [
    "## main",
    "## 09:30",                # no identity field
    "Some prose about 09:30 | main",
    "# 09:30 | main",          # h1, not h2
    "",
]


@pytest.mark.parametrize("line", HEADERS_THAT_ARE_ENTRIES)
def test_python_accepts_every_entry_header(line):
    assert is_entry_header(line), line


@pytest.mark.parametrize("line", HEADERS_THAT_ARE_NOT_ENTRIES)
def test_python_rejects_everything_else(line):
    assert not is_entry_header(line), line


@posix_only
@pytest.mark.parametrize("line", HEADERS_THAT_ARE_ENTRIES + HEADERS_THAT_ARE_NOT_ENTRIES + [
    # Multi-line: grep anchors ^ per line, so Python must too or the two
    # disagree for any caller that passes more than one line.
    "some prose\\n## 09:30 | main",
    "## 09:30 | main\\ntrailing prose",
    "no header\\nstill no header",
])
def test_grep_agrees_with_python(line):
    """The shell validates with `grep -E` against this same pattern.

    Two engines, one pattern — so this is really asking whether the pattern is
    portable between them, which is the price of having a single definition.
    """
    result = subprocess.run(
        ["grep", "-qE", ENTRY_HEADER_ERE],
        input=line, capture_output=True, text=True,
    )
    assert (result.returncode == 0) == is_entry_header(line), (
        f"grep and Python disagree on {line!r}"
    )


def test_consolidation_recognises_a_twelve_hour_entry():
    """The gap that made this worth fixing rather than just deduplicating.

    save-session.sh accepted AM/PM; consolidate.py's own copy did not. The
    header is normally rewritten to 24h, so the two agreed in practice — until
    #139's fallback keeps the model's original line, and then consolidation
    reads a real entry as prose.
    """
    from pipeline.consolidate import _is_valid_consolidation

    entry = "## 9:30 AM | main\n\n- did work\n"
    assert contains_header(entry)
    # And through the consumer that actually decides, which is where a
    # consolidation carrying only 12-hour entries used to be thrown away as
    # "purely conversational".
    assert _is_valid_consolidation(entry)


def test_prose_that_merely_mentions_a_time_is_not_a_header():
    """The cost of accepting 12-hour times, paid for deliberately.

    `## 9:00 AM standup notes` is an ordinary line for a model to write in a
    refusal, and accepting it as consolidated output would put chatter into
    permanent memory. The identity pipe is what separates a real entry from a
    heading that happens to start with a time — save-session.sh will not write
    an entry without one.
    """
    from pipeline.consolidate import _is_valid_consolidation

    refusal = "I cannot do this.\\n\\n## 9:00 AM standup notes\\n- a thing\\n"
    assert not contains_header(refusal)
    assert not _is_valid_consolidation(refusal)


def test_the_shell_fallback_matches_the_source_of_truth():
    """save-session.sh keeps a literal copy of the pattern as a fallback.

    That is deliberate — a failed lookup must not leave `grep -E` matching an
    empty pattern, which matches everything — but an unchecked literal copy is
    the same drift this file exists to prevent, just one level down. If
    TIME_ERE changes and the fallback does not, nothing else would notice.
    """
    text = SAVE_SH.read_text(encoding="utf-8")
    m = re.search(r"ENTRY_HEADER_ERE='([^']+)'", text)
    assert m, "the literal fallback in save-session.sh moved or changed shape"
    assert m.group(1) == ENTRY_HEADER_ERE, (
        "save-session.sh's fallback pattern has drifted from "
        f"pipeline/entry_header.py:\\n  fallback: {m.group(1)}\\n  canonical: "
        f"{ENTRY_HEADER_ERE}"
    )


def test_consolidation_still_recognises_day_and_week_headers():
    assert contains_header("## 2026-07-26\n\n- a day summary\n")
    assert contains_header("## Week of 2026-07-20\n\n- a week summary\n")


def test_neither_file_spells_the_pattern_out_again():
    """A second definition is the bug; drift is only its symptom."""
    for path in (SAVE_SH, CONSOLIDATE_PY):
        text = path.read_text(encoding="utf-8")
        # The literal fallback in save-session.sh is deliberate (a failed lookup
        # must not leave grep matching everything), so allow exactly one.
        occurrences = len(re.findall(r"\[0-9\]\{2\}:\[0-9\]\{2\}|\\d\{2\}:\\d\{2\}", text))
        assert occurrences <= 1, (
            f"{path.name} spells the entry-header pattern out {occurrences} times; "
            "it belongs in pipeline/entry_header.py"
        )


# ── The slug ─────────────────────────────────────────────────────────────────

def test_no_test_file_reimplements_the_slug():
    """Three test files each carried their own copy of the naive rule.

    They agreed only because no fixture used a non-ASCII or over-200-character
    path — the same "true today" that let the shipped copies drift.

    This catches the literal `re.sub(r"[^a-zA-Z0-9]", ...)` copy-paste, which is
    the form every historical duplicate took. It does NOT catch a hand-rolled
    equivalent — a loop over `isalnum()`, a reordered character class, a
    `str.translate` table — so it is a tripwire on the known path, not a proof
    that no second definition exists.
    """
    offenders = []
    for path in (REPO_ROOT / "tests").glob("test_*.py"):
        if path.name in {"test_slug_parity.py", "test_shared_rules.py"}:
            continue  # these two are ABOUT the rule
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            # Prose that quotes the old rule is not a copy of it — several
            # files legitimately explain what the slug used to be.
            if stripped.startswith(("#", '"""', "\'\'\'", "*")):
                continue
            if re.search(r"""re\.sub\(\s*r?["']\[\^a-zA-Z0-9\]["']""", line):
                offenders.append(path.name)
                break
    assert not offenders, (
        f"these test files reimplement the slug instead of importing "
        f"pipeline.slug: {offenders}"
    )


@posix_only
def test_run_tests_sh_uses_the_real_slug():
    """It built the session dir with an inline sed, safe only because
    `mktemp -d` yields ASCII."""
    text = RUN_TESTS_SH.read_text(encoding="utf-8")
    assert "session_dir_slug" in text, "run-tests.sh no longer uses the shared slug"
    assert "sed 's/[^a-zA-Z0-9]/-/g'" not in text, (
        "run-tests.sh still carries its own copy of the slug rule"
    )


# ── Thresholds ───────────────────────────────────────────────────────────────

def test_the_shipped_delta_default_matches_the_example_config():
    """`delta_lines_trigger` lives in log.sh as a config() fallback and in
    config.example.json as a shipped value. Raising the fallback failed the
    cooldown tests loudly; lowering it changed nothing and told nobody.

    The read moved out of post-tool-hook.sh in #350: that hook now replays the
    value log.sh resolved, because the fast path cannot call config(). The
    number it falls back to when nothing set the variable is checked here too —
    a second spelling of the default is exactly the drift this test exists for,
    and it arrived with the fix.
    """
    log_sh = (REPO_ROOT / "scripts" / "log.sh").read_text(encoding="utf-8")
    m = re.search(r'config\s+"\.thresholds\.delta_lines_trigger"\s+(\d+)', log_sh)
    assert m, "the delta_lines_trigger fallback moved or changed shape"
    hook = (REPO_ROOT / "scripts" / "post-tool-hook.sh").read_text(encoding="utf-8")
    replay = re.search(r'DELTA_THRESHOLD="\$\{REMEMBER_DELTA_THRESHOLD:-(\d+)\}"', hook)
    assert replay, (
        "post-tool-hook.sh no longer replays REMEMBER_DELTA_THRESHOLD in the "
        "shape this test can read — if it went back to calling config(), the "
        "#350 fast path is forking jq on every tool call again"
    )
    assert replay.group(1) == m.group(1), (
        "log.sh and post-tool-hook.sh disagree about the delta_lines_trigger "
        f"default ({m.group(1)} vs {replay.group(1)})"
    )
    shipped = json.loads((REPO_ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert int(m.group(1)) == shipped["thresholds"]["delta_lines_trigger"], (
        "log.sh's built-in default and config.example.json disagree about "
        "delta_lines_trigger — one of them is lying to whoever reads it"
    )


def test_the_shipped_save_cooldown_default_matches_everywhere_it_is_written():
    """The same rule for `cooldowns.save_seconds`, which #350 gave a fourth
    home.

    It is a config() fallback in log.sh and in save-session.sh, a replay
    fallback in post-tool-hook.sh, and a shipped value in config.example.json.
    Four numbers that must agree, none of which any runtime test can catch
    disagreeing: a cooldown that is wrong in one place suppresses or permits a
    save silently, which is what makes it worth pinning textually.
    """
    shipped = json.loads((REPO_ROOT / "config.example.json").read_text(encoding="utf-8"))
    expected = shipped["cooldowns"]["save_seconds"]

    found = {}
    for script in ("log.sh", "save-session.sh"):
        text = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
        m = re.search(r'config\s+"\.cooldowns\.save_seconds"\s+(\d+)', text)
        assert m, f"{script}: the save_seconds fallback moved or changed shape"
        found[script] = int(m.group(1))

    hook = (REPO_ROOT / "scripts" / "post-tool-hook.sh").read_text(encoding="utf-8")
    replay = re.search(r'SAVE_COOLDOWN="\$\{REMEMBER_SAVE_COOLDOWN:-(\d+)\}"', hook)
    assert replay, (
        "post-tool-hook.sh no longer replays REMEMBER_SAVE_COOLDOWN in the "
        "shape this test can read — if it went back to calling config(), the "
        "#350 fast path is forking jq on every tool call again"
    )
    found["post-tool-hook.sh"] = int(replay.group(1))

    disagreeing = {k: v for k, v in found.items() if v != expected}
    assert not disagreeing, (
        f"config.example.json ships save_seconds={expected}, but "
        f"{disagreeing} disagree — one of them is lying to whoever reads it"
    )


def test_the_two_header_patterns_stay_related():
    """ANY must be a superset of ENTRY: every session entry is a header."""
    for line in HEADERS_THAT_ARE_ENTRIES:
        assert re.search(ANY_HEADER_ERE, line), (
            f"{line!r} is a valid entry but not a recognised header — "
            "consolidation would read it as prose"
        )
