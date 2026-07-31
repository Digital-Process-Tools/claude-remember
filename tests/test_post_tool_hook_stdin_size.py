"""The PostToolUse payload must never cross an exec boundary (#266).

`post-tool-hook.sh` read the whole `PostToolUse` JSON off stdin and, on line 91,
exported it::

    export REMEMBER_HOOK_STDIN="$HOOK_STDIN"

That payload embeds the full tool result, so on a large `Read` or a verbose
`Grep` it is hundreds of KB. Once exported it is in `envp` for *every* `execve`
the rest of that hook invocation performs — and every one of them fails
`E2BIG`. Not one command: `sed` from lib-slug.sh, `head` from the hook itself,
`date` from lib-clock.sh, `rm` on exit, each from a different sourced library,
each failing silently because the hook must not block the agent. @peterurbanec
reported a `hook-errors.log` full of exactly that (Rocky Linux 8.6).

Two limits are in play and they are *not* the same limit:

* `ARG_MAX` bounds argv **plus** envp **in total** — 1 MB on macOS, commonly
  ~2 MB on Linux. This is the one the report cites.
* `MAX_ARG_STRLEN` bounds a **single string** at 32 pages (131072 bytes) on
  Linux, and it applies to `envp` exactly as it does to `argv`. macOS has no
  equivalent — a 900 KB single environment variable execs there without
  complaint (measured).

So the stricter limit is the per-string one, it exists only on the reporter's
platform, and a green run on macOS proves the least about precisely this
failure. The first test below is therefore written to be independent of the
host's actual limits: it observes the environment that reached each spawned
subprocess and asserts no single variable is anywhere near either cap. It fails
on Linux (nothing execs at all) and on macOS (the variable is there, oversized)
for the right reason on each.

The second test is the reporter's symptom end to end, sized off the host's own
`ARG_MAX` so it reproduces on whichever platform runs it.

The third pins the other half of the fix: the payload is not simply deleted.
`hooks.d/after_post_tool/*` was told it could have it, the distribution ships
no such hook but a user may have written one, and this is a plugin. The
capability survives — by a route that is not the environment, because a route
that cannot carry a 200 KB payload on Linux was never a working route for the
payloads that matter.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "post-tool-hook.sh"

from pipeline.slug import session_dir_slug as _slug  # noqa: E402
from .subprocess_helpers import subprocess_failure_detail  # noqa: E402

TOOL_LINE = '{"type":"assistant","message":{"content":"x"}}\n'
SESSION = "cccccccc-0000-4000-8000-000000000003"

# Linux `MAX_ARG_STRLEN` = 32 * PAGE_SIZE. A single environment string at or
# above this cannot be exec'd there at all, whatever ARG_MAX says. Nothing this
# hook exports may approach it.
MAX_ARG_STRLEN = 32 * 4096

# Well below that, and below any plausible ARG_MAX once the rest of the
# environment is counted. A variable larger than this has no business being
# handed to a subprocess by a hook that fires on every tool call.
SANE_ENV_VAR_CEILING = 64 * 1024


def _arg_max() -> int:
    try:
        return int(os.sysconf("SC_ARG_MAX"))
    except (ValueError, OSError, AttributeError):  # pragma: no cover - exotic host
        return 1 << 20


def _project(tmp_path: Path, *, jsonl_lines: int = 5):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    (remember / "config.json").write_text(
        json.dumps({"thresholds": {"delta_lines_trigger": 5000}}), encoding="utf-8"
    )
    (session_dir / f"{SESSION}.jsonl").write_text(TOOL_LINE * jsonl_lines)
    return home, project, remember, session_dir


def _env(home: Path, project: Path, remember: Path, plugin_root: Path | None = None) -> dict:
    return {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root or REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }


def _payload(body_bytes: int) -> str:
    """A PostToolUse payload shaped like the real one, with a large tool result.

    `session_id` comes first so the hook's shell extractor finds it regardless
    of how big the result is — the point is the payload size, not a parse
    failure standing in for it.
    """
    return json.dumps({
        "session_id": SESSION,
        "transcript_path": f"/does/not/matter/{SESSION}.jsonl",
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/x"},
        "tool_response": {"content": "x" * body_bytes},
    })


def _run(env: dict, payload: str, timeout: int = 120):
    return subprocess.run(
        ["bash", str(HOOK)],
        env=env,
        input=payload,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _reap(remember: Path):
    pid_file = remember / "tmp" / "save-session.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# The host-independent pin: what actually reaches envp
# ---------------------------------------------------------------------------


# Commands the hook chain is known to reach. Shimming these puts an observer on
# the exec boundary itself: each shim records the environment it was handed and
# then execs the real binary, so the hook runs for real. A shim that could not
# be exec'd records nothing — which is the E2BIG failure, and is why the test
# also asserts that observations happened at all.
_OBSERVED = "date jq git sed id stat find mkdir cat rm python3 head ls wc touch".split()


def _observer_dir(tmp_path: Path, log: Path) -> Path:
    shims = tmp_path / "envshims"
    shims.mkdir(exist_ok=True)
    for name in _OBSERVED:
        real = None
        for d in os.environ.get("PATH", "").split(os.pathsep):
            cand = Path(d) / name
            if cand.is_file() and os.access(cand, os.X_OK):
                real = cand
                break
        if real is None:
            continue
        shim = shims / name
        shim.write_text(
            # Absolute interpreter, never `env python3`: the shim directory is
            # first on PATH, so `env` would resolve the shebang back to the
            # python3 shim itself.
            f"#!{sys.executable}\n"
            "import os, sys\n"
            f"_real = {str(real)!r}\n"
            f"_name = {name!r}\n"
            "try:\n"
            "    with open(os.environ['REMEMBER_ENV_LOG'], 'a') as fh:\n"
            "        for k, v in os.environ.items():\n"
            "            fh.write('%s\\t%s\\t%d\\n' % (_name, k, len(v)))\n"
            "except Exception:\n"
            "    pass\n"
            "os.execv(_real, [_name] + sys.argv[1:])\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
    log.write_text("", encoding="utf-8")
    return shims


def _observations(log: Path):
    raw = log.read_bytes().decode("utf-8", "replace")
    out = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].isdigit():
            out.append((parts[0], parts[1], int(parts[2])))
    return out


def test_no_subprocess_of_the_hook_inherits_an_unbounded_environment(tmp_path):
    """A 300 KB tool result must not become a 300 KB environment variable.

    300 KB is chosen to sit above Linux's per-string `MAX_ARG_STRLEN` (128 KB)
    and below macOS's total `ARG_MAX` (1 MB), so the same payload is a hard
    exec failure on one platform and a silently oversized environment on the
    other. Neither is acceptable and this asserts against both without asking
    the host what its limits are.
    """
    home, project, remember = _project(tmp_path)[:3]
    log = tmp_path / "env-observations.log"
    shims = _observer_dir(tmp_path, log)

    env = _env(home, project, remember)
    env["PATH"] = f"{shims}{os.pathsep}{env['PATH']}"
    env["REMEMBER_ENV_LOG"] = str(log)

    result = _run(env, _payload(300 * 1024))
    _reap(remember)
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    seen = _observations(log)
    assert len(seen) >= 3, (
        "the hook spawned (almost) nothing observable with a 300 KB payload. "
        "On Linux that is the bug itself: the environment exceeded "
        "MAX_ARG_STRLEN, so every execve — including these observers — failed "
        f"E2BIG. Observations: {seen!r}"
    )

    oversized = [(cmd, var, size) for cmd, var, size in seen if size > SANE_ENV_VAR_CEILING]
    assert not oversized, (
        "the hook handed an oversized environment variable to a subprocess. "
        f"Anything above {MAX_ARG_STRLEN} bytes cannot be exec'd on Linux at "
        "all, and this is well past a sane ceiling of "
        f"{SANE_ENV_VAR_CEILING}: {sorted(set(oversized))!r}"
    )


# ---------------------------------------------------------------------------
# The reporter's symptom, end to end, on whatever host runs it
# ---------------------------------------------------------------------------


def test_a_payload_larger_than_arg_max_does_not_break_the_hooks_own_forks(tmp_path):
    """`hook-errors.log` filled with `Argument list too long` — this is that.

    Sized off the host's own ARG_MAX so it exceeds the total budget on any
    platform (and, on Linux, the per-string cap several times over). The hook
    must still do its job: exit 0, and record that PostToolUse was wired, which
    it cannot do if its forks are dying.
    """
    home, project, remember = _project(tmp_path)[:3]
    result = _run(_env(home, project, remember), _payload(_arg_max() + 256 * 1024))
    _reap(remember)

    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    errors = remember / "logs" / "hook-errors.log"
    text = errors.read_text(errors="replace") if errors.is_file() else ""
    assert "Argument list too long" not in text, (
        "a large tool result made the hook's own subprocesses fail to exec:\n"
        + text[-4000:]
    )
    assert (remember / "tmp" / "post-tool-ran").exists(), (
        "the wiring marker was not written — the hook could not complete its "
        "work with a large payload"
    )
    assert (remember / "tmp" / "capture-alive.d" / SESSION).exists(), (
        "no capture-alive marker for the invoking session: the large payload "
        "cost the hook the evidence it exists to write"
    )


# ---------------------------------------------------------------------------
# The contract: after_post_tool can still have the payload
# ---------------------------------------------------------------------------


def _plugin_root_with_consumer(tmp_path: Path, script: str) -> tuple[Path, Path]:
    """A plugin root that ships one `after_post_tool` hook.

    `scripts/` is symlinked to the real ones so the hook under test is the hook
    under test; only `hooks.d/` differs.
    """
    root = tmp_path / "plugin"
    (root / "hooks.d" / "after_post_tool").mkdir(parents=True)
    (root / "scripts").symlink_to(REPO_ROOT / "scripts")
    consumer = root / "hooks.d" / "after_post_tool" / "50-consumer.sh"
    consumer.write_text(script, encoding="utf-8")
    consumer.chmod(0o755)
    return root, consumer


def test_an_after_post_tool_hook_still_receives_the_whole_payload(tmp_path):
    """The docstring promised `hooks.d/after_post_tool/*` the payload. Deleting
    the export would quietly break anyone who took that promise up.

    A 300 KB payload is one the environment route provably cannot deliver on
    Linux, so this asserts the capability by outcome — the consumer ends up
    holding all 300 KB — rather than by the mechanism that never worked.
    """
    home, project, remember = _project(tmp_path)[:3]
    out = tmp_path / "consumer-saw.txt"
    root, _ = _plugin_root_with_consumer(
        tmp_path,
        "#!/bin/bash\n"
        f'out="{out}"\n'
        'if [ -n "${REMEMBER_HOOK_STDIN_FILE:-}" ] && [ -f "$REMEMBER_HOOK_STDIN_FILE" ]; then\n'
        '    wc -c < "$REMEMBER_HOOK_STDIN_FILE" > "$out"\n'
        'else\n'
        '    printf "%s" "${#REMEMBER_HOOK_STDIN}" > "$out"\n'
        'fi\n',
    )

    payload = _payload(300 * 1024)
    result = _run(_env(home, project, remember, plugin_root=root), payload)
    _reap(remember)
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    assert out.is_file(), (
        "the after_post_tool consumer never ran — either dispatch skipped it "
        "or it could not be exec'd"
    )
    assert int(out.read_text().strip()) == len(payload), (
        "the consumer received a short payload. A silently truncated payload "
        "is indistinguishable from a genuinely small one, which is the quieter "
        "half of this same bug — whatever is bounded must be disclosed."
    )


def test_the_payload_is_not_left_lying_around_after_the_hook_exits(tmp_path):
    """Whatever route carries the payload must not accumulate one file per tool
    call under `tmp/` for the life of a session."""
    home, project, remember = _project(tmp_path)[:3]
    root, _ = _plugin_root_with_consumer(tmp_path, "#!/bin/bash\nexit 0\n")

    result = _run(_env(home, project, remember, plugin_root=root), _payload(300 * 1024))
    _reap(remember)
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    leftovers = sorted(p.name for p in (remember / "tmp").glob("hook-stdin*"))
    assert not leftovers, f"payload files left behind in tmp/: {leftovers}"


def test_a_small_payload_still_arrives_in_the_environment(tmp_path):
    """The ordinary case is unchanged: a payload that fits is handed over the
    way it always was, so a consumer written against the old contract keeps
    working for every payload the old contract could actually deliver."""
    home, project, remember = _project(tmp_path)[:3]
    out = tmp_path / "consumer-saw.txt"
    root, _ = _plugin_root_with_consumer(
        tmp_path,
        "#!/bin/bash\n" f'printf "%s" "$REMEMBER_HOOK_STDIN" > "{out}"\n',
    )

    payload = _payload(64)
    result = _run(_env(home, project, remember, plugin_root=root), payload)
    _reap(remember)
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    assert out.is_file(), "the after_post_tool consumer never ran"
    assert out.read_text() == payload, (
        "a small payload no longer arrives in REMEMBER_HOOK_STDIN: "
        f"{out.read_text()[:200]!r}"
    )
