"""One counted-command list, shared by every spawn-budget test.

#227 pinned `user-prompt-hook.sh` to a spawn budget and #230 pinned
`post-tool-hook.sh` to one. Each arrived with its own copy of "every external
command this chain is known to reach", and the copies **disagreed**: #230's had
`head`, because `ls -t … | head -1` is a two-process idiom and counting only the
`ls` understates it by half. #227's did not.

A budget test that cannot see part of what it claims to measure reports a number
that reads as complete. Adding `| head` to the prompt hook would have been
invisible to the prompt hook's own budget — which is precisely the defect class
this repo files on, arriving inside a guard. So the list lives here, once, and
both files import it rather than restating it.

(Aligning them cost nothing: with `head` counted, the prompt hook's warm run is
still 1 spawn on bash 3.2 against a budget of 2. The omission had never hidden
anything — it was a loaded gun, not a wound.)
"""

from __future__ import annotations

import os
from pathlib import Path

# Every external command the hook chains are known to reach, plus the ones a
# plausible rewrite would reach. A command absent from this list is simply not
# counted, so the list erring long is the safe direction — and anything ADDED
# here only ever makes the budgets stricter, never looser.
COUNTED = (
    "date whoami jq git dirname basename sed tr id stat find mkdir cat rm cp mv "
    "python3 python iconv cygpath tar wc touch uname expr awk grep ls sleep nohup "
    "head sort cut"
).split()


# On native Windows the binaries this shimming exists to intercept are never
# named bare ("jq", "git") -- they carry one of these suffixes, and a lookup
# that only tries the bare name never finds any of them, so make_shim_dir
# silently shimmed NOTHING on Windows -- not just jq, every one of COUNTED --
# and every existing caller either blanket-skips win32 (most of them) or
# only ever asserts an UPPER bound on the spawn count (test_log_sh.py's
# `assert len(reads) <= 1`), which still passes when the true count is 0
# because the shim never intercepted anything at all. The gap had no
# consumer that could fail loudly until a test asserted a LOWER bound --
# "at least one spawn must be observed" -- which is exactly what exposed it
# (#670, CI: windows-latest 3.11/3.12, test_config_flatten_cache_668.py's
# own positive control: "got spawns: []", the whole list empty, not merely
# missing jq -- consistent with zero shims having been created at all).
_EXE_SUFFIXES = [""] if os.name != "nt" else ["", ".exe", ".cmd", ".bat"]


def make_shim_dir(tmp_path: Path, log: Path | None = None) -> Path:
    """A PATH front-end that records every external execution, then execs it.

    Each shim appends `<name> <args>` to `$SPAWN_LOG` and `exec`s the real
    binary, so the chain under test runs for real and the count is by execution
    rather than by wall clock — wall clock is what differs between platforms,
    spawn count is what causes it.
    """
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    for name in COUNTED:
        real = None
        for d in os.environ.get("PATH", "").split(os.pathsep):
            for suffix in _EXE_SUFFIXES:
                cand = Path(d) / (name + suffix)
                if cand.is_file() and os.access(cand, os.X_OK):
                    real = cand
                    break
            if real is not None:
                break
        if real is None:
            continue
        shim = shims / name
        # newline="": write_text's default universal-newline translation
        # turns every \n into \r\n on Windows, and a shebang line ending
        # in \r is a broken interpreter directive under Git Bash/MSYS --
        # `#!/bin/bash\r` is not a path bash's own exec() can resolve.
        # Observed live on windows-latest/3.11 CI (#669): every COUNTED
        # command was correctly located via _EXE_SUFFIXES (the #670 fix),
        # shim files existed with the right content, `command -v` still
        # reported the shim PATH entry as present/executable (NTFS ACL
        # default), but invoking it never actually ran the wrapper --
        # SPAWN_LOG stayed empty for an entire cold+warm pair that still
        # exited 0, meaning the real (unshimmed) binary answered instead
        # and the shim silently never fired. This is the exact CRLF class
        # this repo already named and fixed once elsewhere
        # (tests/test_install_agy_hooks_563.py, #577).
        #
        # `open(..., newline="")`, not `Path.write_text(..., newline=...)`:
        # Path.write_text only gained a `newline` parameter in Python 3.10,
        # and this repo's own matrix floors at 3.9 (.github/workflows/tests.yml)
        # -- the first attempt at this fix used write_text(newline=...) and
        # broke every already-passing caller of make_shim_dir on
        # windows-latest/3.9 with TypeError: write_text() got an unexpected
        # keyword argument 'newline' (observed on CI, job 103473817312).
        # open()'s own `newline` parameter has existed on every Python this
        # repo supports.
        with open(shim, "w", encoding="utf-8", newline="") as _f:
            _f.write(
                "#!/bin/bash\n"
                f'printf "%s %s\\n" "{name}" "$*" >> "$SPAWN_LOG"\n'
                # .as_posix(), not a raw str(real): on Windows `real` is a
                # backslash-separated WindowsPath, and embedding that directly
                # into a bash script is the identical class of bug #670 already
                # fixed once in this same PR (tests/test_start_context_cache_668.py,
                # tests/test_config_flatten_cache_668.py) -- this fix must not
                # reintroduce it one function away.
                f'exec "{real.as_posix()}" "$@"\n'
            )
        shim.chmod(0o755)
    if log is not None:
        with open(log, "w", encoding="utf-8", newline="") as _f:
            _f.write("")
    return shims


def spawns(log: Path) -> list[str]:
    """The recorded executions. Decoded leniently: a shimmed command's arguments
    can carry any bytes at all (log.sh's dash-squashing `sed` script is not
    UTF-8), and a budget assertion must not become a UnicodeDecodeError."""
    raw = log.read_bytes().decode("utf-8", "replace")
    return [line for line in raw.splitlines() if line.strip()]
