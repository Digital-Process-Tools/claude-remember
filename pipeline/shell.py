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
    save-position   Write position to last-save.json
    consolidate     Run full consolidation pipeline

"""

from __future__ import annotations

import json
import os
import sys

from .extract import extract_session
from .haiku import _parse_response
from .prompts import build_save_prompt, build_ndc_prompt


def _shell_escape(value: str) -> str:
    """Escape a string for safe shell ``eval`` by single-quote wrapping.

    Args:
        value: Raw string that may contain spaces, quotes, or special chars.

    Returns:
        Single-quoted string safe for shell evaluation. Internal single
        quotes are escaped via the ``'\\''`` idiom.
    """
    return "'" + value.replace("'", "'\\''") + "'"


def cmd_extract(session_id: str, project_dir: str) -> None:
    """Extract session exchanges and print shell variables to stdout.

    Writes the formatted exchange text to a temp file (avoiding shell
    escaping of large text) and prints its path as ``EXTRACT_FILE``.

    Args:
        session_id: UUID of the session to extract.
        project_dir: Root directory of the Claude Code project.

    Prints:
        POSITION, HUMAN_COUNT, ASSISTANT_COUNT, EXCHANGE_COUNT,
        EXTRACT_FILE (path to temp file containing exchange text).
    """
    import tempfile
    r = extract_session(session_id=session_id, project_dir=project_dir)

    # Write exchanges to temp file (avoids shell escaping of large text)
    fd, extract_file = tempfile.mkstemp(prefix="remember-extract-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
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
    """
    with open(extract_file, encoding="utf-8") as f:
        extract = f.read().strip()
    with open(last_entry_file, encoding="utf-8") as f:
        last_entry = f.read().strip()

    prompt = build_save_prompt(
        time=time,
        branch=branch,
        last_entry=last_entry,
        extract=extract,
    )
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(prompt)


def cmd_build_ndc_prompt(memory_file: str, output_file: str) -> None:
    """Build the NDC compression prompt and write it to an output file.

    Args:
        memory_file: Path to now.md (the file to be compressed).
        output_file: Path where the assembled prompt will be written.
    """
    with open(memory_file, encoding="utf-8") as f:
        content = f.read()
    prompt = build_ndc_prompt(content)
    with open(output_file, "w", encoding="utf-8") as f:
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
    import tempfile
    raw = sys.stdin.read()
    r = _parse_response(raw)

    # Write text to temp file (can contain newlines, quotes, anything)
    fd, text_file = tempfile.mkstemp(prefix="remember-haiku-text-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(r.text)

    print(f"HAIKU_TEXT_FILE={_shell_escape(text_file)}")
    print(f"IS_SKIP={'true' if r.is_skip else 'false'}")
    print(f"TK_IN={r.tokens.input}")
    print(f"TK_OUT={r.tokens.output}")
    print(f"TK_CACHE={r.tokens.cache}")
    print(f"TK_COST={r.tokens.cost_usd:.6f}")

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(r.text)


def cmd_save_position(last_save_file: str, session_id: str, position: int) -> None:
    """Write the current extraction position to last-save.json.

    Stores the session ID and line number so the next extraction can
    resume from where this one left off (incremental extraction).

    Args:
        last_save_file: Path to the last-save.json file.
        session_id: UUID of the session being saved.
        position: JSONL line number to resume from next time.
    """
    with open(last_save_file, "w", encoding="utf-8") as f:
        json.dump({"session": session_id, "line": position}, f)


def cmd_consolidate(staging_dir: str, recent_file: str, archive_file: str) -> None:
    """Run the full consolidation pipeline and print shell variables.

    Collects staging files (excluding today's and ``.done`` files), reads
    current recent and archive content, calls Haiku for consolidation,
    and writes results to temp files.

    Args:
        staging_dir: Directory containing ``today-*.md`` staging files.
        recent_file: Path to the current recent.md file.
        archive_file: Path to the current archive.md file.

    Prints:
        STAGING_COUNT (0 if nothing to consolidate), RECENT_OUT and
        ARCHIVE_OUT (paths to temp files with new content), TK_IN,
        TK_OUT, TK_CACHE, TK_COST, and one STAGING line per processed
        staging file (for the shell rename step).
    """
    import glob as globmod
    import tempfile

    from ._tz import today_str
    from .consolidate import consolidate

    today = today_str()

    # Find staging files (exclude today + .done files)
    staging_contents: dict[str, str] = {}
    for path in sorted(globmod.glob(os.path.join(staging_dir, "today-*.md"))):
        basename = os.path.basename(path)
        if today in basename or basename.endswith(".done.md"):
            continue
        with open(path, encoding="utf-8") as f:
            staging_contents[basename] = f.read()

    if not staging_contents:
        print("STAGING_COUNT=0")
        return

    recent = ""
    if os.path.exists(recent_file):
        with open(recent_file, encoding="utf-8") as f:
            recent = f.read()

    archive = ""
    if os.path.exists(archive_file):
        with open(archive_file, encoding="utf-8") as f:
            archive = f.read()

    result = consolidate(staging_contents, recent, archive)

    # Write results to temp files
    fd_r, recent_out = tempfile.mkstemp(prefix="remember-recent-", suffix=".md")
    with os.fdopen(fd_r, "w", encoding="utf-8") as f:
        f.write(result.recent)

    fd_a, archive_out = tempfile.mkstemp(prefix="remember-archive-", suffix=".md")
    with os.fdopen(fd_a, "w", encoding="utf-8") as f:
        f.write(result.archive)

    print(f"STAGING_COUNT={len(staging_contents)}")
    print(f"RECENT_OUT={_shell_escape(recent_out)}")
    print(f"ARCHIVE_OUT={_shell_escape(archive_out)}")
    print(f"TK_IN={result.tokens.input}")
    print(f"TK_OUT={result.tokens.output}")
    print(f"TK_CACHE={result.tokens.cache}")
    print(f"TK_COST={result.tokens.cost_usd:.6f}")

    # Print staging file basenames for rename step
    for name in staging_contents:
        print(f"STAGING={_shell_escape(os.path.join(staging_dir, name))}")


def main() -> None:
    """CLI dispatcher for ``python3 -m pipeline.shell <command> [args]``.

    Routes the first positional argument to the corresponding ``cmd_*``
    function, passing remaining arguments positionally. Exits with
    status 1 on unknown commands or missing arguments.
    """
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
        )
    elif cmd == "build-ndc-prompt":
        cmd_build_ndc_prompt(memory_file=sys.argv[2], output_file=sys.argv[3])
    elif cmd == "parse-haiku":
        output_file = sys.argv[2] if len(sys.argv) > 2 else ""
        cmd_parse_haiku(output_file=output_file)
    elif cmd == "save-position":
        cmd_save_position(
            last_save_file=sys.argv[2],
            session_id=sys.argv[3],
            position=int(sys.argv[4]),
        )
    elif cmd == "consolidate":
        cmd_consolidate(
            staging_dir=sys.argv[2],
            recent_file=sys.argv[3],
            archive_file=sys.argv[4],
        )
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
