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
            cand = Path(d) / name
            if cand.is_file() and os.access(cand, os.X_OK):
                real = cand
                break
        if real is None:
            continue
        shim = shims / name
        shim.write_text(
            "#!/bin/bash\n"
            f'printf "%s %s\\n" "{name}" "$*" >> "$SPAWN_LOG"\n'
            f'exec "{real}" "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
    if log is not None:
        log.write_text("", encoding="utf-8")
    return shims


def spawns(log: Path) -> list[str]:
    """The recorded executions. Decoded leniently: a shimmed command's arguments
    can carry any bytes at all (log.sh's dash-squashing `sed` script is not
    UTF-8), and a budget assertion must not become a UnicodeDecodeError."""
    raw = log.read_bytes().decode("utf-8", "replace")
    return [line for line in raw.splitlines() if line.strip()]
