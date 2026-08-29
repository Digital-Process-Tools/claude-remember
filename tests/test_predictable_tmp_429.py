"""scripts/lib-memory-dir.sh and scripts/lib-env-cache.sh build a shared-tmpdir
path from `$$` through an intermediate variable instead of `mktemp` (#429).

Both are the same predictable-name TOCTOU #427 fixed in `doctor.sh`: the shell
(and Python's `open()`, and `jq`'s `>`) follows a symlink when opening a
redirection target and truncates on open, before a byte is written -- so a
pre-seeded symlink at the predictable name lets an attacker truncate an
arbitrary file as the invoking user, or -- for `lib-memory-dir.sh` -- receive
the actual write that follows.

`lib-memory-dir.sh` is the sharper case: its `_merged_cfg` can carry a live
`haiku.oauth_token` (README.md's own documented config key), and unlike
`doctor.sh`'s stderr-capture file, this one is written to twice -- once to
create it private, once with the real merged JSON -- so a planted symlink at
the predictable path receives the credential itself, not just a truncation.
`test_credential_bearing_config_lands_at_a_planted_symlink_pre_fix` proves it
with a live subprocess and a real (fake) oauth token; the paired
`test_legitimate_write_actually_carries_the_token` is the positive control --
it proves the harness really does put the token in the merged config when
nothing interferes, so "the victim file was untouched" in the attack test
means the write was redirected away from the attacker's path, not that the
write silently stopped happening altogether.

`lib-env-cache.sh`'s `_remember_env_cache_publish` builds its private-then-
renamed temp file as `${_REMEMBER_ENV_CACHE_FILE}.$$` (line ~195, sourced from
the predictable `${TMPDIR:-/tmp}/remember-env-<key>` built at line 80) -- the
same shape, without the credential. `test_env_cache_publish_temp_survives_a_
planted_symlink` and its positive-control pair cover it the same way.

Neither site is caught by #427's own class-pin regex
(`tests/test_doctor_predictable_tmp_427.py::test_no_script_builds_a_shared_
tmpdir_path_from_pid`): both build the path through an intermediate variable
(`SYS_TMPDIR`, `_REMEMBER_ENV_CACHE_FILE`) rather than spelling `${TMPDIR:-
/tmp}` or a bare `/tmp` literal directly beside the `$$` on the same line, so
the regex's two alternatives never match either line. That gap is exactly
what #427's own review says when it filed this issue ("filed separately for
two sites found to still have exactly that shape") -- recorded here rather
than fixed, since the regex lives in `tests/test_doctor_predictable_tmp_427.py`,
a file held by lane fix/427 (open as PR #428), not this lane.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_needs_posix_bash = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX symlink semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_MEMORY_DIR = REPO_ROOT / "scripts" / "lib-memory-dir.sh"
LIB_ENV_CACHE = REPO_ROOT / "scripts" / "lib-env-cache.sh"

FAKE_TOKEN = "sk-ant-oat-FAKE-TOKEN-FOR-429-DO-NOT-USE"


def _memory_project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    pipeline = tmp_path / "pipeline"
    shared_tmp = tmp_path / "shared-tmp"
    project.mkdir()
    home.mkdir()
    pipeline.mkdir()
    shared_tmp.mkdir()
    (pipeline / "config.json").write_text(
        json.dumps({"haiku": {"oauth_token": FAKE_TOKEN}, "model": "haiku"})
    )
    return home, project, pipeline, shared_tmp


def _run_memory_dir(home, project, pipeline, shared_tmp, extra_script=""):
    env = {
        **os.environ,
        "HOME": str(home),
        "PROJECT_DIR": str(project),
        "PIPELINE_DIR": str(pipeline),
        "TMPDIR": str(shared_tmp),
    }
    script = f'source "{LIB_MEMORY_DIR}"\n{extra_script}\n'
    return subprocess.Popen(
        ["bash", "-c", script], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


@_needs_posix_bash
def test_credential_bearing_config_lands_at_a_planted_symlink_pre_fix(tmp_path):
    home, project, pipeline, shared_tmp = _memory_project(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("attacker did not touch this\n")

    proc = _run_memory_dir(home, project, pipeline, shared_tmp)
    predicted_path = shared_tmp / f"remember-config-{proc.pid}.json"

    deadline = time.monotonic() + 5
    while not shared_tmp.exists() and time.monotonic() < deadline:
        pass  # shared_tmp already exists; this loop never actually spins

    os.symlink(victim, predicted_path)

    try:
        proc.wait(timeout=30)
    finally:
        if predicted_path.is_symlink():
            predicted_path.unlink()

    assert victim.read_text() == "attacker did not touch this\n", (
        "the merged config -- including the fake haiku.oauth_token -- was "
        "written through a predictable, attacker-plantable symlink at "
        + str(predicted_path) + "; victim now holds:\n" + victim.read_text()
    )


@_needs_posix_bash
def test_legitimate_write_actually_carries_the_token(tmp_path):
    """Positive control: prove the harness really writes the token into
    REMEMBER_CONFIG when nothing interferes -- otherwise "victim untouched"
    above would be equally true of a write that silently stopped happening."""
    home, project, pipeline, shared_tmp = _memory_project(tmp_path)

    proc = _run_memory_dir(
        home, project, pipeline, shared_tmp,
        extra_script='echo "RC=$REMEMBER_CONFIG"; cat "$REMEMBER_CONFIG" >&2',
    )
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, f"stdout={out!r} stderr={err!r}"
    assert FAKE_TOKEN in err, (
        "the merged config the harness actually wrote does not contain the "
        f"fake token -- harness broken independent of the symlink attack: {err!r}"
    )


def _env_cache_project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    home.mkdir()
    shared_tmp = tmp_path / "shared-tmp"
    shared_tmp.mkdir()
    return home, project, remember, shared_tmp


def _publish_script(remember: Path) -> str:
    return (
        f'source "{LIB_ENV_CACHE}"\n'
        f'REMEMBER_DIR="{remember}"\n'
        'export REMEMBER_DIR REMEMBER_TZ=UTC PROJECT_DIR PIPELINE_DIR\n'
        'REMEMBER_SAVE_COOLDOWN=120\n'
        'REMEMBER_DELTA_THRESHOLD=50\n'
        'export REMEMBER_SAVE_COOLDOWN REMEMBER_DELTA_THRESHOLD\n'
        '_remember_env_cache_path\n'
        'echo "CACHE_FILE=$_REMEMBER_ENV_CACHE_FILE"\n'
        '_remember_env_cache_publish\n'
    )


@_needs_posix_bash
def test_env_cache_publish_temp_survives_a_planted_symlink(tmp_path):
    home, project, remember, shared_tmp = _env_cache_project(tmp_path)
    victim = tmp_path / "victim2.txt"
    victim.write_text("attacker did not touch this either\n")

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "PROJECT_DIR": str(project),
        "PIPELINE_DIR": str(REPO_ROOT / "pipeline"),
        "TMPDIR": str(shared_tmp),
    }

    # The cache file NAME is deterministic from CLAUDE_PROJECT_DIR alone (no
    # PID needed) -- compute it the same way _remember_env_cache_path does.
    key = "".join(c if c.isalnum() else "-" for c in str(project))
    key = key[-120:] if len(key) > 120 else key
    cache_file = shared_tmp / f"remember-env-{key}"

    proc = subprocess.Popen(
        ["bash", "-c", _publish_script(remember)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    predicted_tmp = Path(f"{cache_file}.{proc.pid}")

    deadline = time.monotonic() + 5
    while not shared_tmp.exists() and time.monotonic() < deadline:
        pass

    os.symlink(victim, predicted_tmp)

    try:
        out, err = proc.communicate(timeout=30)
    finally:
        if predicted_tmp.is_symlink():
            predicted_tmp.unlink()

    assert victim.read_text() == "attacker did not touch this either\n", (
        "the env-cache publish temp file was written through a predictable, "
        "attacker-plantable symlink at " + str(predicted_tmp) + f"; stdout={out!r} stderr={err!r}"
    )


@_needs_posix_bash
def test_env_cache_publish_actually_writes_the_cache_file(tmp_path):
    """Positive control for the env-cache publish test above."""
    home, project, remember, shared_tmp = _env_cache_project(tmp_path)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "PROJECT_DIR": str(project),
        "PIPELINE_DIR": str(REPO_ROOT / "pipeline"),
        "TMPDIR": str(shared_tmp),
    }
    proc = subprocess.run(
        ["bash", "-c", _publish_script(remember)], env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    cache_line = [l for l in proc.stdout.splitlines() if l.startswith("CACHE_FILE=")]
    assert cache_line, proc.stdout
    cache_file = Path(cache_line[0].split("=", 1)[1])
    assert cache_file.is_file(), (
        f"publish did not actually create the cache file at {cache_file} -- "
        f"harness broken independent of the symlink attack: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
