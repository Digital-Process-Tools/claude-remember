"""An unmeasured file size must not launder into `0` (round-1 audit finding).

`_remember_render_memory_section` measures every memory file in one batched
`wc -c` and caches the result per path. `_remember_emit_file` then picks how to
emit each file BY THAT SIZE: under 16 KB it uses bash's own
`IFS= read -r -d ''`, and at or above it uses `cat`, because the read path is
catastrophically slower on a large file -- 4 MB measured at 24.1s on
windows-latest, on the session-start foreground path. That threshold shipped in
this same release (#660/#696).

It also has a third arm for "no usable size", commented "`cat` is the one that
cannot go quadratic" -- and that arm was unreachable from its only caller.
`_remember_wc_size_get_into` returned `${!key:-0}` and the caller then ran
`case "$MFILE_BYTES" in (''|*[!0-9]*) MFILE_BYTES=0 ;; esac`: two separate
places turning "never measured" into the literal `0`. `0` is a valid digit
string and `0 -gt 16384` is false, so an unmeasured file of any size took the
`read` path -- defeating the threshold in exactly the degraded case it was
written for.

Not hypothetical: the batched `wc -c` can fail wholesale (ARG_MAX, `wc`
absent), which leaves every file unmeasured at once.

The same laundering reached the reader, too: the compact-mode "not re-injected"
notice printed `<path> (0 bytes)` for a file whose size was never established,
which is indistinguishable from an empty file.

So `unknown` is kept as its own value all the way through: empty, not zero.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "lib-memory-context.sh"


def _run(body: str, tmp_path: Path) -> subprocess.CompletedProcess:
    script = f"""
    set -u
    export PIPELINE_DIR={REPO_ROOT}
    export PROJECT_DIR={tmp_path}
    source {LIB} >/dev/null 2>&1
    {body}
    """
    return subprocess.run(["bash", "-c", script], check=False,
                          capture_output=True, text=True, timeout=30,
                          env={**os.environ})


class TestTheUnmeasuredSizeStaysUnknown:

    def test_a_file_with_no_cached_measurement_reads_back_empty(self, tmp_path):
        """The seam. `0` here is a measurement nobody made."""
        r = _run('_remember_wc_size_get_into GOT "/no/such/file"; printf "[%s]" "$GOT"',
                 tmp_path)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "[]", (
            f"_remember_wc_size_get_into answered {r.stdout.strip()!r} for a "
            f"path it holds no measurement for. `0` is a size, and every "
            f"consumer downstream treats it as one -- including the emit "
            f"threshold, which then picks the read path for a file of any "
            f"size at all"
        )

    def test_a_real_measurement_still_reads_back(self, tmp_path):
        """Positive control: keeping `unknown` distinct must not lose the
        ordinary answer."""
        r = _run('_remember_wc_size_set "/some/file" 4242;'
                 ' _remember_wc_size_get_into GOT "/some/file"; printf "[%s]" "$GOT"',
                 tmp_path)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "[4242]"

    def test_an_unknown_size_takes_the_cat_arm(self, tmp_path):
        """`_remember_emit_file`'s own third arm, reached with what the getter
        now actually produces. Asserted by putting a recording `cat` on PATH:
        the bytes come out identical either way, so output alone cannot tell
        the arms apart -- which is how this went unnoticed."""
        big = tmp_path / "big.md"
        big.write_text("x" * 40000, encoding="utf-8")
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        marker = tmp_path / "cat-ran"
        shim = shim_dir / "cat"
        shim.write_text(f"#!/bin/bash\ntouch '{marker}'\nexec /bin/cat \"$@\"\n",
                        encoding="utf-8")
        shim.chmod(0o755)

        script = f"""
        set -u
        export PIPELINE_DIR={REPO_ROOT}
        export PROJECT_DIR={tmp_path}
        export PATH="{shim_dir}:$PATH"
        source {LIB} >/dev/null 2>&1
        _remember_wc_size_get_into SZ "{big}"
        _remember_emit_file "{big}" "$SZ" | wc -c
        """
        r = subprocess.run(["bash", "-c", script], check=False, capture_output=True,
                           text=True, timeout=30, env={**os.environ})
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip().endswith("40000"), (
            f"the file's bytes did not survive the emit: {r.stdout!r}")
        assert marker.exists(), (
            "a 40 KB file whose size was never measured was emitted through "
            "bash's `read` rather than `cat` -- the unmeasured case takes the "
            "path that goes quadratic, which is the one the third arm exists "
            "to avoid"
        )

    def test_a_known_small_size_still_avoids_the_fork(self, tmp_path):
        """Paired must-not-fire: the fix must not send everything to `cat`,
        which would undo #660's whole point."""
        small = tmp_path / "small.md"
        small.write_text("hello", encoding="utf-8")
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        marker = tmp_path / "cat-ran"
        shim = shim_dir / "cat"
        shim.write_text(f"#!/bin/bash\ntouch '{marker}'\nexec /bin/cat \"$@\"\n",
                        encoding="utf-8")
        shim.chmod(0o755)

        script = f"""
        set -u
        export PIPELINE_DIR={REPO_ROOT}
        export PROJECT_DIR={tmp_path}
        export PATH="{shim_dir}:$PATH"
        source {LIB} >/dev/null 2>&1
        _remember_emit_file "{small}" 5
        """
        r = subprocess.run(["bash", "-c", script], check=False, capture_output=True,
                           text=True, timeout=30, env={**os.environ})
        assert r.returncode == 0, r.stderr
        assert r.stdout == "hello"
        assert not marker.exists(), (
            "a 5-byte file with a known size forked `cat` -- the read path is "
            "what #660 introduced the threshold for"
        )
