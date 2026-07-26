"""A rejected summary must not look exactly like a legitimate SKIP.

`_is_non_summary` catches refusal-shaped replies — text opening "I'm sorry",
"Could you", "Please provide" — and folds them into `is_skip`. `save-session.sh`
then takes the SKIP branch: logs `SKIP — position → N`, advances the position,
clears the failure marker. Byte for byte what a genuinely empty span produces.

So a span of real work is discarded, permanently, and nothing anywhere records
that it happened. Memory quietly stops accumulating and every session start
looks normal.

It also slips past the safeguard built for exactly this. `max_summary_failures`
(#147: "losing every future span is worse") only counts a non-zero exit from
call-haiku. The reject gate returns successfully, so it never increments that
counter and never trips the give-up warning — this failure mode is invisible
because it resembles success.

The span still has to be dropped rather than retried forever, so the position
keeps advancing; what changes is that it is now visible and the text is kept.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from pipeline.haiku import _parse_response  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

from test_save_session_gates import _make_env, _run  # noqa: E402

REFUSAL = "I'm sorry, but I can't summarize this conversation without more context."
CLARIFY = "Could you provide the conversation you would like me to summarize?"


def _response(text: str) -> str:
    import json
    return json.dumps({"result": text, "input_tokens": 1, "output_tokens": 1,
                       "cache_read_input_tokens": 0})


def test_a_refusal_is_marked_rejected_not_merely_skipped():
    """The parsed result must say WHICH of the two happened."""
    rejected = _parse_response(_response(REFUSAL))
    assert rejected.is_skip, "a refusal must still be kept out of memory"
    assert rejected.is_rejected, "nothing distinguishes it from a model SKIP"


def test_a_model_skip_is_not_marked_rejected():
    """The ordinary SKIP must stay ordinary."""
    skipped = _parse_response(_response("SKIP"))
    assert skipped.is_skip
    assert not skipped.is_rejected, "a real SKIP was misreported as a rejection"


def test_a_clarifying_question_is_also_rejected():
    """The gate covers hedges, not just outright refusals."""
    assert _parse_response(_response(CLARIFY)).is_rejected


# The shell-level tests below drive the branch through the stub's IS_REJECTED,
# not by feeding refusal text. Feeding the text does NOT reach the reject gate:
# the stub stands in for pipeline.shell, so `_is_non_summary` never runs, the
# response arrives as an ordinary entry, and it is the malformed-HEADER path
# (#136) that quarantines it. Written that way these tests passed against
# completely unfixed code, proving a different fix than the one they name.
# test_the_real_pipeline_reports_a_refusal_as_rejected below closes the gap
# between this contract and the code that actually produces it.

def test_a_rejected_span_is_logged_distinctly(tmp_path):
    """The log must not read identically to a content-free session."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    env["STUB_HAIKU_REJECTED"] = "1"
    _run(plugin, env, sid)

    logs = "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in (project / ".remember" / "logs").glob("*.log"))
    assert "REJECTED" in logs, (
        f"a discarded span logged exactly like an empty one:\n{logs}"
    )


def test_a_rejected_span_is_kept_for_inspection(tmp_path):
    """Dropped from memory, but not destroyed — as malformed output already is."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    env["STUB_HAIKU_REJECTED"] = "1"
    env["STUB_HAIKU_TEXT"] = REFUSAL
    _run(plugin, env, sid)

    rejects = list((project / ".remember" / "tmp").glob("rejected-*.md"))
    assert rejects, "the discarded text was not kept anywhere"
    assert "I'm sorry" in rejects[0].read_text(encoding="utf-8")


def test_a_rejected_span_still_advances_the_position(tmp_path):
    """Otherwise the same span is retried forever — the #147 wedge."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5,
                                                 position=777)
    env["STUB_HAIKU_REJECTED"] = "1"
    _run(plugin, env, sid)

    import json
    saved = json.loads((project / ".remember" / "tmp" / "last-save.json").read_text())
    assert saved["line"] == 777


def test_a_genuine_skip_leaves_no_reject_trail(tmp_path):
    """The ordinary path must stay quiet: no warning, no file."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    env["STUB_HAIKU_SKIP"] = "1"
    _run(plugin, env, sid)

    logs = "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in (project / ".remember" / "logs").glob("*.log"))
    assert "REJECTED" not in logs, "a normal SKIP was reported as a rejection"
    assert not list((project / ".remember" / "tmp").glob("rejected-*.md"))


def test_the_real_pipeline_reports_a_refusal_as_rejected(tmp_path):
    """Bridge: the real emitter must speak the contract the stub encodes."""
    import io, contextlib
    from pipeline.shell import _emit_haiku_result

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _emit_haiku_result(_parse_response(_response(REFUSAL)))
    out = buf.getvalue()

    assert "IS_SKIP=true" in out, "a refusal must still be kept out of memory"
    assert "IS_REJECTED=true" in out, "bash cannot tell this from a model SKIP"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _emit_haiku_result(_parse_response(_response("SKIP")))
    assert "IS_REJECTED=false" in buf.getvalue()
