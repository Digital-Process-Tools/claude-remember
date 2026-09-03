"""Establishes (does not assume) whether the locking surface is safe when
Claude Code and Codex share one `.remember/` store on one machine (#491).

`scripts/lib-lock.sh` (the one lock primitive, #182) and
`scripts/lib-staging-lock.sh` (the today-*.md lock, #225) were both written
and tested against a single host's process model -- neither file, nor its
existing test suite, contains the string "codex" or "host". The primitive
itself turns out to already be host-agnostic where it matters: `mkdir` and
`kill -0` are OS-level, process-table operations, indifferent to which CLI
spawned the contending process. This file writes that contract down and
proves it under real concurrent writes from two simulated hosts -- not a
two-host test that passes because the second host never actually wrote,
which the issue itself names as the failure mode to design against.

It also demonstrates the one real, PRE-EXISTING limitation this design
inherits from `kill -0`-based staleness detection: a dead holder's PID can be
reused by an unrelated live process before the lock is checked, and nothing
here can tell that process apart from the true (dead) holder. This is not
introduced or worsened by adding a second host -- it is exactly as likely
between two Claude Code processes as between a Claude Code process and a
Codex process, because `kill -0` cannot see which CLI started a PID. It is
recorded here as a known, general limitation, not as new cross-host damage.

Finally: whether anything in the lock or staging path assumes the Claude
session-ID shape. Neither `lib-lock.sh` nor `lib-staging-lock.sh` ever reads
a session id at all -- lock directories are keyed by fixed literal names
(save.lock, consolidation.lock, staging.lock) and staging files are keyed by
*day*, not by session. The session-ID shape mismatch between Claude Code's
UUID and Codex's `rollout-<date>-<uuid>` basename is real (#459/#468) but
lives entirely in `post-tool-hook.sh`/`save-session.sh`'s own session
resolution, upstream of anything this file touches -- confirmed here with a
grep-based contract test so a future edit that starts threading a session id
into the lock layer trips it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX process semantics (kill -0, mkdir races) -- "
    "not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_LOCK = REPO_ROOT / "scripts" / "lib-lock.sh"
LIB_STAGING_LOCK = REPO_ROOT / "scripts" / "lib-staging-lock.sh"


def _run(script: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, **kwargs
    )


# ---------------------------------------------------------------------------
# 1. Two concurrent writers, genuinely different simulated hosts, one store.
# ---------------------------------------------------------------------------


def test_two_concurrent_hosts_appending_to_staging_lose_nothing_and_interleave_nothing(tmp_path):
    """Claude Code and Codex both append to the same today-*.md, concurrently,
    each holding CODEX_SESSION_ID xor CLAUDE_CODE_SESSION_ID -- the actual
    signature `pipeline/host.py` uses to tell them apart -- for the whole
    round, so a bug that keyed anything off that env var would be exercised.
    Every entry from every round of every host must survive intact, with no
    entry's separator/summary split apart by another writer landing between
    them (#225's own reported failure shape)."""
    today = tmp_path / "today-2026-08-01.md"
    rounds_per_host = 6

    script = f"""
    source {LIB_LOCK}
    source {LIB_STAGING_LOCK}
    today="{today}"

    host_write() {{
        local host="$1" n="$2" text_file
        text_file=$(mktemp)
        printf '## marker-%s-%s | main\\n\\n- entry from %s round %s\\n' "$host" "$n" "$host" "$n" > "$text_file"
        if staging_lock_acquire 10; then
            staging_append "$today" "$text_file"
            staging_lock_release
        else
            echo "TIMEOUT $host $n" >&2
        fi
        rm -f "$text_file"
    }}

    for i in $(seq 1 {rounds_per_host}); do
        ( export CLAUDE_CODE_ENTRYPOINT=hook CLAUDE_CODE_SESSION_ID=aaaa-claude; unset CODEX_SESSION_ID CODEX_THREAD_ID; host_write claude "$i" ) &
    done
    for i in $(seq 1 {rounds_per_host}); do
        ( export CODEX_SESSION_ID=bbbb-codex CODEX_THREAD_ID=cccc-thread; unset CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_SESSION_ID; host_write codex "$i" ) &
    done
    wait
    """
    result = _run(script)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "TIMEOUT" not in result.stderr, (
        f"a host timed out waiting for staging.lock -- not a clean two-writer "
        f"round: {result.stderr!r}"
    )

    content = today.read_text(encoding="utf-8")

    for host in ("claude", "codex"):
        for n in range(1, rounds_per_host + 1):
            marker = f"marker-{host}-{n}"
            assert content.count(marker) == 1, (
                f"{marker} appears {content.count(marker)} times, expected exactly "
                f"1 -- an entry was lost or duplicated under concurrent writes from "
                f"both hosts. Full content:\n{content!r}"
            )

    # No entry's two-line append (separator, then summary) was split apart by
    # the OTHER host's write landing between them: every marker heading is
    # immediately followed by a blank line and then its own "entry from"
    # line, never by another marker or by nothing.
    entries = re.findall(
        r"## (marker-\w+-\d+) \| main\n\n- entry from (\w+) round (\d+)\n",
        content,
    )
    assert len(entries) == 2 * rounds_per_host, (
        f"expected {2 * rounds_per_host} well-formed entries (heading immediately "
        f"followed by its own blank line and summary), found {len(entries)} -- "
        f"some entry's two-part append was interleaved with another writer's. "
        f"Full content:\n{content!r}"
    )


# ---------------------------------------------------------------------------
# 2. A dead holder's PID reused by an unrelated live process. General
#    kill-0 hazard, present identically with or without a second host --
#    documented, not new, not fixed here.
# ---------------------------------------------------------------------------


def test_a_dead_holders_pid_reused_by_an_unrelated_live_process_blocks_recovery(tmp_path):
    """PRE-EXISTING, GENERAL limitation of `kill -0`-based staleness detection
    (present since #182, not introduced by a second host): if the PID a dead
    holder's lock names has since been reused by some OTHER, unrelated live
    process -- which `kill -0` cannot distinguish from the true holder -- the
    lock is judged live and is never recovered until that unrelated process
    also exits. A second host sharing the store does not make this MORE
    likely than two Claude Code processes racing on their own: `kill -0` sees
    a PID, never which CLI spawned it. Recorded here as a documented limit,
    not as new cross-host damage; a real fix (a start-time or generation
    check) is a design decision out of this issue's scope -- see the pull
    request body."""
    lock_dir = tmp_path / "stale.lock"
    lock_dir.mkdir()

    # Stand-in for "recycled by an unrelated process": a real, currently-live
    # sibling process this test controls, whose PID is not the true holder's
    # -- the same shape a coincidentally-reused PID would present to
    # `kill -0`, without depending on the OS actually reusing anything.
    innocent = subprocess.Popen(["sleep", "5"])
    try:
        (lock_dir / "pid").write_text(str(innocent.pid), encoding="utf-8")

        result = _run(f"""
        source {LIB_LOCK}
        lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
        """)
        assert "REFUSED" in result.stdout, (
            "expected the lock to still be judged live (this documents a known "
            f"limitation, not a fix): {result.stdout!r} {result.stderr!r}"
        )
    finally:
        innocent.terminate()
        innocent.wait(timeout=10)


# ---------------------------------------------------------------------------
# 3. Contract: the lock/staging layer never reads a session id.
# ---------------------------------------------------------------------------


def test_lock_and_staging_layer_never_reference_a_session_id(tmp_path):
    """The Claude-Code-vs-Codex session-ID SHAPE mismatch (#459/#468, a UUID
    vs `rollout-<date>-<uuid>`) is real, but lives entirely in
    post-tool-hook.sh/save-session.sh's own session resolution -- upstream of
    the lock primitive and the staging lock, which key everything by fixed
    literal lock names (save.lock, consolidation.lock, staging.lock) or by
    *day* (today-YYYY-MM-DD.md), never by session. A grep contract rather
    than an inspection: trips if a future edit starts threading a session id
    into either file."""
    for path in (LIB_LOCK, LIB_STAGING_LOCK):
        text = path.read_text(encoding="utf-8")
        for needle in ("SESSION_ID", "session_id", "session-id"):
            assert needle not in text, (
                f"{path.name} references {needle!r} -- the lock/staging layer was "
                "believed to be session-shape agnostic; if this is now "
                "intentional, update this contract test's docstring and #491's "
                "closing note rather than silently breaking it"
            )
