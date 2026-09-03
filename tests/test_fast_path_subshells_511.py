"""#511 -- user-prompt-hook.sh's warm path still forks a bash subshell for
every `$( ... )` command substitution it runs, even where the thing inside
never shells out to an external process. That fork is cheap on Linux and
50-300ms+ on Windows Git Bash per the reporter's measurement (see the #227
COST section this file's header restates) -- and it fires on every single
prompt, not just a cold one.

No Windows box is available to this lane -- every claim about actual
milliseconds saved is REASONED, not observed. What is observed, directly,
is the textual subshell count on the warm path and the byte-for-byte parity
between the old print-and-capture extractor and its new set-a-variable
sibling.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ._bash_runner import resolve_bash

BASH = resolve_bash()
import pytest

# Per-test, not module-wide (self-review / audit finding): a module-level
# `pytestmark` skips every test in this file if no bash is found, including
# the two pure-source-text checks at the bottom
# (test_stdin_cwd_is_no_longer_captured_through_a_subshell and its sibling),
# which read the script file and grep it -- no subprocess, no bash binary
# needed at all. Under the module-wide form, a leg where resolve_bash()
# returns None would silently drop exactly the two regression guards this
# issue's own commit message leans on hardest, alongside the ones that
# genuinely need a shell. `_needs_bash` below is applied only to the tests
# that actually invoke BASH via subprocess.
_needs_bash = pytest.mark.skipif(BASH is None, reason="no usable bash found")

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "user-prompt-hook.sh"
LIB_CLOCK = REPO_ROOT / "scripts" / "lib-clock.sh"


def _extract_function(text: str, func_name: str) -> str:
    m = re.search(rf"^{re.escape(func_name)}\(\)\s*\{{", text, re.MULTILINE)
    assert m, func_name + " not found"
    depth = 0
    for j in range(m.end() - 1, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[m.start() : j + 1]
    raise AssertionError("unbalanced braces extracting " + func_name)


# ── Parity: the old print-and-capture extractor and the new set-a-variable
# one must agree on every input, or the fast path silently starts resolving
# a different project root than the one the pinned #447 characterization
# test exercises. ──────────────────────────────────────────────────────────

CASES = [
    ('{"cwd":"/some/project"}', "/some/project"),
    # Nested BEFORE the top-level key -- the documented #447 gap: first
    # occurrence in the raw text wins, and that is the nested one here.
    ('{"tool":{"cwd":"/nested"},"cwd":"/top-level"}', "/nested"),
    # Nested AFTER the top-level key -- the common, safe shape: top-level wins.
    ('{"cwd":"/top-level","tool":{"cwd":"/nested"}}', "/top-level"),
    ("no cwd here at all", None),
    ('{"cwd":""}', None),
]


@_needs_bash
@pytest.mark.parametrize("raw, expected", CASES)
def test_stdin_cwd_into_agrees_with_stdin_cwd(raw, expected):
    text = HOOK.read_text(encoding="utf-8")
    old_fn = _extract_function(text, "_stdin_cwd")
    new_fn = _extract_function(text, "_stdin_cwd_into")

    script = (
        old_fn + "\n" + new_fn + "\n"
        + 'RAW=$STDIN_CWD_TEST_RAW\n'
        + 'OLD=$(_stdin_cwd "$RAW" 2>/dev/null); OLD_RC=$?\n'
        + '_stdin_cwd_into NEW_VAR "$RAW"; NEW_RC=$?\n'
        + 'echo "OLD_RC=$OLD_RC OLD=$OLD"\n'
        + 'echo "NEW_RC=$NEW_RC NEW=$NEW_VAR"\n'
    )
    # RAW travels through an environment variable rather than argv: on
    # Windows, subprocess.run's argv list is re-flattened into a single
    # command line by Python's own MSVCRT-style quoting rules
    # (subprocess.list2cmdline) before CreateProcess hands it to bash.exe,
    # and MSYS's argv re-parsing of that line does not agree with it on a
    # raw string containing embedded double quotes (every CASES entry
    # above but the no-cwd ones does) -- the quotes can be stripped before
    # $1 ever reaches the function under test, so `"cwd"` no longer
    # literally appears in $1 and both extractors report a miss. An
    # environment variable is passed as an already-decoded block, with no
    # command-line re-quoting step in between, so it sidesteps the whole
    # cross-runtime quoting question. REASONED: no Windows box available to
    # confirm this mechanism directly; test_env_cache_key_windows_normalize_504.py
    # in this same commit hits an adjacent but distinct Windows subprocess
    # hazard (bash resolving to the WSL launcher stub) at the same review pass.
    env = dict(os.environ)
    env["STDIN_CWD_TEST_RAW"] = raw
    result = subprocess.run(
        [BASH, "-c", script], env=env, capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    old_rc = int(lines[0].split()[0].split("=")[1])
    old_val = lines[0].split("OLD=", 1)[1]
    new_rc = int(lines[1].split()[0].split("=")[1])
    new_val = lines[1].split("NEW=", 1)[1]

    if expected is None:
        assert old_rc != 0, "harness expected a miss but _stdin_cwd hit: " + repr(lines)
        assert new_rc != 0, "_stdin_cwd_into diverges from _stdin_cwd: hit where the old one missed"
    else:
        assert old_rc == 0 and old_val == expected, repr(lines)
        assert new_rc == 0 and new_val == expected, (
            "_stdin_cwd_into diverges from _stdin_cwd for " + repr(raw) + ": "
            + repr(lines)
        )


# ── _remember_date_into parity with _remember_date ──────────────────────────

@_needs_bash
def test_remember_date_into_matches_remember_date_builtin_path():
    script = (
        f'source "{LIB_CLOCK}"\n'
        'OLD=$(_remember_date "+%H:%M")\n'
        '_remember_date_into NEW "+%H:%M"\n'
        'echo "OLD=$OLD"\n'
        'echo "NEW=$NEW"\n'
    )
    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    lines = {l.split("=", 1)[0]: l.split("=", 1)[1] for l in result.stdout.splitlines()}
    # A minute boundary crossed between the two calls is the one legitimate
    # way this could differ without a bug -- tolerate that one case by
    # retrying rather than asserting a `... or True` that would never fail
    # in the first place (self-review finding): only mismatch reruns below,
    # and re-running immediately makes a boundary-crossing false pass
    # implausible -- both calls happen back to back, and if they ever
    # disagree it must be only in the last digit of a minute crossing, which
    # a second run at the same instant would not reproduce twice.
    if lines["OLD"] != lines["NEW"]:
        result2 = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=10, check=False)
        lines2 = {l.split("=", 1)[0]: l.split("=", 1)[1] for l in result2.stdout.splitlines()}
        assert lines2["OLD"] == lines2["NEW"], (lines, lines2)


@_needs_bash
def test_remember_date_into_matches_remember_date_with_tz():
    script = (
        f'source "{LIB_CLOCK}"\n'
        'REMEMBER_TZ=UTC\n'
        'OLD=$(_remember_date "+%Y-%m-%d %H:%M %Z")\n'
        '_remember_date_into NEW "+%Y-%m-%d %H:%M %Z"\n'
        'echo "OLD=$OLD"\n'
        'echo "NEW=$NEW"\n'
    )
    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    lines = {l.split("=", 1)[0]: l.split("=", 1)[1] for l in result.stdout.splitlines()}
    assert lines["OLD"] == lines["NEW"], lines


# ── The two subshells #511 removes must actually be gone from the source ───
#
# A blanket count of every `$(` in the file is not the right measure: most of
# them are either comments describing the change (this very file quotes
# `$(_remember_date ...)` in prose) or code on a path that is not the common
# warm one at all (`_notice_body=$(cat ...)` only runs when a notice file
# exists; the `whoami` fallback only runs when neither $USER nor $USERNAME is
# set). Asserting on the two call sites this issue actually changed is a more
# honest characterization than a regex that would also flag its own comments.

def test_stdin_cwd_is_no_longer_captured_through_a_subshell():
    text = HOOK.read_text(encoding="utf-8")
    assert "_stdin_cwd_into REMEMBER_HOOK_CWD" in text, (
        "the call site should use the set-a-variable extractor, not "
        "`$(_stdin_cwd ...)`"
    )
    assert "$(_stdin_cwd " not in text, (
        "a `$(_stdin_cwd ...)` command substitution is still present -- #511 "
        "meant to remove the subshell fork this pays for every prompt"
    )


def test_remember_date_is_no_longer_captured_through_a_subshell_on_the_fast_path():
    text = HOOK.read_text(encoding="utf-8")
    assert text.count("_remember_date_into _REMEMBER_NOW") == 2, (
        "expected both stamp branches (with and without CTX_PCT) to use the "
        "set-a-variable form"
    )
    code_lines = [l for l in text.splitlines() if not l.strip().startswith("#")]
    assert not any("$(_remember_date " in l for l in code_lines), (
        "a `$(_remember_date ...)` command substitution is still present in "
        "user-prompt-hook.sh's code (not just a comment) -- #511 meant to "
        "remove the subshell fork this pays for every prompt on the common "
        "(no REMEMBER_TZ, bash >= 4.2) path"
    )
