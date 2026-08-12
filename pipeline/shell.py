"""Shell integration helpers — output shell-evaluable variables from Python.

Each ``cmd_*`` function prints ``KEY=VALUE`` pairs to stdout that shell
scripts consume via ``eval "$(python3 -m pipeline.shell <command> ...)"```.
This eliminates the pattern of calling ``python3 -c`` multiple times to
read individual fields from the same JSON.

Large text values (exchanges, Haiku responses) are written to temp files
and their paths are printed as shell variables, avoiding shell escaping
issues with multi-line or quote-containing text.

The ``main()`` function acts as a CLI dispatcher, routing subcommands
to the appropriate ``cmd_*`` function.

Available subcommands::

    extract         Extract session exchanges
    build-prompt    Build save-summary prompt file
    build-ndc-prompt Build NDC compression prompt file
    parse-haiku     Parse Haiku JSON response from stdin
    call-haiku      Invoke Haiku on a prompt file (sandbox + parse in one)
    save-position   Write position to last-save.json
    consolidate     Run full consolidation pipeline

"""

from __future__ import annotations

import json
import os
import re
import sys

from .extract import _is_line_number, extract_session, read_positions
from .haiku import _parse_response
from .prompts import build_save_prompt, build_ndc_prompt


def _shell_escape(value: str) -> str:
    """Emit a value for the shell variable bridge consumed by ``safe_eval``.

    ``scripts/log.sh:safe_eval`` parses ``KEY=VALUE`` lines and assigns
    ``VALUE`` verbatim via ``printf -v`` — no shell expansion, no ``eval``.
    The only constraint is that ``VALUE`` must not contain a newline
    (the parser is line-oriented).

    Earlier versions single-quote-wrapped per POSIX ``eval`` convention,
    which broke on Windows: paths with backslashes were quoted, but
    ``safe_eval``'s verbatim assignment kept the quotes literal (issue #84).

    Args:
        value: Raw string. Must not contain newlines.

    Returns:
        The value as-is — emission is verbatim to match parser semantics.

    Raises:
        ValueError: If ``value`` contains a newline character.
    """
    if "\n" in value or "\r" in value:
        raise ValueError("shell-bridged values must not contain newlines")
    return value


def cmd_extract(session_id: str, project_dir: str) -> None:
    """Extract session exchanges and print shell variables to stdout.

    Writes the formatted exchange text to a temp file (avoiding shell
    escaping of large text) and prints its path as ``EXTRACT_FILE``.

    Respects the REMEMBER_DIR environment variable for external-mode
    last-save.json lookup.

    Args:
        session_id: UUID of the session to extract.
        project_dir: Root directory of the Claude Code project.

    Prints:
        POSITION, HUMAN_COUNT, ASSISTANT_COUNT, EXCHANGE_COUNT,
        EXTRACT_FILE (path to temp file containing exchange text).
    """
    import tempfile
    remember_dir = os.environ.get("REMEMBER_DIR") or None
    r = extract_session(session_id=session_id, project_dir=project_dir, remember_dir=remember_dir)

    # Write exchanges to temp file (avoids shell escaping of large text)
    fd, extract_file = tempfile.mkstemp(prefix="remember-extract-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as f:
        f.write(r.exchanges)

    print(f"POSITION={r.position}")
    print(f"HUMAN_COUNT={r.human_count}")
    print(f"ASSISTANT_COUNT={r.assistant_count}")
    print(f"EXCHANGE_COUNT={r.human_count + r.assistant_count}")
    print(f"EXTRACT_FILE={_shell_escape(extract_file)}")


def cmd_build_prompt(
    extract_file: str,
    last_entry_file: str,
    time: str,
    branch: str,
    output_file: str,
    max_extract_bytes: int = 0,
) -> None:
    """Build the save-summary prompt and write it to an output file.

    Reads extract and last-entry content from files rather than shell
    arguments, avoiding interpolation issues with large or complex text.

    Args:
        extract_file: Path to the temp file containing extracted exchanges.
        last_entry_file: Path to a file containing the last staging entry.
        time: Current timestamp string (e.g., "14:32").
        branch: Current git branch name.
        output_file: Path where the assembled prompt will be written.
        max_extract_bytes: Upper bound on the extract's UTF-8 byte size. A
            long-lived session can accumulate an extract larger than Haiku's
            context window, making the prompt unsendable and silently halting
            daily rotation (#96). When the extract exceeds this size, keep only
            the most-recent tail (the work worth summarizing) and prepend a
            truncation note. ``0`` disables the cap.
    """
    with open(extract_file, encoding="utf-8", errors="replace") as f:
        extract = f.read().strip()
    with open(last_entry_file, encoding="utf-8", errors="replace") as f:
        last_entry = f.read().strip()

    if max_extract_bytes > 0:
        raw = extract.encode("utf-8")
        if len(raw) > max_extract_bytes:
            kept = raw[-max_extract_bytes:].decode("utf-8", errors="replace")
            extract = (
                f"[NOTE: transcript truncated to the last {max_extract_bytes} "
                f"of {len(raw)} bytes — summarize the most recent work below]"
                f"\n\n{kept}"
            )

    prompt = build_save_prompt(
        time=time,
        branch=branch,
        last_entry=last_entry,
        extract=extract,
    )
    with open(output_file, "w", encoding="utf-8", errors="replace") as f:
        f.write(prompt)


def cmd_build_ndc_prompt(memory_file: str, output_file: str) -> None:
    """Build the NDC compression prompt and write it to an output file.

    Args:
        memory_file: Path to now.md (the file to be compressed).
        output_file: Path where the assembled prompt will be written.
    """
    with open(memory_file, encoding="utf-8", errors="replace") as f:
        content = f.read()
    prompt = build_ndc_prompt(content)
    with open(output_file, "w", encoding="utf-8", errors="replace") as f:
        f.write(prompt)


def cmd_parse_haiku(output_file: str = "") -> None:
    """Parse Haiku JSON response from stdin and print shell variables.

    Reads the raw JSON from stdin, parses it into a HaikuResult, writes
    the text to a temp file (since it can contain newlines, quotes, and
    arbitrary content), and prints metadata as shell variables.

    Args:
        output_file: If non-empty, also writes the Haiku text to this
            path (in addition to the temp file).

    Prints:
        HAIKU_TEXT_FILE (path to temp file), IS_SKIP (true/false),
        TK_IN, TK_OUT, TK_CACHE, TK_COST.
    """
    # Redirected stdin/pipes use the locale codec on Windows (cp1252), not
    # UTF-8 — PEP 528's UTF-8 console only covers interactive consoles. Force
    # UTF-8 so the claude JSON decodes correctly (#91). Guarded: a StringIO
    # substituted in tests has no reconfigure().
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    raw = sys.stdin.read()
    _emit_haiku_result(_parse_response(raw), output_file)


def _emit_haiku_result(r, output_file: str = "") -> None:
    """Write Haiku text to a temp file and print the shell vars bash consumes.

    Shared by ``parse-haiku`` (parse pre-fetched JSON) and ``call-haiku``
    (invoke + parse), so both emit an identical contract:
    HAIKU_TEXT_FILE, IS_SKIP, TK_IN/OUT/CACHE/COST.
    """
    import tempfile

    # Write text to temp file (can contain newlines, quotes, anything)
    fd, text_file = tempfile.mkstemp(prefix="remember-haiku-text-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as f:
        f.write(r.text)

    print(f"HAIKU_TEXT_FILE={_shell_escape(text_file)}")
    print(f"IS_SKIP={'true' if r.is_skip else 'false'}")
    print(f"IS_REJECTED={'true' if r.is_rejected else 'false'}")
    print(f"TK_IN={r.tokens.input}")
    print(f"TK_OUT={r.tokens.output}")
    print(f"TK_CACHE={r.tokens.cache}")
    print(f"TK_COST={r.tokens.cost_usd:.6f}")

    if output_file:
        with open(output_file, "w", encoding="utf-8", errors="replace") as f:
            f.write(r.text)


def cmd_call_haiku(prompt_file: str, output_file: str = "", timeout: int = 120) -> None:
    """Invoke Haiku on the prompt in ``prompt_file`` and print the shell vars.

    The single entry point bash uses to run the summarizer subprocess: the
    ``claude -p`` invocation itself lives only in ``haiku.call_haiku`` (one
    place — no inline duplicate that could drift, #94/#98/#100). ``timeout``
    is forwarded to ``call_haiku`` (NDC compresses a whole now.md and needs a
    longer budget than the per-session summary). On any failure — a missing
    prompt file (OSError) or a claude error (RuntimeError) — prints the error
    to stderr and exits 1 so the caller aborts; never leaks a traceback to
    stdout, which the bash caller captures as the shell-var payload.

    A spawn the guard DECLINED (#204) is not a failure and exits
    ``EXIT_SPAWN_DECLINED`` instead. The difference is load-bearing:
    ``save-session.sh`` counts failures against a span and, past
    ``thresholds.max_summary_failures``, advances the read cursor past it — so
    reporting a working cap as a failure would lose the span it protected.
    """
    from .haiku import call_haiku
    from .spawn_guard import EXIT_SPAWN_DECLINED, SummarizerSpawnDeclined

    try:
        with open(prompt_file, encoding="utf-8", errors="replace") as f:
            prompt = f.read()
        r = call_haiku(prompt, timeout=timeout)
    except SummarizerSpawnDeclined as e:
        print(f"call-haiku declined: {e}", file=sys.stderr)
        sys.exit(EXIT_SPAWN_DECLINED)
    except (OSError, RuntimeError) as e:
        print(f"call-haiku error: {e}", file=sys.stderr)
        sys.exit(1)
    _emit_haiku_result(r, output_file)


#: How many sessions keep a remembered position. Interleaved work is a handful
#: of terminals, not dozens, and the file is read on every tool call.
_POSITION_SLOTS = 32


def cmd_save_position(last_save_file: str, session_id: str, position: int) -> None:
    """Record the current extraction position for this session.

    Positions are keyed by session ID. A single slot meant two live sessions
    overwrote each other: A saves, B saves, and A's next save no longer
    recognises its own ID, resumes from 0, and re-summarizes its whole span as
    duplicate entries (issue #140). Sessions interleave whenever someone runs
    two terminals, or a background/worktree session shares the store.

    The newest ``_POSITION_SLOTS`` sessions are kept, oldest evicted first.
    ``session``/``line`` are still written as a mirror of the most recent save,
    so a reader from an older install — or one mid-upgrade — keeps working.

    Args:
        last_save_file: Path to the last-save.json file.
        session_id: UUID of the session being saved.
        position: JSONL line number to resume from next time.
    """
    sessions = read_positions(last_save_file)
    # Re-insert at the end: dicts keep insertion order, so the oldest entry is
    # simply the first one, and a session that keeps saving keeps its slot.
    sessions.pop(session_id, None)
    sessions[session_id] = position
    while len(sessions) > _POSITION_SLOTS:
        del sessions[next(iter(sessions))]

    payload = {"sessions": sessions, "session": session_id, "line": position}
    # Strict: machine-written structured JSON. session_id is an ASCII UUID
    # (regex-validated upstream) and position is an int, so this never raises;
    # keeping it strict avoids silently U+FFFD-corrupting the recovery file.
    #
    # Written via a temp file and renamed: the read-merge-write above is not
    # atomic, and a reader hitting the file mid-write would see truncated JSON
    # and resume from 0 — the very duplicate this is fixing.
    tmp = f"{last_save_file}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, last_save_file)


def cmd_read_position(last_save_file: str, session_id: str) -> None:
    """Print the saved position for a session, or 0.

    Exists so scripts/post-tool-hook.sh does not need its own JSON parser.
    It had one, and it drifted: while every other reader was taught that a
    bool is not a position and an integral float is, that copy kept a bare
    isinstance check and reported 0 for a position the rest of the pipeline
    resumed from. Five copies of this rule was four too many.

    Args:
        last_save_file: Path to the last-save.json file.
        session_id: Session whose position is wanted.

    Prints:
        The line number, or 0 when this session has no usable position.
    """
    print(read_positions(last_save_file).get(session_id, 0))


def _rotate_archive(archive_file: str) -> str | None:
    """Rename a non-empty archive.md to a dated sibling so consolidation can
    proceed with a fresh archive instead of stalling forever on an archive that
    has grown past the prompt cap.

    Returns the rotated path (e.g. ``archive-2026-06-29.md``, with a ``-N``
    suffix on same-day collisions), or ``None`` when there is nothing worth
    rotating (missing/empty archive -> the oversized bulk is staging/recent, not
    the archive, so rotating would not help).
    """
    if not archive_file or not os.path.exists(archive_file) or os.path.getsize(archive_file) == 0:
        return None
    from ._tz import today_str
    parent = os.path.dirname(archive_file)
    stem = f"archive-{today_str()}"
    target = os.path.join(parent, f"{stem}.md")
    n = 2
    while os.path.exists(target):
        target = os.path.join(parent, f"{stem}-{n}.md")
        n += 1
    os.rename(archive_file, target)
    return target


def _eligible_staging(directory: str, filter_today: bool = True) -> list[str]:
    """Sorted paths of the staging files a consolidation round may consume.

    One answer to "which files are in scope", shared by the snapshot step and
    the read that follows it. Two copies of the predicate would be two chances
    to disagree about which day is today, and the whole point of the snapshot
    is that the second step sees exactly what the first one took under the lock.

    Args:
        directory: Directory to scan.
        filter_today: Whether to exclude today's file. False when scanning a
            snapshot: its contents were already filtered under the lock, and a
            round that crosses midnight must not drop a file it has taken.

    Returns:
        Sorted paths, today's file and ``.done.md`` files excluded.
    """
    import glob as globmod

    from ._tz import today_str

    today = today_str() if filter_today else ""
    eligible = []
    for path in sorted(globmod.glob(os.path.join(directory, "today-*.md"))):
        basename = os.path.basename(path)
        if basename.endswith(".done.md"):
            continue
        if today and today in basename:
            continue
        eligible.append(path)
    return eligible


def cmd_consolidate_snapshot(staging_dir: str, snapshot_dir: str) -> None:
    """Copy the eligible staging files into ``snapshot_dir``.

    run-consolidation.sh calls this while holding staging.lock, so every append
    it sees is whole. ``staging_append`` writes a separator and then the summary
    as two operations, and a reader landing between them consumes a blank line
    as if it were the end of the day — the span retired into ``.done.md`` ends
    with a separator whose entry is not there, and the entry is re-consolidated
    on a later round under a later timestamp (#235).

    The lock could not simply be held across ``cmd_consolidate``: that call
    contains the Haiku round, and a critical section containing a model call is
    how #142 happened and why save.lock was rejected as the staging lock in
    #225. So the critical section ends here, at a process boundary, and the
    bytes cross it on disk. The caller owns ``snapshot_dir`` and its cleanup.

    Args:
        staging_dir: Directory containing ``today-*.md`` staging files.
        snapshot_dir: Directory to copy them into. Created if absent.

    Prints:
        STAGING_COUNT — how many files were snapshotted.
    """
    os.makedirs(snapshot_dir, exist_ok=True)
    count = 0
    for path in _eligible_staging(staging_dir):
        with open(path, "rb") as src:
            raw = src.read()
        with open(os.path.join(snapshot_dir, os.path.basename(path)), "wb") as dst:
            dst.write(raw)
        count += 1
    print(f"STAGING_COUNT={count}")


def cmd_consolidate(staging_dir: str, recent_file: str, archive_file: str,
                    max_prompt_bytes: int = 0, snapshot_dir: str = "") -> None:
    """Run the full consolidation pipeline and print shell variables.

    Collects staging files (excluding today's and ``.done`` files), reads
    current recent and archive content, calls Haiku for consolidation,
    and writes results to temp files.

    Args:
        staging_dir: Directory containing ``today-*.md`` staging files.
        recent_file: Path to the current recent.md file.
        archive_file: Path to the current archive.md file.
        max_prompt_bytes: Skip-guard cap on the assembled consolidation
            prompt's UTF-8 byte size. ``0`` disables it. An oversized prompt
            yields ``CONSOLIDATION_STATUS=skip`` instead of overflowing.
        snapshot_dir: Directory of copies taken under staging.lock by
            cmd_consolidate_snapshot. Read instead of ``staging_dir`` when set;
            the paths emitted for the retire step still name ``staging_dir``,
            because the basenames are identical on both sides. Empty reads the
            live directory — the pre-#235 behaviour, kept so a caller that has
            not been taught the two-step still works.

    Prints:
        STAGING_COUNT (0 if nothing to consolidate), RECENT_OUT and
        ARCHIVE_OUT (paths to temp files with new content), TK_IN,
        TK_OUT, TK_CACHE, TK_COST, and one STAGING line per processed
        staging file (for the shell rename step).
    """
    import tempfile

    from .consolidate import consolidate, ConsolidationSkipped, ConsolidationTooLarge
    from .spawn_guard import EXIT_SPAWN_DECLINED, SummarizerSpawnDeclined

    # Read the snapshot taken under staging.lock when there is one, the live
    # directory otherwise. Either way the basenames are the staging basenames,
    # so the paths emitted for the retire loop below still name the real files.
    source_dir = snapshot_dir or staging_dir

    staging_contents: dict[str, str] = {}
    # Raw file size, captured at read time. The consumed-byte count below drives
    # the retire-vs-keep-tail split, so it has to describe the FILE, not the
    # decoded string: errors="replace" turns each undecodable byte into one
    # U+FFFD that re-encodes to three, and len(decoded.encode()) then overstates
    # the file. Overstated, `staging_now -gt staging_consumed` reads false in
    # run-consolidation.sh and it falls through to the blind rename — sealing
    # concurrently appended entries inside .done.md, which is the exact loss
    # this count exists to prevent. #142 measures now.md with `wc -c` for the
    # same reason.
    staging_raw_bytes: dict[str, int] = {}
    for path in _eligible_staging(source_dir, filter_today=not snapshot_dir):
        basename = os.path.basename(path)
        with open(path, "rb") as f:
            raw = f.read()
        staging_raw_bytes[basename] = len(raw)
        staging_contents[basename] = raw.decode("utf-8", errors="replace")

    if not staging_contents:
        print("STAGING_COUNT=0")
        return

    def _emit_skip() -> None:
        # Skip status so the shell leaves recent.md/archive.md untouched and does
        # NOT rename the source staging files to .done.md — they remain available
        # for the next run. STAGING_COUNT is non-zero (we found files) but the
        # shell gates on CONSOLIDATION_STATUS.
        print(f"STAGING_COUNT={len(staging_contents)}")
        print("CONSOLIDATION_STATUS=skip")

    # Size the store before reading it (#346). The cap is enforced on the
    # assembled prompt, so the whole store had to be read into memory and a
    # prompt built around it before the pipeline was allowed to notice it was
    # too large to send: several times the store's size in allocation to reach
    # a decision ``stat`` answers for free. Against the reporter's 6.4 GB
    # recent.md that is what took the machine down, from a script that runs
    # disowned beside a live session.
    #
    # It can never be a false skip. The assembled prompt is the template plus
    # per-file labels plus these bytes, so it is strictly larger than their
    # sum, and a sum already over the cap is proof the prompt would be.
    rotated: str | None = None

    def _restore_rotation() -> None:
        """Undo an up-front rotation when the round did not go through.

        Existence-checked because the handlers inside ``ConsolidationTooLarge``
        below do their own restore and then re-raise; an exception raised
        inside an except clause does not re-enter its siblings, but the guard
        keeps that a property of this function rather than of Python's
        control flow.
        """
        if rotated is not None and os.path.exists(rotated):
            os.replace(rotated, archive_file)

    recent_size = os.path.getsize(recent_file) if os.path.exists(recent_file) else 0
    archive_size = os.path.getsize(archive_file) if os.path.exists(archive_file) else 0
    if max_prompt_bytes > 0:
        embedded = sum(staging_raw_bytes.values()) + recent_size + archive_size
        if embedded > max_prompt_bytes:
            # Same recovery ConsolidationTooLarge gets below, taken before the
            # read rather than after it: if archive.md is the bulk, rotate it
            # to a dated sibling and carry on with a fresh one. Only when
            # dropping it still would not fit — recent.md is the bulk, #346's
            # shape — is nothing read at all.
            if embedded - archive_size <= max_prompt_bytes:
                rotated = _rotate_archive(archive_file)
            if rotated is None:
                _emit_skip()
                return
            archive_size = 0

    recent = ""
    if os.path.exists(recent_file):
        with open(recent_file, encoding="utf-8", errors="replace") as f:
            recent = f.read()

    archive = ""
    if os.path.exists(archive_file):
        with open(archive_file, encoding="utf-8", errors="replace") as f:
            archive = f.read()

    try:
        result = consolidate(staging_contents, recent, archive,
                             max_prompt_bytes=max_prompt_bytes)
    except ConsolidationTooLarge:
        # archive.md is the bulk of the oversized prompt. Rotate it to a dated
        # sibling (memory preserved in cold storage) and retry once with a fresh
        # empty archive, so consolidation keeps progressing instead of skipping
        # every run forever. If there is nothing to rotate, or the retry still
        # overflows (staging + recent alone exceed the cap), restore and skip.
        # Only reachable when the stat guard above let the round through and
        # the template plus per-file labels tipped it over, or when the guard
        # is disabled. An up-front rotation has already happened in the first
        # case, so do not rotate a second time and orphan the first sibling.
        if rotated is None:
            rotated = _rotate_archive(archive_file)
        if rotated is None:
            _emit_skip()
            return
        try:
            result = consolidate(staging_contents, recent, "",
                                 max_prompt_bytes=max_prompt_bytes)
        except ConsolidationSkipped:
            os.replace(rotated, archive_file)  # still too big -> undo, skip
            _emit_skip()
            return
        except Exception:
            os.replace(rotated, archive_file)  # retry errored -> undo, re-raise
            raise
    except SummarizerSpawnDeclined as declined:
        # The spawn guard refused (#204). Staging is left exactly as it is and
        # the next run consolidates it — not a skip, which retires staging, and
        # not a failure either. An up-front rotation is undone for the same
        # reason: this round never happened, so nothing it moved may persist.
        _restore_rotation()
        print(f"consolidate declined: {declined}", file=sys.stderr)
        sys.exit(EXIT_SPAWN_DECLINED)
    except ConsolidationSkipped:
        # Model declined (SKIP), returned non-conforming output, or returned
        # more bytes than the pipeline is willing to write (#346).
        _restore_rotation()
        _emit_skip()
        return
    except Exception:
        # A transient failure (the model call erroring, say) must not leave the
        # up-front rotation applied: nothing was consolidated, so archive.md
        # has to be where the next run expects it. The post-hoc rotation path
        # has restored itself on this branch since #123; the pre-read one owes
        # the same guarantee.
        _restore_rotation()
        raise

    # Write results to temp files
    fd_r, recent_out = tempfile.mkstemp(prefix="remember-recent-", suffix=".md")
    with os.fdopen(fd_r, "w", encoding="utf-8", errors="replace") as f:
        f.write(result.recent)

    fd_a, archive_out = tempfile.mkstemp(prefix="remember-archive-", suffix=".md")
    with os.fdopen(fd_a, "w", encoding="utf-8", errors="replace") as f:
        f.write(result.archive)

    # Write staging paths to a NUL-separated temp file so the shell rename step
    # can read them safely regardless of single quotes, spaces, or other
    # metacharacters in the filename.  Shell reads with:
    #   while IFS= read -r -d '' path; do ...; done < "$STAGING_PATHS_FILE"
    # Each record is path\0consumed_bytes\0. The byte count is what was actually
    # read into this prompt: run-consolidation.sh renames the file afterwards,
    # and a save can land in between — the Haiku call above has a 180s budget
    # and consolidation runs disowned alongside any live session. Renaming
    # blindly sealed those newer bytes inside the .done.md, which nothing globs
    # again and session start never injects: written to disk, then unreachable.
    # Same shape as #142, which fixed it for now.md and not for staging.
    fd_s, staging_paths_file = tempfile.mkstemp(prefix="remember-staging-paths-", suffix=".bin")
    with os.fdopen(fd_s, "wb") as f:
        for name in staging_contents:
            # surrogatepass: os.listdir() surrogate-escapes undecodable filename
            # bytes on Windows; round-trip them so the shell gets the real path.
            f.write(os.path.join(staging_dir, name).encode("utf-8", "surrogatepass") + b"\x00")
            f.write(str(staging_raw_bytes[name]).encode("ascii") + b"\x00")

    print(f"STAGING_COUNT={len(staging_contents)}")
    print("CONSOLIDATION_STATUS=ok")
    print(f"RECENT_OUT={_shell_escape(recent_out)}")
    print(f"ARCHIVE_OUT={_shell_escape(archive_out)}")
    print(f"TK_IN={result.tokens.input}")
    print(f"TK_OUT={result.tokens.output}")
    print(f"TK_CACHE={result.tokens.cache}")
    print(f"TK_COST={result.tokens.cost_usd:.6f}")
    print(f"STAGING_PATHS_FILE={_shell_escape(staging_paths_file)}")


def main() -> None:
    """CLI dispatcher for ``python3 -m pipeline.shell <command> [args]``.

    Routes the first positional argument to the corresponding ``cmd_*``
    function, passing remaining arguments positionally. Exits with
    status 1 on unknown commands or missing arguments.
    """
    # Every cmd_* funnels its KEY=value lines through print(), and bash captures
    # them by command substitution to pass on as argv to the next call. On
    # Windows print() encodes with the console's ANSI codepage, not UTF-8 — the
    # same boundary class as #91/#104, on the output side this time — so a temp
    # path under a non-ASCII profile came back mojibake and the very next step
    # failed with FileNotFoundError on a file that existed (issue #145). Same
    # guard as the stdin reconfigure in cmd_parse_haiku: tests substitute a
    # StringIO, which has no reconfigure().
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        print("Usage: python3 -m pipeline.shell <command> [args]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "extract":
        cmd_extract(session_id=sys.argv[2], project_dir=sys.argv[3])
    elif cmd == "build-prompt":
        cmd_build_prompt(
            extract_file=sys.argv[2],
            last_entry_file=sys.argv[3],
            time=sys.argv[4],
            branch=sys.argv[5],
            output_file=sys.argv[6],
            max_extract_bytes=int(sys.argv[7]) if len(sys.argv) > 7 else 0,
        )
    elif cmd == "build-ndc-prompt":
        cmd_build_ndc_prompt(memory_file=sys.argv[2], output_file=sys.argv[3])
    elif cmd == "parse-haiku":
        output_file = sys.argv[2] if len(sys.argv) > 2 else ""
        cmd_parse_haiku(output_file=output_file)
    elif cmd == "call-haiku":
        output_file = sys.argv[3] if len(sys.argv) > 3 else ""
        timeout = int(sys.argv[4]) if len(sys.argv) > 4 else 120
        cmd_call_haiku(prompt_file=sys.argv[2], output_file=output_file, timeout=timeout)
    elif cmd == "read-position":
        cmd_read_position(last_save_file=sys.argv[2], session_id=sys.argv[3])
    elif cmd == "save-position":
        cmd_save_position(
            last_save_file=sys.argv[2],
            session_id=sys.argv[3],
            position=int(sys.argv[4]),
        )
    elif cmd == "consolidate-snapshot":
        cmd_consolidate_snapshot(staging_dir=sys.argv[2], snapshot_dir=sys.argv[3])
    elif cmd == "consolidate":
        cmd_consolidate(
            staging_dir=sys.argv[2],
            recent_file=sys.argv[3],
            archive_file=sys.argv[4],
            max_prompt_bytes=int(sys.argv[5]) if len(sys.argv) > 5 else 0,
            snapshot_dir=sys.argv[6] if len(sys.argv) > 6 else "",
        )
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
