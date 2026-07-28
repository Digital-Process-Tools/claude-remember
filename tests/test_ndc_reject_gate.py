"""NDC compression must gate on the model's verdict, not just non-empty text
(review of 8d2cdab).

`save-session.sh`'s NDC block runs its own Haiku call through the exact same
IS_SKIP/IS_REJECTED contract as the main summarize call, but until this fix it
never read the verdict: it appended to today-*.md whenever `NDC_TEXT` was
non-empty. A refusal is non-empty text, so
"I'm sorry, but I can't summarize this conversation without more context."
got written into today-*.md as if it were a genuine day summary, with no log
line — not lost memory, corrupted memory, permanently on record.

The fix adds a reject branch mirroring the one the main summarize path already
had (#136/#reject-gate-visibility), and resets IS_SKIP/IS_REJECTED right
before parsing the NDC response — the subshell inherits whatever those
variables held from the parent's own (already-consumed) summarize call, and a
stale value must not be allowed to decide this gate.
"""

import json
import sys
from pathlib import Path

import pytest

from .test_save_session_gates import _make_env, _run, REPO_ROOT
from .subprocess_helpers import subprocess_failure_detail

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REFUSAL = "I'm sorry, but I can't summarize this conversation without more context.\n"

# Same STUB_SHELL as test_save_session_gates, extended with independent control
# over the NDC call's response (STUB_NDC_TEXT / STUB_NDC_REJECTED / STUB_NDC_SKIP).
# The original stub always sent a valid "## 2026-07-25 ..." summary for the NDC
# call and could not fail it — the reject gate literally could not be exercised.
STUB_SHELL_NDC = '''\
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
    with open(sys.argv[6], "w") as f:
        f.write("a prompt with no placeholders\\n")
elif cmd == "call-haiku":
    if os.environ.get("STUB_HAIKU_FAIL") == "1":
        sys.stderr.write("stub: simulated haiku failure\\n")
        sys.exit(1)
    is_ndc = len(sys.argv) > 3
    if is_ndc and os.environ.get("STUB_APPEND_DURING_NDC"):
        with open(os.environ["STUB_MEMORY_FILE"], "a") as f:
            f.write(os.environ["STUB_APPEND_DURING_NDC"])
    fd, path = tempfile.mkstemp(suffix="-haiku")
    with os.fdopen(fd, "w") as f:
        if is_ndc:
            f.write(os.environ.get(
                "STUB_NDC_TEXT", "## 2026-07-25\\n\\n- compressed summary\\n"))
        elif os.environ.get("STUB_HAIKU_TEXT"):
            f.write(os.environ["STUB_HAIKU_TEXT"])
        elif os.environ.get("STUB_HAIKU_SKIP", "1") == "1":
            f.write("SKIP\\n")
        else:
            f.write("## 10:00 | main\\n\\n- did some work\\n")
    if is_ndc:
        rejected = os.environ.get("STUB_NDC_REJECTED") == "1"
        skipping = os.environ.get("STUB_NDC_SKIP") == "1"
    else:
        rejected = os.environ.get("STUB_HAIKU_REJECTED") == "1"
        skipping = (os.environ.get("STUB_HAIKU_SKIP", "1") == "1"
                    and not os.environ.get("STUB_HAIKU_TEXT"))
    print("IS_SKIP=" + ("true" if (rejected or skipping) else "false"))
    print("IS_REJECTED=" + ("true" if rejected else "false"))
    print(f"HAIKU_TEXT_FILE={path}")
    tk = os.environ.get("STUB_NDC_TOKENS") if is_ndc else None
    if tk:
        tin, tout, tcache, tcost = tk.split(",")
        print(f"TK_IN={tin}"); print(f"TK_OUT={tout}")
        print(f"TK_CACHE={tcache}"); print(f"TK_COST={tcost}")
    else:
        print("TK_IN=0"); print("TK_OUT=0"); print("TK_CACHE=0"); print("TK_COST=0")
elif cmd == "build-ndc-prompt":
    with open(sys.argv[3], "w") as f:
        f.write("compress this now.md\\n")
'''


def _ndc_env(tmp_path: Path, **kwargs):
    """Harness env with NDC enabled (0 cooldown) and the response-gate stub."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5, **kwargs)
    # Real content from the main summarize call, so it reaches step 7/8
    # instead of taking the SKIP exit at step 6.
    env["STUB_HAIKU_SKIP"] = "0"
    (plugin / "pipeline" / "shell.py").write_text(STUB_SHELL_NDC)
    for cfg_path in (Path(env["REMEMBER_CONFIG"]), plugin / "config.json"):
        cfg = json.loads(cfg_path.read_text())
        cfg["cooldowns"]["ndc_seconds"] = 0
        cfg_path.write_text(json.dumps(cfg))
    return env, project, plugin, calls, sid


def _wait_for_calls_to_settle(calls_log: Path, timeout: float = 30) -> str:
    """The NDC subshell is disowned, so poll the calls log until it stops growing."""
    import time
    deadline = time.monotonic() + timeout
    last = None
    stable_since = None
    while time.monotonic() < deadline:
        current = calls_log.read_text() if calls_log.exists() else ""
        if current == last:
            if stable_since and time.monotonic() - stable_since > 1.0:
                return current
            stable_since = stable_since or time.monotonic()
        else:
            last, stable_since = current, None
        time.sleep(0.2)
    raise TimeoutError("NDC subshell never settled")


class TestNdcRefusalIsRejectedNotAppended:
    """The exact defect: a refusal is non-empty text, so the old `[ -n
    "$NDC_TEXT" ]` gate alone appended it into today-*.md as a day summary."""

    def test_refusal_does_not_reach_today_md(self, tmp_path):
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        env["STUB_NDC_TEXT"] = REFUSAL
        env["STUB_NDC_REJECTED"] = "1"

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        _wait_for_calls_to_settle(calls)

        today_files = list((project / ".remember").glob("today-*.md"))
        written = "".join(f.read_text() for f in today_files)
        assert REFUSAL.strip() not in written, (
            f"the refusal reached today-*.md as though it were a real summary: "
            f"{written!r}"
        )

    def test_refusal_is_logged_as_rejected(self, tmp_path):
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        env["STUB_NDC_TEXT"] = REFUSAL
        env["STUB_NDC_REJECTED"] = "1"

        _run(plugin, env, sid)
        _wait_for_calls_to_settle(calls)

        logs = "".join(p.read_text() for p in (project / ".remember" / "logs").glob("*.log"))
        assert "ndc" in logs and "REJECTED" in logs, (
            f"no REJECTED line logged for the NDC refusal:\n{logs}"
        )

    def test_a_rejected_call_still_reports_what_it_cost(self, tmp_path):
        """#180: the tokens were spent before the verdict was known.

        Logged only on success, a model that starts refusing compression gives
        a run where memory stops growing AND reported cost drops to zero —
        which reads as "nothing happened" rather than "this failed repeatedly
        and was paid for". Same invisibility class as #178.
        """
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        env["STUB_NDC_TEXT"] = REFUSAL
        env["STUB_NDC_REJECTED"] = "1"
        env["STUB_NDC_TOKENS"] = "4321,77,910,0.004242"

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        _wait_for_calls_to_settle(calls)

        logs = "".join(p.read_text() for p in (project / ".remember" / "logs").glob("*.log"))
        ndc_token_lines = [
            line for line in logs.splitlines()
            if "[ndc]" in line and "tokens:" in line
        ]
        assert ndc_token_lines, (
            f"a refused NDC call cost 4321 input + 77 output tokens and left no "
            f"record of it:\n{logs}"
        )
        assert "4321" in ndc_token_lines[0] and "77" in ndc_token_lines[0], (
            f"the logged counts are not the ones the call spent: {ndc_token_lines[0]!r}"
        )
        assert "0.004242" in ndc_token_lines[0], (
            f"the cost is missing from the record: {ndc_token_lines[0]!r}"
        )

    def test_now_md_is_not_truncated_on_refusal(self, tmp_path):
        """A rejected NDC run must leave now.md intact so the next round retries."""
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        env["STUB_NDC_TEXT"] = REFUSAL
        env["STUB_NDC_REJECTED"] = "1"

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        _wait_for_calls_to_settle(calls)

        now_md = project / ".remember" / "now.md"
        assert now_md.is_file() and now_md.stat().st_size > 0, (
            "now.md was truncated despite the NDC response being rejected — "
            "the next round can no longer retry this span"
        )
        assert "did some work" in now_md.read_text(), (
            "the entry written by the main summarize call is gone from now.md"
        )
