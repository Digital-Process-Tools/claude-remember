"""Every shipped shell script parses — under the interpreters actually present.

## The defect

A syntax error anywhere in `scripts/*.sh` or `hooks.d/**` makes the hook exit
non-zero, and on `UserPromptSubmit` a non-zero exit **discards the prompt the
user just typed**. Nothing on disk is lost; what the user wrote is. From their
side it reads as a UI glitch, not a broken hook.

While implementing #301 an agent wrote a `case` statement inside the hook's
`$( )` command substitution. Bash 3.2's parser reads the `)` closing a `case`
pattern as the end of the substitution, so the file fails *at parse time*:

    scripts/user-prompt-hook.sh: line NNN: syntax error near unexpected token `)'

It was caught in seconds only because `user-prompt-hook.sh` is one of the
better-tested paths here. A quieter hook under `hooks.d/` would have shipped it.

## Which interpreter, and why that is the whole question

`bash -n` under whatever `bash` is first on `PATH` is a green that can mean
nothing. Stock macOS ships bash **3.2**; CI runners and most Linux boxes have
5.x. Constructs valid in 5.x are not always valid in 3.2 — `;;&`, `|&`,
`coproc`, and the `case`-inside-`$( )` above are all bash-4+ *parse* features.
A gate that only ever sees 5.x reports `ok` on a file that cannot parse on a
real user's Mac: this repo's signature defect wearing a new hat.

So the gate runs under **every** bash it can find (deduplicated by real path),
records the version of each, and reports which of them it used. It does not
install anything and it does not pretend. Two things follow, and they are kept
separate on purpose:

- **Parse errors** are errors under any interpreter that saw them. That check
  runs wherever a bash exists, which is everywhere this repo is developed or
  tested, and it is what would have caught #301.
- **Floor coverage** — did a bash 3.x actually run? — is a *separate* claim,
  and the only thing entitled to make it is a caller that checks
  `report.floor_covered`. On a Linux CI runner it is `False`, and the reason
  says so by name rather than being folded into a clean pass.

## What this gate deliberately cannot catch

`printf '%(FMT)T'` is the repo's standing bash-3.2 example (#227), and it is a
**runtime** failure, not a parse one: `bash -n` returns 0 on it under 3.2, and
`scripts/lib-clock.sh` carries it today, guarded at runtime, as it should.
Reading this gate as "bash 3.2 compatibility is covered" would be exactly
backwards. It covers *grammar*, which is a strict and small subset.

## `sh` is not `bash`

A `#!/bin/sh` script checked with `bash -n` is being checked against the wrong
grammar — laxer in the ways that matter, so the check would pass constructs
`dash` rejects. Files are dispatched on their shebang. No `#!/bin/sh` file
exists in the tree today; the dispatch is here so the first one is not silently
checked against bash's grammar instead.

A `.sh` file with **no** shebang is a library meant to be sourced. It is
checked with bash — every sourcer in this tree is bash — and listed under
`assumptions` so the assumption is visible rather than buried. A `.sh` file
whose shebang names something that is not a shell lands in `unchecked`, which
callers assert is empty: a gate that silently skips a file is the same defect
it exists to prevent.

## The walk

Every `*.sh` under the root, minus dot-directories and anything git reports as
ignored. The *ignored* set rather than the tracked set, deliberately: a hook
written five minutes ago and not yet `git add`ed is exactly when this earns its
keep. `tests/fixtures/*.sh` is included — it is real shell that the suite
executes, and excluding it would cost more to justify than to check.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

STATUS_OK = "ok"
STATUS_VIOLATIONS = "violations"
STATUS_SKIPPED = "skipped"

KIND_BASH = "bash"
KIND_SH = "sh"

FLOOR_MAJOR = 4
"""A bash below this major is a *floor* interpreter: stock macOS ships 3.2, and
the grammar differences that matter (`;;&`, `|&`, `coproc`, `case` inside
`$( )`) all arrived in 4.0."""

_EXCLUDED_NAMES = frozenset({"venv", "node_modules", "build", "dist", "__pycache__"})

_BASH_CANDIDATES = (
    "/bin/bash",
    "/usr/bin/bash",
    "/usr/local/bin/bash",
    "/opt/homebrew/bin/bash",
    "/opt/local/bin/bash",
)

_VERSION_RE = re.compile(r"version (\d+)\.(\d+)")
_SH_SHEBANG_RE = re.compile(r"^#!\s*(?:/usr/bin/env\s+)?(\S+)")

_TIMEOUT = 30


@dataclass(frozen=True)
class Interpreter:
    """One shell binary the gate can hand a file to, with its version stated.

    `version is None` means `--version` did not answer — the binary is used
    anyway, but it can never satisfy a floor claim, because an unknown version
    is not a low one and is not a high one either.
    """

    path: str
    kind: str
    version: Optional[Tuple[int, int]]

    @property
    def label(self) -> str:
        if self.version is None:
            return "{} ({}, version unknown)".format(self.path, self.kind)
        return "{} ({} {}.{})".format(self.path, self.kind, *self.version)

    @property
    def is_floor(self) -> bool:
        return self.version is not None and self.version[0] < FLOOR_MAJOR


@dataclass(frozen=True)
class ParseError:
    """One file that does not parse, and the interpreter that said so."""

    path: Path
    interpreter: str
    message: str


@dataclass(frozen=True)
class GateReport:
    """`skipped` is not a pass — it is the gate saying it did not run, with the
    reason, rather than reporting its own silence as a clean result.

    `floor_covered` is the separate, narrower claim: a `True` here means a bash
    below 4.0 actually parsed these files. `gaps` names, in prose, every piece
    of coverage this run did not have.

    `interpreters` is what the machine *offered*; `used` is what was actually
    handed a file. They differ — `/bin/sh` is discovered on every Unix and is
    handed nothing while the tree holds no `#!/bin/sh` script — and reporting
    the first as though it were the second would overstate the coverage by
    exactly the amount this gate exists to stop people overstating.
    """

    status: str
    reason: str
    files: Tuple[Path, ...]
    interpreters: Tuple[Interpreter, ...]
    used: Tuple[Interpreter, ...] = ()
    errors: Tuple[ParseError, ...] = ()
    floor_covered: bool = False
    gaps: Tuple[str, ...] = ()
    assumptions: Tuple[Path, ...] = ()
    unchecked: Tuple[Tuple[Path, str], ...] = ()

    @property
    def files_scanned(self) -> int:
        return len(self.files)


# ── The walk ──────────────────────────────────────────────────────────────

def _git_ignored(root: Path) -> Set[str]:
    if not (root / ".git").exists():
        return set()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--ignored",
             "--exclude-standard", "--directory"],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()
    return {line.rstrip("/") for line in result.stdout.splitlines() if line.strip()}


def _excluded_by_name(part: str) -> bool:
    return part.startswith(".") or part in _EXCLUDED_NAMES


def repo_shell_files(root: Path) -> List[Path]:
    """Every `*.sh` in the repository that is source rather than machine state
    which happens to sit in the working tree."""
    ignored = _git_ignored(root)
    files = []
    for path in sorted(root.rglob("*.sh")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(_excluded_by_name(part) for part in relative.parts):
            continue
        posix = relative.as_posix()
        if any(posix == entry or posix.startswith(entry + "/") for entry in ignored):
            continue
        files.append(path)
    return files


# ── Shebang dispatch ──────────────────────────────────────────────────────

def shebang_interpreter(path: Path) -> Optional[str]:
    """The bare interpreter name from the shebang, or `None` if there is none.

    `#!/usr/bin/env bash` and `#!/bin/bash` both answer `bash`.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
    except OSError:
        return None
    match = _SH_SHEBANG_RE.match(first)
    if match is None:
        return None
    return os.path.basename(match.group(1))


def required_kind(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """`(kind, note)`. `kind` is the grammar this file must be checked against;
    `None` means no shell can honestly check it and the caller must say so.

    A missing shebang yields `bash` plus a note — the assumption is recorded,
    not hidden.
    """
    name = shebang_interpreter(path)
    if name is None:
        return KIND_BASH, "no shebang; assumed bash (sourced library)"
    if name == "bash":
        return KIND_BASH, None
    if name in ("sh", "dash", "ash"):
        return KIND_SH, None
    return None, "shebang names `{}`, which is not a shell this gate can check".format(name)


# ── Interpreter discovery ─────────────────────────────────────────────────

def _version_of(path: str) -> Optional[Tuple[int, int]]:
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(result.stdout or "")
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _candidate_paths(name: str) -> List[str]:
    found: List[str] = []
    on_path = shutil.which(name)
    if on_path:
        found.append(on_path)
    if name == "bash":
        found.extend(_BASH_CANDIDATES)
    else:
        found.append("/bin/" + name)
    return found


def discover_interpreters() -> List[Interpreter]:
    """Every distinct `bash`, plus one `sh`, that this machine can offer.

    Deduplicated by real path so a symlink farm does not inflate the count, and
    so `/usr/local/bin/bash -> /bin/bash` is one interpreter rather than two.
    `sh` is kept separate from `bash` even when it is the same binary: the
    grammar it enforces is what is being selected, not the executable.
    """
    interpreters: List[Interpreter] = []
    seen: Dict[str, Set[str]] = {KIND_BASH: set(), KIND_SH: set()}
    for kind, name in ((KIND_BASH, "bash"), (KIND_SH, "sh")):
        for candidate in _candidate_paths(name):
            if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
                continue
            real = os.path.realpath(candidate)
            if real in seen[kind]:
                continue
            seen[kind].add(real)
            interpreters.append(Interpreter(candidate, kind, _version_of(candidate)))
            if kind == KIND_SH:
                break
    return interpreters


# ── The check ─────────────────────────────────────────────────────────────

def check_parse(path: Path, interpreter: Interpreter) -> Optional[ParseError]:
    """`bash -n` / `sh -n` on one file. `None` means it parsed."""
    try:
        result = subprocess.run(
            [interpreter.path, "-n", str(path)],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ParseError(path, interpreter.label, "could not run: {}".format(exc))
    if result.returncode == 0:
        return None
    message = (result.stderr or result.stdout or "").strip()
    return ParseError(path, interpreter.label, message or "exit {}".format(result.returncode))


def _describe_gaps(used: Sequence[Interpreter]) -> List[str]:
    gaps: List[str] = []
    if not any(i.is_floor for i in used if i.kind == KIND_BASH):
        versions = ", ".join(
            i.label for i in used if i.kind == KIND_BASH
        ) or "none"
        gaps.append(
            "no bash below {}.0 was available ({}), so constructs that parse on "
            "modern bash but not on the 3.2 stock macOS ships — `;;&`, `|&`, "
            "`coproc`, a `case` inside `$( )` — went unchecked".format(
                FLOOR_MAJOR, versions
            )
        )
    if not any(not i.is_floor for i in used if i.kind == KIND_BASH):
        gaps.append(
            "no bash {}.0 or newer was available, so constructs the floor "
            "accepts and a modern bash rejects went unchecked".format(FLOOR_MAJOR)
        )
    if any(i.version is None for i in used):
        gaps.append(
            "an interpreter did not report a version: {}".format(
                ", ".join(i.label for i in used if i.version is None)
            )
        )
    return gaps


def run_gate(root: Path, interpreters: Optional[Sequence[Interpreter]] = None) -> GateReport:
    """Parse every shipped shell file under every interpreter that fits it."""
    available = list(interpreters) if interpreters is not None else discover_interpreters()
    if not any(i.kind == KIND_BASH for i in available):
        return GateReport(
            STATUS_SKIPPED,
            "no bash interpreter found on this machine — the gate did not run, "
            "which is not the same as finding nothing wrong",
            (), tuple(available), (),
        )

    files = repo_shell_files(root)
    if not files:
        return GateReport(
            STATUS_SKIPPED,
            "walked {} and found no shell files to check — the gate did not "
            "run, which is not the same as finding nothing wrong".format(root),
            (), tuple(available), (),
        )

    errors: List[ParseError] = []
    assumptions: List[Path] = []
    unchecked: List[Tuple[Path, str]] = []
    used: List[Interpreter] = []

    for path in files:
        kind, note = required_kind(path)
        if kind is None:
            unchecked.append((path, note or "no interpreter could be determined"))
            continue
        if note:
            assumptions.append(path)
        matching = [i for i in available if i.kind == kind]
        if not matching:
            unchecked.append((path, "no `{}` on this machine to check it with".format(kind)))
            continue
        for interpreter in matching:
            if interpreter not in used:
                used.append(interpreter)
            error = check_parse(path, interpreter)
            if error is not None:
                errors.append(error)

    gaps = _describe_gaps(used)
    for path, why in unchecked:
        gaps.append("{} was not checked: {}".format(path.relative_to(root).as_posix(), why))
    floor_covered = any(i.is_floor for i in used if i.kind == KIND_BASH)

    interpreter_list = ", ".join(i.label for i in used) or "none"
    if errors:
        return GateReport(
            STATUS_VIOLATIONS,
            "{} of {} shell file(s) do not parse under {}".format(
                len({e.path for e in errors}), len(files), interpreter_list
            ),
            tuple(files), tuple(available), tuple(used), tuple(errors),
            floor_covered, tuple(gaps), tuple(assumptions), tuple(unchecked),
        )
    return GateReport(
        STATUS_OK,
        "{} shell file(s) parse under {}".format(len(files), interpreter_list),
        tuple(files), tuple(available), tuple(used), (),
        floor_covered, tuple(gaps), tuple(assumptions), tuple(unchecked),
    )
