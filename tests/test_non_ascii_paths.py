"""session_dir_slug must agree with Claude Code for non-ASCII project paths.

Claude Code names ~/.claude/projects/<slug>/ with a JS regex that replaces one
CHARACTER per dash. A slug that disagrees points at a directory that does not
exist, the hook finds no transcript, and the entire pipeline no-ops for the
life of the session with nothing logged (issue #144).

Agreeing portably is the whole problem, because sed's idea of a "character"
follows the locale and every locale answer is wrong somewhere:

* ambient — byte-wise on Git Bash/MSYS even under LC_CTYPE=C.UTF-8, which is
  what the reporter hit: three dashes per CJK character;
* LC_ALL=C.utf8 — absent on macOS, and a missing locale falls back to C, which
  is byte-wise: the same bug, moved to a different platform;
* LC_ALL=en_US.UTF-8 — present on macOS, but then [a-z] follows collation and
  matches accented letters, so "café" keeps its é.

So the tests below run the real bash function under each hostile locale and
compare against the JS reference, which is UTF-16 code units — see _js_slug.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess assertions — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECT_SCRIPT = REPO_ROOT / "scripts" / "detect-tools.sh"

# The paths that matter: the reporter's CJK shape, accents (the case a pinned
# en_US.UTF-8 would break), an emoji (4-byte UTF-8), and plain ASCII as the
# guard that none of this changed the common path.
PATHS = [
    "/Users/f/Documents/dvsi",
    "/home/u/p",
    "/plain/ascii-123",
    "/tmp/café/x",
    "/tmp/accents-éàü/x",
    "/tmp/プロジェクト/bar",
    "/tmp/中文/项目",
    "/tmp/emoji-🎉-dir",
    "/tmp/𠮷野家/x",
    "/tmp/user@host.fr/p",
]

# Locales to force. "" means "whatever the runner has", which is the ambient
# case the reporter's host got wrong.
HOSTILE_LOCALES = ["", "C", "en_US.UTF-8", "C.UTF-8"]


def _js_slug(path: str) -> str:
    """The reference: what `s.replace(/[^a-zA-Z0-9]/g, '-')` actually produces.

    There is no /u flag on that regex in the shipped CLI, so it walks UTF-16
    code units. Every BMP character costs one dash; an astral one is a
    surrogate pair and costs two. Python strings are codepoint-based, so a
    plain re.sub() would quietly disagree on exactly the astral case — which is
    how the first version of this file managed to agree with a wrong fix.
    """
    return "".join(
        c if c.isascii() and c.isalnum() else "-" * (2 if ord(c) > 0xFFFF else 1)
        for c in path
    )


def _bash_slug(path: str, locale: str) -> str:
    """Call the real session_dir_slug under a forced locale."""
    export = f"export LC_ALL={locale}\n" if locale else "unset LC_ALL\n"
    script = f"""
    {export}source {DETECT_SCRIPT}
    session_dir_slug "$1"
    """
    result = subprocess.run(
        ["bash", "-c", script, "_", path],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"slug failed:\n{result.stderr}"
    return result.stdout.strip()


@pytest.mark.parametrize("locale", HOSTILE_LOCALES)
@pytest.mark.parametrize("path", PATHS)
def test_slug_matches_codepoint_reference_under_any_locale(path: str, locale: str):
    """One dash per character, whatever the ambient or forced locale is."""
    assert _bash_slug(path, locale) == _js_slug(path), (
        f"slug disagrees with Claude Code under LC_ALL={locale or '<ambient>'}; "
        f"the session directory would never be found (#144)"
    )


def test_slug_gives_one_dash_per_cjk_character():
    """The reporter's exact observation: 3 dashes per CJK char, not 1 (#144).

    Pinned as a literal because it is the number that made the difference
    between finding ~/.claude/projects/<slug>/ and silently never saving.
    """
    assert _bash_slug("/tmp/プロジェクト/bar", "C") == "-tmp--------bar"


def test_slug_replaces_accented_letters():
    """é must become a dash, not survive into the slug.

    Pinning this separately because it is what a pinned en_US.UTF-8 gets wrong:
    [a-z] under that locale's collation matches é and leaves it in place.
    """
    assert _bash_slug("/tmp/café/x", "en_US.UTF-8") == "-tmp-caf--x"


def test_ascii_paths_are_untouched_by_the_utf8_handling():
    """The common case must be byte-identical to the old behaviour."""
    for path in ("/Users/f/Documents/dvsi", "/home/u/p", "/plain/ascii-123"):
        assert _bash_slug(path, "") == _js_slug(path)


# ── The silent half of #144: nothing said when the slug matches nothing ──────

HOOK = REPO_ROOT / "scripts" / "post-tool-hook.sh"


def _run_hook_without_session_dir(tmp_path: Path):
    """Run the post-tool hook for a project whose session dir does not exist.

    That is precisely the state a mis-slugged non-ASCII path leaves behind: the
    directory Claude Code actually wrote is there, but under a different name,
    so the hook looks in a place with no transcript in it.
    """
    import os
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (home / ".claude" / "projects").mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    proc = subprocess.run(["bash", str(HOOK)], env=env,
                          capture_output=True, text=True, timeout=60)
    logs = "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in sorted((remember / "logs").glob("*.log")))
    return proc, remember, logs


def test_missing_session_dir_is_reported_not_silent(tmp_path):
    """A slug that matches no directory must say so (#144).

    This used to be a bare `exit 0`: memory simply never appeared, hook-errors
    stayed empty, and the only way to find out was to diff the computed slug
    against ~/.claude/projects/ by hand.
    """
    proc, _, logs = _run_hook_without_session_dir(tmp_path)
    assert proc.returncode == 0, f"hook must still exit 0:\n{proc.stderr}"
    assert "WARNING" in logs and "no session dir" in logs, (
        f"the hook exited silently on a slug that matches nothing:\n{logs}"
    )


def test_missing_session_dir_warning_is_logged_once(tmp_path):
    """The hook runs on every tool call — the warning must not repeat.

    A line per tool call would bury the logs it is meant to make readable.
    """
    _run_hook_without_session_dir(tmp_path)
    remember = tmp_path / "project" / ".remember"
    import os
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(tmp_path / "project"),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    for _ in range(3):
        subprocess.run(["bash", str(HOOK)], env=env,
                       capture_output=True, text=True, timeout=60)
    logs = "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in sorted((remember / "logs").glob("*.log")))
    assert logs.count("no session dir") == 1, (
        f"warning repeated on every tool call:\n{logs}"
    )


def test_missing_session_dir_warning_returns_after_its_ttl(tmp_path):
    """A persistent cause must keep surfacing, not warn once and go quiet.

    The cause here is environmental — a mis-slugged path stays mis-slugged — so
    a once-ever sentinel would fire in the first session and leave every later
    one as silent as before the fix. Ageing the marker past its TTL stands in
    for the next session an hour later.
    """
    import os
    _run_hook_without_session_dir(tmp_path)
    remember = tmp_path / "project" / ".remember"
    marker = remember / "tmp" / "no-transcript-notice"
    assert marker.exists(), "no marker written"
    marker.write_text("0")  # epoch 0: far older than the TTL

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(tmp_path / "project"),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    subprocess.run(["bash", str(HOOK)], env=env,
                   capture_output=True, text=True, timeout=60)
    logs = "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in sorted((remember / "logs").glob("*.log")))
    assert logs.count("no session dir") == 2, (
        f"warning did not return after its TTL — a persistent cause goes "
        f"silent forever:\n{logs}"
    )


def test_astral_characters_cost_two_dashes_not_one():
    """A surrogate pair is TWO code units to the JS regex, so two dashes.

    Verified against the shipped CLI's own regex under node. Getting this wrong
    is invisible on a Latin or BMP-CJK path and only bites the Extension-B
    kanji and emoji cases — which is the population this issue came from.
    """
    assert _bash_slug("/tmp/𠮷野家/x", "C") == "-tmp------x"
    assert _bash_slug("/tmp/emoji-🎉-dir", "C") == "-tmp-emoji----dir"


# ── Byte-level shapes, pinned against the real regex ────────────────────────
#
# These are the classes successive review rounds found the sed getting wrong,
# and none of them can be written as a Python str: they are byte sequences no
# decoder accepts, so they go in as raw argv bytes. The expected values were
# taken from the shipped regex run under node, which CI does not have — hence
# literals rather than a live oracle.

def _bash_slug_bytes(raw: bytes) -> str:
    """session_dir_slug on raw argv bytes, valid UTF-8 or not."""
    result = subprocess.run(
        ["bash", "-c",
         f"source {DETECT_SCRIPT}\nsession_dir_slug \"$1\"", b"_", raw],
        capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout.decode("utf-8", "replace").strip()


def test_embedded_newline_is_slugged():
    """A newline is legal in a POSIX filename and is sed's record separator.

    It never reaches an s/// rule, so it used to survive into the slug whole —
    a path with one could never match its session directory, which is #144's
    failure all over again on a byte that needs no exotic locale to produce.
    """
    assert _bash_slug_bytes(b"/Users/max/weird\nproject") == "-Users-max-weird-project"
    assert _bash_slug_bytes(b"/tmp/two\n\nnewlines/x") == "-tmp-two--newlines-x"


def test_malformed_utf8_costs_one_dash_per_byte():
    """What no decoder accepts is one replacement character per byte.

    The lead-byte ranges have to follow the well-formedness table for this: a
    blanket [\\302-\\364] class swallowed surrogate and overlong forms as if
    they were characters, and an unbounded continuation run swallowed the lone
    bytes after a valid sequence.
    """
    assert _bash_slug_bytes(b"/tmp/\xed\xa0\x80/x") == "-tmp-----x"    # surrogate
    assert _bash_slug_bytes(b"/tmp/\xe0\x80\x80/x") == "-tmp-----x"    # overlong 3-byte
    assert _bash_slug_bytes(b"/tmp/\xc0\x80/x") == "-tmp----x"         # overlong 2-byte
    assert _bash_slug_bytes(b"/tmp/\xf5\x80\x80\x80/x") == "-tmp------x"  # > U+10FFFF
    assert _bash_slug_bytes(b"/tmp/\x80/x") == "-tmp---x"              # lone continuation
    assert _bash_slug_bytes(b"/tmp/\xc2\x80\x80\x80/x") == "-tmp-----x"  # valid, then two lone
