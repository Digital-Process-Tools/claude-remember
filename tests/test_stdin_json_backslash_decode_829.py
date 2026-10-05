r"""#829: the hook stdin extractors must decode JSON's `\\` escape.

Codex never publishes CLAUDE_PROJECT_DIR (#463), so on Windows the project
directory comes from the payload's `cwd`, which a JSON encoder writes as
`"C:\\work\\proj"`. Returned undecoded, every separator arrives doubled, and
`pipeline.slug.session_dir_slug` turns each one into two dashes --
`c---work--proj` instead of `c--work-proj` -- so a Codex-hosted
session-start-hook.sh's recovery force-save looked for the previous Claude
Code transcript in a directory that does not exist (observed on Windows 11
under Codex Desktop, reproduced against main in #829).

Every extractor variant is exercised, pulled verbatim out of its script, with
payloads produced by a real JSON encoder rather than hand-escaped strings.
"""

from __future__ import annotations

import json
import shlex
import subprocess

import pytest

from ._bash_runner import resolve_bash
from .test_stdin_extractor_top_level_wins_447 import _call, _extract_function

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found (checked PATH, then Git-for-Windows install locations)",
)

WIN_CWD = "C:\\work\\proj"
UNC_CWD = "\\\\server\\share\\proj"
POSIX_CWD = "/home/dev/proj"


def _call_into(script: str, func_name: str, keyed: bool, key: str, raw: str):
    """`_call` for the `printf -v` variants: `FUNC VAR [key] raw`, then print VAR."""
    body = _extract_function(script, func_name)
    args = ["OUT_829"] + ([key] if keyed else []) + [raw]
    call = func_name + " " + " ".join(shlex.quote(a) for a in args) + '; printf %s "$OUT_829"'
    result = subprocess.run(
        [BASH],
        input=body + "\n" + call,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode, result.stdout


# (script, function, keyed, printf -v variant)
EXTRACTORS = [
    pytest.param("scripts/session-start-hook.sh", "_stdin_json_string_into", True, True, id="session-start/into"),
    pytest.param("scripts/session-end-hook.sh", "_stdin_json_string", True, False, id="session-end"),
    pytest.param("scripts/post-tool-hook.sh", "_stdin_json_string", True, False, id="post-tool"),
    pytest.param("scripts/user-prompt-hook.sh", "_stdin_cwd", False, False, id="user-prompt"),
    pytest.param("scripts/user-prompt-hook.sh", "_stdin_cwd_into", False, True, id="user-prompt/into"),
]


def _extract_cwd(script, func_name, keyed, into, cwd):
    raw = json.dumps({"session_id": "s-829", "cwd": cwd})
    run = _call_into if into else _call
    rc, out = run(script, func_name, keyed, "cwd", raw)
    assert rc == 0, script + "/" + func_name + " rejected a well-formed payload: " + out
    return out


@pytest.mark.parametrize("script, func_name, keyed, into", EXTRACTORS)
def test_windows_cwd_is_decoded(script, func_name, keyed, into):
    out = _extract_cwd(script, func_name, keyed, into, WIN_CWD)
    assert out == WIN_CWD, (
        script + "/" + func_name + " returned the JSON-escaped form " + repr(out)
        + " instead of the path it encodes, " + repr(WIN_CWD)
    )


@pytest.mark.parametrize("script, func_name, keyed, into", EXTRACTORS)
def test_unc_prefix_decodes_pairwise(script, func_name, keyed, into):
    r"""A UNC `\\server` is `\\\\server` in JSON text: two escapes, not one."""
    assert _extract_cwd(script, func_name, keyed, into, UNC_CWD) == UNC_CWD


@pytest.mark.parametrize("script, func_name, keyed, into", EXTRACTORS)
def test_posix_cwd_is_unchanged(script, func_name, keyed, into):
    """Positive control: a value with no escapes still comes back verbatim."""
    assert _extract_cwd(script, func_name, keyed, into, POSIX_CWD) == POSIX_CWD
