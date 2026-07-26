"""Follow-up to #142: NDC compression against now.md must be lock-serialized.

#142 replaced the blind ``: > "$MEMORY_FILE"`` truncate with a partial one
that keeps bytes appended after the snapshot — but held no lock, so three
defects remained in ``save-session.sh``:

  (a) the byte-count snapshot (``wc -c``) and the ``build-ndc-prompt`` read
      are two unsynchronized reads of now.md;
  (b) the closing ``tail``/``mv`` that commits the truncate is itself
      unserialized against a concurrent append;
  (c) the ``tail``-failure branch still falls back to a blind
      ``: > "$MEMORY_FILE"`` — the original #142 bug, preserved as an error
      path.

``scripts/lib-now-lock.sh`` gives every now.md writer (append, NDC snapshot,
NDC commit) one flock-guarded critical section (``now_locked``), plus a
bounds-checked, atomic-rename truncate (``now_truncate_first``) that leaves
now.md completely untouched — never emptied — whenever it cannot safely
proceed.

These tests exercise the library directly: (1) mutual exclusion between two
concurrent ``now_locked`` holders, and (2)/(3) ``now_truncate_first``'s
bounds guard and its behavior on a failing ``tail``. All three fail on
pristine 0.8.6, where the library does not exist at all.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock + bash subprocess — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_SCRIPT = REPO_ROOT / "scripts" / "lib-now-lock.sh"


def _require_lib():
    if not LIB_SCRIPT.exists():
        pytest.fail(
            "scripts/lib-now-lock.sh does not exist — the #142 follow-up "
            "fix (locked NDC snapshot/commit) is not present"
        )


class TestNowLockedMutualExclusion:

    def test_two_concurrent_holders_never_overlap(self, tmp_path):
        """Two now_locked critical sections racing on the same REMEMBER_DIR
        must be strictly serialized — proof that a concurrent save's append
        and a concurrent NDC snapshot/commit cannot interleave (defects a/b)."""
        _require_lib()
        remember = tmp_path / ".remember" / "tmp"
        remember.mkdir(parents=True)
        trace = tmp_path / "trace.log"

        # Each holder: acquire the lock, record "start <id>", sleep (simulating
        # the work a real snapshot/commit does), record "end <id>". If the
        # lock does not serialize, both "start" lines can appear before either
        # "end" line.
        script = f"""
        set -e
        export REMEMBER_DIR={tmp_path}/.remember
        source {LIB_SCRIPT}
        _work() {{
            echo "start $1" >> {trace}
            sleep 0.5
            echo "end $1" >> {trace}
        }}
        now_locked 10 _work "$1"
        """
        p1 = subprocess.Popen(["bash", "-c", script, "_", "A"])
        time.sleep(0.1)  # give A a head start so it acquires first
        p2 = subprocess.Popen(["bash", "-c", script, "_", "B"])
        assert p1.wait(timeout=15) == 0
        assert p2.wait(timeout=15) == 0

        lines = trace.read_text().splitlines()
        assert lines == ["start A", "end A", "start B", "end B"], (
            f"critical sections overlapped: {lines!r} — now_locked did not "
            "serialize two concurrent holders"
        )


class TestNowTruncateFirstSafety:

    def test_refuses_when_n_exceeds_current_size(self, tmp_path):
        """A stale/racing byte count larger than the file must be refused —
        never truncated past EOF, never treated as 'truncate everything'."""
        _require_lib()
        mem = tmp_path / "now.md"
        mem.write_text("short content")

        script = f"""
        set -e
        export REMEMBER_DIR={tmp_path}
        source {LIB_SCRIPT}
        now_truncate_first "{mem}" 9999
        """
        result = subprocess.run(["bash", "-c", script],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert mem.read_text() == "short content", (
            "now.md was modified despite the bounds guard refusing the stale count"
        )

    def test_tail_failure_leaves_file_untouched_not_emptied(self, tmp_path):
        """If `tail` itself fails, now.md must be left exactly as it was —
        never fall back to `: > $mem` (the original #142 bug as an error path)."""
        _require_lib()
        mem = tmp_path / "now.md"
        mem.write_text("precious content that must survive a tail failure")

        # A fake `tail` that always fails, placed first on PATH.
        fake_bin = tmp_path / "fake-bin"
        fake_bin.mkdir()
        (fake_bin / "tail").write_text("#!/bin/bash\nexit 1\n")
        (fake_bin / "tail").chmod(0o755)

        script = f"""
        set -e
        export REMEMBER_DIR={tmp_path}
        source {LIB_SCRIPT}
        now_truncate_first "{mem}" 5
        """
        env = {**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
        result = subprocess.run(["bash", "-c", script], env=env,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert mem.read_text() == "precious content that must survive a tail failure", (
            "a failing `tail` emptied now.md instead of leaving it untouched"
        )
        # The sibling temp file must never accumulate either.
        assert not list(tmp_path.glob("now.md.tail.*"))
