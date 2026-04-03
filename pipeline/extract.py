"""Session JSONL parser — extract human/assistant exchanges from Claude Code sessions.

Reads Claude Code session JSONL files, filters out metadata and system messages,
and produces formatted text with role-labeled exchanges suitable for
summarization by Haiku.

Supports incremental extraction (only new messages since last save) and
full extraction (all messages or last N).

Callable as module::

    python3 -m pipeline.extract --session <id>
    python3 -m pipeline.extract 10          # last 10 exchanges
    python3 -m pipeline.extract --all       # everything

Or imported::

    from pipeline.extract import extract_session
    result = extract_session(session_id="abc123", count=5)
"""

import json
import glob
import os
import re
import sys

from .types import ExtractResult

# Session directory computed from project root
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(_SCRIPT_DIR)))


def _session_dir(project_dir: str) -> str:
    """Convert a project directory path to its Claude sessions directory.

    Replaces all non-alphanumeric characters with dashes, matching the
    bash pattern: sed 's/[^a-zA-Z0-9]/-/g'. Handles Unix (/), Windows
    backslashes (\\) and drive colons (D:).
    """
    slug = re.sub(r'[^a-zA-Z0-9]', '-', project_dir)
    return os.path.expanduser("~/.claude/projects/" + slug)


def _last_save_path(project_dir: str) -> str:
    """Return the path to last-save.json for incremental extraction."""
    return os.path.join(project_dir, ".remember", "tmp", "last-save.json")


def _validate_session_id(session_id: str) -> None:
    """Reject session IDs containing path traversal characters."""
    if "/" in session_id or "\\" in session_id or ".." in session_id:
        raise ValueError(f"invalid session_id: {session_id}")


def find_session(session_id: str | None = None,
                 project_dir: str = _DEFAULT_PROJECT_DIR) -> str:
    """Locate a session JSONL file by ID, or find the most recent one.

    Args:
        session_id: UUID of a specific session. If None, returns the
            most recently modified JSONL file in the sessions directory.
        project_dir: Root directory of the Claude Code project.

    Returns:
        Absolute path to the session JSONL file.

    Raises:
        FileNotFoundError: If no session files exist in the directory.
    """
    sdir = _session_dir(project_dir)
    if session_id:
        _validate_session_id(session_id)
        path = os.path.join(sdir, session_id + ".jsonl")
        if os.path.exists(path):
            return path
    files = glob.glob(os.path.join(sdir, "*.jsonl"))
    if not files:
        raise FileNotFoundError(f"no session files in {sdir}")
    return max(files, key=os.path.getmtime)


def get_last_save_line(session_id: str,
                       project_dir: str = _DEFAULT_PROJECT_DIR) -> int:
    """Return the JSONL line number where the last save happened.

    Reads ``last-save.json`` and returns the saved line position if it
    matches the given session ID. Returns 0 if the file is missing,
    corrupt, or belongs to a different session.

    Args:
        session_id: Session UUID to match against the saved position.
        project_dir: Root directory of the Claude Code project.

    Returns:
        Line number (0-indexed) to resume extraction from, or 0.
    """
    path = _last_save_path(project_dir)
    if not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("session") == session_id:
            return data.get("line", 0)
    except (json.JSONDecodeError, KeyError, OSError):
        pass
    return 0


def count_lines(path: str) -> int:
    """Count total lines in a JSONL file.

    Args:
        path: Absolute path to the JSONL file.

    Returns:
        Number of lines in the file.
    """
    count = 0
    with open(path, encoding="utf-8") as f:
        for _ in f:
            count += 1
    return count


def extract_messages(path: str, skip_lines: int = 0) -> list[tuple[str, str]]:
    """Parse a session JSONL file into role-labeled message tuples.

    Reads each line as JSON, skips metadata messages and system reminders,
    and extracts readable text from both string and block-list content
    formats. Tool use blocks are condensed into short summaries.

    Args:
        path: Absolute path to the session JSONL file.
        skip_lines: Number of lines to skip from the beginning (for
            incremental extraction after a previous save).

    Returns:
        List of ``("HUMAN", text)`` or ``("AGENT", text)`` tuples,
        one per message, in chronological order.
    """
    messages: list[tuple[str, str]] = []
    corrupt_count = 0

    try:
        f = open(path, encoding="utf-8")
    except OSError:
        return messages

    with f:
        for line_num, line in enumerate(f):
            if line_num < skip_lines:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                corrupt_count += 1
                continue

            msg_type = obj.get("type")
            is_meta = obj.get("isMeta", False)
            if msg_type not in ("user", "assistant") or is_meta:
                continue

            content = obj.get("message", {}).get("content", "")
            texts = _extract_texts(content)

            if texts:
                combined = "\n".join(texts)
                role = "HUMAN" if msg_type == "user" else "AGENT"
                messages.append((role, combined))

    return messages


def _extract_texts(content) -> list[str]:
    """Extract readable text from message content (string or block list).

    Args:
        content: Raw message content — either a plain string or a list
            of content blocks (text, tool_use, etc.).

    Returns:
        List of non-empty text strings. System reminders and command
        tags are filtered out. Tool use blocks are formatted as
        short summaries.
    """
    texts: list[str] = []

    if isinstance(content, str):
        if "<system-reminder>" in content or "<command-name>" in content or "<local-command" in content:
            return texts
        stripped = content.strip()
        if stripped:
            texts.append(stripped)

    elif isinstance(content, list):
        for block in content:
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "").strip()
                if text:
                    texts.append(text)
            elif btype == "tool_use":
                texts.append(_format_tool_use(block))

    return texts


def _format_tool_use(block: dict) -> str:
    """Format a tool_use block as a compact [TOOL: Name detail] summary.

    Args:
        block: A tool_use content block dict with "name" and "input" keys.

    Returns:
        Short string like [TOOL: Read config.ini] or [TOOL: Bash 'git status'].
    """
    name = block.get("name", "?")
    inp = block.get("input", {})

    if name in ("Edit", "Read", "Write"):
        filename = inp.get("file_path", "?").split("/")[-1]
        return f"[TOOL: {name} {filename}]"
    elif name == "Bash":
        cmd = inp.get("command", "?")[:80]
        return f"[TOOL: Bash `{cmd}`]"
    elif name in ("Grep", "Glob"):
        return f"[TOOL: {name} '{inp.get('pattern', '?')}']"
    else:
        return f"[TOOL: {name}]"


def extract_session(
    session_id: str | None = None,
    project_dir: str = _DEFAULT_PROJECT_DIR,
    count: int | None = None,
    show_all: bool = False,
) -> ExtractResult:
    """Main entry point: extract exchanges from a session.

    Args:
        session_id: Specific session to extract. None = latest.
        project_dir: Project root directory.
        count: If set, return only the last N exchanges.
        show_all: If True, extract from line 0.

    Returns:
        ExtractResult with formatted exchanges and position.

    Raises:
        FileNotFoundError: If no matching session JSONL file is found.
    """
    path = find_session(session_id, project_dir)
    actual_id = os.path.basename(path).replace(".jsonl", "")
    total_lines = count_lines(path)

    if show_all:
        messages = extract_messages(path, skip_lines=0)
    elif count is not None:
        messages = extract_messages(path, skip_lines=0)
        messages = messages[-count:]
    else:
        last_line = get_last_save_line(actual_id, project_dir)
        messages = extract_messages(path, skip_lines=last_line)

    # Format as text
    lines = [f"Session: {actual_id}", f"Lines: {total_lines}", "=" * 60]
    human_count = 0
    assistant_count = 0
    for role, text in messages:
        lines.append(f"\n[{role}]")
        lines.append(text)
        lines.append("-" * 40)
        if role == "HUMAN":
            human_count += 1
        else:
            assistant_count += 1

    return ExtractResult(
        exchanges="\n".join(lines),
        position=total_lines,
        human_count=human_count,
        assistant_count=assistant_count,
    )


def main() -> None:
    """CLI entry point for ``python3 -m pipeline.extract``.

    Parses command-line arguments and prints extracted exchanges to
    stdout. Supports ``--session <id>``, ``--project-dir <path>``,
    ``--all``, ``--json``, and a bare integer for last-N extraction.
    """
    count = None
    show_all = False
    target_session = None
    project_dir = _DEFAULT_PROJECT_DIR

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--all":
            show_all = True
        elif args[i] == "--session" and i + 1 < len(args):
            target_session = args[i + 1]
            i += 1
        elif args[i] == "--project-dir" and i + 1 < len(args):
            project_dir = args[i + 1]
            i += 1
        elif args[i] == "--json":
            pass  # handled below
        else:
            try:
                count = int(args[i])
            except ValueError:
                print(f"Usage: python3 -m pipeline.extract [N|--all|--session ID]",
                      file=sys.stderr)
                sys.exit(1)
        i += 1

    result = extract_session(
        session_id=target_session,
        project_dir=project_dir,
        count=count,
        show_all=show_all,
    )

    if "--json" in sys.argv:
        import json as _json
        print(_json.dumps({
            "exchanges": result.exchanges,
            "position": result.position,
            "human_count": result.human_count,
            "assistant_count": result.assistant_count,
        }))
    else:
        print(result.exchanges)
        print(f"\n__POSITION__:{result.position}")


if __name__ == "__main__":
    main()
