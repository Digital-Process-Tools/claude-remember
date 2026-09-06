"""A non-English refusal must not pass the NDC reject gate either (#597).

`DEFAULT_REJECT_PATTERN` (pipeline/haiku.py) is anchored to English refusal
stems ("i cannot", "could you", "i'm sorry", ...). #593 put the model's reply
in the *conversation's own language* into `now.md` in ordinary use for the
first time, so a refusal in Chinese, French, Spanish, German or Japanese comes
back with `IS_REJECTED=false` -- the auditor exercised this directly against
`_parse_response` and confirmed it.

On the NDC compression branch, `IS_REJECTED` was the ONLY content gate
(test_ndc_reject_gate.py covers that branch's English case). A missed
non-English refusal is appended to `today-*.md` as though it were a genuine
day summary, and `now.md` is then truncated over the entries that were
supposed to have been compressed -- gone, with no copy anywhere, since
`keep_rejected_text` is never called on that branch.

The fix adds a second, language-independent gate: a genuine NDC reply always
opens with a "## " header (compress-ndc.prompt.txt requires it be preserved),
and no refusal in any language does. This file drives that gate the same way
test_ndc_reject_gate.py drives the first one: through the STUB_NDC_TEXT /
STUB_NDC_REJECTED knobs, feeding text a real (unpatched) `_is_non_summary`
would have missed -- IS_REJECTED is deliberately left false/unset, which is
exactly what the real gate does for non-English text.

Positive control alongside the two "must not fire" cases: a genuine
non-English SUMMARY (still opening with "## ", still real compressed content)
must still stage and still truncate now.md -- otherwise the assertion would
also pass on a gate that rejects everything.
"""

import sys

import pytest

from .subprocess_helpers import subprocess_failure_detail
from .test_ndc_reject_gate import _ndc_env, _wait_for_calls_to_settle
from .test_save_session_gates import _run

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout -- not portable to Windows runners (#79)",
)

# None of these open with "i cannot" / "could you" / "i'm sorry" / etc, so
# DEFAULT_REJECT_PATTERN's English-only anchors would genuinely miss every
# one of them -- this is not a synthetic string chosen to dodge the pattern,
# it is what the pattern's own anchors are blind to.
NON_ENGLISH_REFUSAL_ZH = "抱歉,我无法在没有更多上下文的情况下总结此对话。\n"
NON_ENGLISH_REFUSAL_FR = (
    "Je suis désolé, mais je ne peux pas résumer cette conversation "
    "sans plus de contexte.\n"
)
GENUINE_NON_ENGLISH_SUMMARY = "## 10:00 | main\n\n- 修复了缓存问题; 更新了配置\n"


class TestNdcNonEnglishRefusalIsRejectedNotAppended:
    """The gap #593 opened: a non-English refusal reaches today-*.md unfiltered."""

    def test_chinese_refusal_does_not_reach_today_md(self, tmp_path):
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        env["STUB_NDC_TEXT"] = NON_ENGLISH_REFUSAL_ZH
        # Left unset/false on purpose: this is what the REAL gate does for
        # non-English text today, per pipeline.haiku._parse_response.

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        _wait_for_calls_to_settle(calls)

        today_files = list((project / ".remember").glob("today-*.md"))
        written = "".join(f.read_text(encoding="utf-8") for f in today_files)
        assert NON_ENGLISH_REFUSAL_ZH.strip() not in written, (
            f"a Chinese refusal reached today-*.md as though it were a real "
            f"summary: {written!r}"
        )

    def test_french_refusal_does_not_reach_today_md(self, tmp_path):
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        env["STUB_NDC_TEXT"] = NON_ENGLISH_REFUSAL_FR

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        _wait_for_calls_to_settle(calls)

        today_files = list((project / ".remember").glob("today-*.md"))
        written = "".join(f.read_text(encoding="utf-8") for f in today_files)
        assert NON_ENGLISH_REFUSAL_FR.strip() not in written, (
            f"a French refusal reached today-*.md as though it were a real "
            f"summary: {written!r}"
        )

    def test_chinese_refusal_leaves_now_md_intact(self, tmp_path):
        """The #597 half that made this `destroys`: entries must not be lost."""
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        env["STUB_NDC_TEXT"] = NON_ENGLISH_REFUSAL_ZH

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        _wait_for_calls_to_settle(calls)

        now_md = project / ".remember" / "now.md"
        assert now_md.is_file() and now_md.stat().st_size > 0, (
            "now.md was truncated despite a missed non-English refusal -- "
            "the entries it held are gone with no copy anywhere"
        )
        assert "did some work" in now_md.read_text(encoding="utf-8"), (
            "the entry written by the main summarize call is gone from now.md"
        )

    def test_chinese_refusal_is_logged_as_rejected(self, tmp_path):
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        env["STUB_NDC_TEXT"] = NON_ENGLISH_REFUSAL_ZH

        _run(plugin, env, sid)
        _wait_for_calls_to_settle(calls)

        logs = "".join(
            p.read_text(encoding="utf-8", errors="replace")
            for p in (project / ".remember" / "logs").glob("*.log")
        )
        assert "ndc" in logs and "REJECTED" in logs, (
            f"no REJECTED line logged for the missed Chinese refusal:\n{logs}"
        )


class TestGenuineNonEnglishSummaryStillStages:
    """The positive control: the new gate must not reject everything."""

    def test_genuine_non_english_summary_still_reaches_today_md(self, tmp_path):
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        env["STUB_NDC_TEXT"] = GENUINE_NON_ENGLISH_SUMMARY

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        _wait_for_calls_to_settle(calls)

        today_files = list((project / ".remember").glob("today-*.md"))
        written = "".join(f.read_text(encoding="utf-8") for f in today_files)
        assert "修复了缓存问题" in written, (
            f"a genuine non-English compression was rejected along with the "
            f"refusals -- the gate is too strict, not just missing: {written!r}"
        )

    def test_genuine_non_english_summary_still_truncates_now_md(self, tmp_path):
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        env["STUB_NDC_TEXT"] = GENUINE_NON_ENGLISH_SUMMARY

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        content = _wait_for_calls_to_settle(calls)
        assert content, "sanity: the stub actually ran"

        import time
        deadline = time.monotonic() + 10
        now_md = project / ".remember" / "now.md"
        last = None
        while time.monotonic() < deadline:
            last = now_md.read_text(encoding="utf-8") if now_md.exists() else ""
            if "did some work" not in last:
                break
            time.sleep(0.2)
        assert "did some work" not in last, (
            "a genuine compression must still remove the compressed span "
            "from now.md, or the fix has turned into a permanent no-op"
        )
