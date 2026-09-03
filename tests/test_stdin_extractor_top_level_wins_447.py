"""The #344 top-level-wins pin generalised to the extractor, not one key (#447).

#344 filed the "first `"KEY"` occurrence wins, not first *top-level*
occurrence" heuristic against `source` alone. #379 pinned it with
`tests/test_stdin_source_top_level_wins_344.py`, keyed to `source` and
routed through a full `session-start-hook.sh` run (the recap injection is
the observable there).

Since #444, the same extractor -- `_stdin_json_string` in
session-start-hook.sh, session-end-hook.sh and post-tool-hook.sh, and its
`cwd`-hardcoded twin `_stdin_cwd` in user-prompt-hook.sh -- resolves `cwd`
on all four hooks and `session_id` on three of them, and neither key
carries the whitelist that bounded #344's impact for `source`. This file
is #447's answer to the "what would settle it" the issue poses: the pin
generalises to the extractor across every (hook, key) pair that reads
through it, rather than staying keyed to `source`.

This is a CHARACTERIZATION of the extractor's mechanism, exactly as #379
was -- it does not change `_stdin_json_string` or `_stdin_cwd` (#447 says
not to reach for a JSON parser, and #340/#344 already ruled that out for
the same reason: a hook that must survive a broken install is the wrong
place to acquire one). Each (hook, key) pair gets the same two-test shape
#379 established:

  - nested key AFTER the top-level one (the common, safe shape): the
    top-level value wins. This must keep holding.
  - nested key BEFORE the top-level one (the extractor's own gap, on a
    synthetic payload no known host produces -- see #494 below): the nested
    value wins today. Pinned so a change to the scan is a visible,
    deliberate decision rather than a silent shift in which direction is
    unsafe.

#494 -- REACHABILITY, researched rather than assumed. Filed after this pin
landed, asking whether any of the three hosts' hook payloads can actually
put a `cwd`-bearing nested object ahead of the top-level `cwd` field. The
answer, read from each host's own schema (docs for Claude Code and Gemini
CLI, source for Codex -- see the block above `_stdin_cwd()` in
scripts/user-prompt-hook.sh for the full citation trail): no. `cwd` sits in
the shared top-level object on all three, and the only nested object a hook
payload ever carries (`tool_input`/`tool_response`) is declared AFTER it in
every schema checked -- Codex's is source-verified (serde struct order),
Claude Code's and Gemini's are docs-observed only (neither serializer is
open source). Even a third-party MCP tool naming one of its own parameters
`cwd` still lands inside `tool_input`, which is still positioned after the
top-level field -- the safe "nested-after" case, never "nested-before".

That settles TODAY's shipped hosts, not the future: a host is free to
reorder its own schema, and this repo has no way to be notified when one
does. So `test_nested_key_ahead_of_top_level_is_the_documented_gap` below
stays a characterization of the extractor's OWN mechanism against a
synthetic payload, not a live gap -- it is not flipped to a hard assertion
that the shape can never occur, because that would be a claim about
software this repo does not control. Read it as "no known host reaches
this today", not as "this cannot happen".

Tested directly against the extracted function body rather than through a
full hook run: `_stdin_json_string`/`_stdin_cwd` take two plain strings and
return a string or a nonzero exit, so driving them through the whole
recap-injection or path-resolution machinery each of the four hooks builds
on top of them would test that machinery too, not just the extractor. The
function bodies are pulled out of the actual script files (never
retyped), so a change to the real extractor is what this test exercises --
not a hand-copied stand-in that could drift from it.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from ._bash_runner import resolve_bash

# #432: a blanket skipif(sys.platform == "win32") makes the windows-latest CI
# leg collect these tests, skip every one of them, and report the leg green --
# a check that never ran rendering exactly like a check that found nothing.
# tests/test_hooks_json.py already proves a real bash is reachable under Git
# Bash on that same leg, so the platform is not the limitation; narrow the
# skip to the one thing that actually is: no usable bash on PATH at all.
BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found (checked PATH, then Git-for-Windows install locations)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _extract_function(script: str, func_name: str) -> str:
    """Pull `func_name() { ... }` verbatim out of `script`, brace-balanced."""
    text = (REPO_ROOT / script).read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(func_name)}\(\)\s*\{{", text, re.MULTILINE)
    assert m, func_name + " not found in " + script
    depth = 0
    for j in range(m.end() - 1, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[m.start() : j + 1]
    raise AssertionError("unbalanced braces extracting " + func_name + " from " + script)


def _call(script: str, func_name: str, keyed: bool, key: str, raw: str):
    """Run the extracted function in a fresh bash, return (returncode, stdout)."""
    body = _extract_function(script, func_name)
    args = [key, raw] if keyed else [raw]
    call = func_name + " " + " ".join(shlex.quote(a) for a in args)
    result = subprocess.run(
        [BASH, "-c", body + "\n" + call],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode, result.stdout


def _nested_after(key: str, top: str, nested: str) -> str:
    """Common shape: top-level `key` written first, a same-named key nested
    inside some other field comes later in the joined stdin string."""
    return "{" + json.dumps(key) + ":" + json.dumps(top) + ',"tool":{' + json.dumps(key) + ":" + json.dumps(nested) + "}}"


def _nested_before(key: str, top: str, nested: str) -> str:
    """The documented gap: the nested `key` occurs in the raw stdin BEFORE
    the top-level one."""
    return '{"tool":{' + json.dumps(key) + ":" + json.dumps(nested) + "}," + json.dumps(key) + ":" + json.dumps(top) + "}"


# Every (script, function, keyed-call, key) triple that reads through the
# first-occurrence extractor today. `keyed` distinguishes
# `_stdin_json_string KEY RAW` (session-start/-end, post-tool) from
# user-prompt-hook.sh's own `_stdin_cwd RAW`, which hardcodes the key.
CASES = [
    pytest.param("scripts/session-start-hook.sh", "_stdin_json_string", True, "source", id="session-start/source"),
    pytest.param("scripts/session-start-hook.sh", "_stdin_json_string", True, "cwd", id="session-start/cwd"),
    pytest.param("scripts/session-start-hook.sh", "_stdin_json_string", True, "session_id", id="session-start/session_id"),
    pytest.param("scripts/session-end-hook.sh", "_stdin_json_string", True, "cwd", id="session-end/cwd"),
    pytest.param("scripts/session-end-hook.sh", "_stdin_json_string", True, "session_id", id="session-end/session_id"),
    pytest.param("scripts/post-tool-hook.sh", "_stdin_json_string", True, "cwd", id="post-tool/cwd"),
    pytest.param("scripts/post-tool-hook.sh", "_stdin_json_string", True, "session_id", id="post-tool/session_id"),
    pytest.param("scripts/user-prompt-hook.sh", "_stdin_cwd", False, "cwd", id="user-prompt/cwd"),
]


@pytest.mark.parametrize("script, func_name, keyed, key", CASES)
def test_top_level_wins_when_nested_key_comes_after(script, func_name, keyed, key):
    raw = _nested_after(key, top="TOP-VALUE-447", nested="NESTED-VALUE-447")
    rc, out = _call(script, func_name, keyed, key, raw)
    assert rc == 0, script + "/" + func_name + " rejected a well-formed payload: " + out
    assert out == "TOP-VALUE-447", (
        script + "/" + func_name + "(" + key + ") stopped preferring the top-level "
        "occurrence when a same-named nested key comes after it -- got " + repr(out)
    )


@pytest.mark.parametrize("script, func_name, keyed, key", CASES)
def test_nested_key_ahead_of_top_level_is_the_documented_gap(script, func_name, keyed, key):
    raw = _nested_before(key, top="TOP-VALUE-447", nested="NESTED-VALUE-447")
    rc, out = _call(script, func_name, keyed, key, raw)
    assert rc == 0, script + "/" + func_name + " rejected a well-formed payload: " + out
    assert out == "NESTED-VALUE-447", (
        script + "/" + func_name + "(" + key + ") is characterized as taking the FIRST "
        "occurrence in the joined stdin string, not the first top-level one -- this test "
        "pins that today's mechanism still does, so a fix to the scan is a visible, "
        "deliberate decision (got " + repr(out) + ", expected the nested value to win as "
        "documented). #494: this payload shape is synthetic -- no known host schema "
        "nests a same-named key ahead of the top-level one (see the module docstring)."
    )


def test_a_key_with_no_nested_shadow_is_unaffected():
    """Positive control for the two tests above: when there is no nested
    same-named key at all, the extractor must still resolve the ordinary
    top-level value -- the pairing above must not be trivially satisfied by
    an extractor that always returns the wrong thing."""
    raw = '{"session_id":"PLAIN-VALUE-447","cwd":"/does/not/matter"}'
    rc, out = _call("scripts/session-start-hook.sh", "_stdin_json_string", True, "session_id", raw)
    assert rc == 0
    assert out == "PLAIN-VALUE-447"
