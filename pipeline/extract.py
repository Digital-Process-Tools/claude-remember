"""Session JSONL parser — extract human/assistant exchanges from a host's transcript.

Reads a session JSONL file, filters out metadata and system messages, and
produces formatted text with role-labeled exchanges suitable for
summarization by Haiku. Two transcript envelopes are understood -- Claude
Code's and Codex's -- via ``sniff_file_envelope()`` and the per-host
adapters in ``pipeline/host.py`` (#443); a third, unrecognised, shape is
reported rather than silently read as an empty session.

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

from __future__ import annotations

import json
import glob
import os
import re
import sys

from . import host as _host
from .slug import session_dir_slug
from .types import ExtractResult

# Session directory computed from project root
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(_SCRIPT_DIR)))


def _session_dir(project_dir: str) -> str:
    """Convert a project directory path to its Claude sessions directory.

    The slug itself lives in pipeline/slug.py, which is also what the shell
    side calls for the over-long-path hash. It used to be a plain
    ``re.sub(r'[^a-zA-Z0-9]', '-', ...)`` here: Python strings are
    codepoint-based while Claude Code's regex has no ``/u`` flag and walks
    UTF-16 code units, so the two disagreed on astral characters — an emoji or
    an Extension-B kanji in the path produced a directory Claude Code never
    created, and nothing ever saved (#174). It also never truncated at 200
    characters, so a deep enough project path missed too (#157).
    """
    slug = session_dir_slug(project_dir)
    # CLAUDE_CONFIG_DIR relocates Claude Code's whole config tree, projects/
    # included (#166). Hardcoding ~/.claude meant reading a directory with no
    # current transcripts — the save pipeline no-oped in silence, and a stale
    # transcript sitting in the default tree could be summarized into memory
    # as though it were the live session.
    #
    # Honor HOME explicitly so test fixtures patching only HOME also work on Windows
    # (where os.path.expanduser defaults to USERPROFILE, ignoring HOME).
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        home = os.environ.get("HOME") or os.path.expanduser("~")
        config_dir = home + "/.claude"
    return config_dir.rstrip("/\\") + "/projects/" + slug


def _is_line_number(value: object) -> bool:
    """Whether a stored position is a usable line number.

    Two traps, both found by review. `bool` subclasses `int` in Python, so a
    hand-edited ``true`` would sail through an ``isinstance(v, int)`` check and
    behave as line 1. And jq — which session-start-hook.sh reads the same file
    with — cannot tell ``1.0`` from ``1``: it parses every number to a double
    and prints both as ``1``. So an integral float has to count here too, or
    the hook calls a session saved while this reader resumes it from 0 and
    re-summarizes the whole span, which is the duplicate #140 exists to
    prevent. Fractions are rejected on both sides.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


def read_positions(last_save_file: str) -> dict[str, int]:
    """Read the session→position map, tolerating every shape the file can take.

    THE one implementation. It lived in two python modules and drifted twice:
    this module's copy was missing the top-level type guard below, so a file
    that was valid JSON but not an object (``[]``, ``null``, ``42`` — a
    truncated write landing on a byte boundary that still parses, or a hand
    edit) raised AttributeError inside extract_session. That runs in a
    backgrounded, disowned process with stderr discarded, so the save died
    silently and kept dying on every later run while the file stayed that
    shape. Lost memory, with nothing to notice it by.

    Args:
        last_save_file: Path to the last-save.json file.

    Returns:
        Mapping of session ID to line number; empty if unreadable.
    """
    try:
        # Strict on purpose: this is machine-written structured JSON, not user
        # prose. A corrupt byte must fail cleanly (-> resume from the start)
        # rather than be patched with U+FFFD into a wrong line number that
        # silently skips or re-processes messages.
        with open(last_save_file, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    sessions = data.get("sessions")
    if isinstance(sessions, dict):
        return {k: int(v) for k, v in sessions.items() if _is_line_number(v)}
    # Pre-#140 file: one session, one line. Carry it over rather than dropping
    # it, or the first save after an upgrade re-summarizes from the start.
    if isinstance(data.get("session"), str) and _is_line_number(data.get("line")):
        return {data["session"]: int(data["line"])}
    return {}


def _last_save_path(project_dir: str, remember_dir: str | None = None) -> str:
    """Return the path to last-save.json for incremental extraction.

    Uses REMEMBER_DIR env var when set, so external-mode paths work
    without changing the call signature everywhere.
    """
    # POSIX-style join: this path is consumed by bash hooks (Git Bash on Windows accepts /),
    # and keeps it portable across platforms without os.path.join inserting backslashes on Windows.
    effective = remember_dir or os.environ.get("REMEMBER_DIR") or (project_dir.rstrip("/\\") + "/.remember")
    return effective.rstrip("/\\") + "/tmp/last-save.json"


def _unread_envelope_path(project_dir: str, remember_dir: str | None = None) -> str:
    """Path to the unread-envelope quarantine sidecar (#450).

    Sibling of last-save.json, in the same tmp/ directory, keyed the same
    way (by session ID) -- but it is a SEPARATE file rather than a field
    inside last-save.json, because the two have different lifetimes: the
    saved position always advances (that is what keeps #147's retry loop
    closed), while an entry here should survive until something has
    actually read the span it names, however many runs that takes.
    """
    effective = remember_dir or os.environ.get("REMEMBER_DIR") or (project_dir.rstrip("/\\") + "/.remember")
    return effective.rstrip("/\\") + "/tmp/unread-envelope.json"


def read_unread_envelope_status(path: str) -> tuple[dict[str, int], bool]:
    """Read the session -> earliest-unread-line quarantine map, plus
    whether the sidecar was there and could not be trusted (#458).

    A missing sidecar -- nothing was ever quarantined for this project --
    and an unreadable one -- a torn write from a crash mid-write, a disk
    fault, a truncated file -- both have no entries to offer, but they are
    not the same fact: the first is the ordinary case, correct to render
    silently; the second means a caller resuming extraction from "no
    quarantine on record" may be re-losing a span #450 already caught once.

    Args:
        path: Path to unread-envelope.json.

    Returns:
        ``(sessions, unreadable)`` -- ``sessions`` maps session ID to the
        earliest JSONL line not yet successfully read, empty in both the
        absent and the unreadable case. ``unreadable`` is True only when
        the file EXISTS but its contents could not be trusted (parse
        failure, an OS-level read error, or a shape that is not the
        expected mapping) -- never for a sidecar that simply is not there.
    """
    if not os.path.exists(path):
        return {}, False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return {}, True
    if not isinstance(data, dict):
        return {}, True
    return {k: int(v) for k, v in data.items() if _is_line_number(v)}, False


def read_unread_envelope(path: str) -> dict[str, int]:
    """Read the session -> earliest-unread-line quarantine map.

    Tolerates every shape the file can take, the same way ``read_positions``
    does: a corrupt or missing file reads as "nothing quarantined" rather
    than raising, because this is a best-effort recovery aid, not the
    source of truth for the saved position. Callers that need to tell
    "nothing quarantined" apart from "the sidecar exists and could not be
    read" -- #458 -- want ``read_unread_envelope_status()`` instead; this
    function's contract (a plain mapping, corrupt-or-absent both empty) is
    kept unchanged for every existing caller.

    Args:
        path: Path to unread-envelope.json.

    Returns:
        Mapping of session ID to the earliest JSONL line not yet
        successfully read; empty if unreadable or absent.
    """
    return read_unread_envelope_status(path)[0]


def _write_unread_envelope(path: str, sessions: dict[str, int]) -> None:
    """Write the quarantine map atomically (temp file + rename)."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sessions, f)
    os.replace(tmp, path)


def mark_unread_envelope(path: str, session_id: str, from_line: int) -> None:
    """Record that ``session_id`` has an unread span starting at ``from_line``.

    Set-if-absent: an existing entry is never overwritten with a LATER
    value, because doing so would let the quarantined point creep forward
    across repeated "still unrecognised" runs and quietly narrow the span a
    future, envelope-aware build would recover. In practice the caller
    always passes back the same value it read (extraction resumes FROM the
    quarantine point once one is set), so this is a safety net rather than
    the normal path.
    """
    sessions = read_unread_envelope(path)
    if session_id in sessions:
        return
    sessions[session_id] = from_line
    _write_unread_envelope(path, sessions)


def clear_unread_envelope(path: str, session_id: str) -> None:
    """Drop ``session_id``'s quarantine entry, if any.

    Called once something has actually read starting from the quarantined
    point -- a successful save whose envelope was not "unrecognised" -- so
    the span is not re-read forever. A no-op if there is nothing to clear,
    including when the sidecar itself does not exist yet.
    """
    sessions = read_unread_envelope(path)
    if session_id not in sessions:
        return
    del sessions[session_id]
    _write_unread_envelope(path, sessions)


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

    A supplied ``REMEMBER_TRANSCRIPT_PATH`` (via ``_host.transcript_path()``)
    is returned here before ``_validate_session_id()``'s own traversal check
    below ever runs. That is not an oversight (#431): the hooks with no
    payload of their own to offer (``scripts/post-tool-hook.sh``,
    ``scripts/user-prompt-hook.sh``) clear the variable before this module is
    ever imported, and every OTHER caller of this function -- a manual
    ``scripts/save-session.sh``, ``scripts/doctor.sh``, a direct
    ``python3 -m pipeline.extract`` -- trusts its own process environment by
    design, the same way it already trusts ``$PATH`` or ``$HOME``. See
    ``pipeline/host.transcript_path()``'s docstring for why a containment
    check was rejected rather than merely deferred.
    """
    supplied = _host.transcript_path()
    if supplied:
        return supplied
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
                       project_dir: str = _DEFAULT_PROJECT_DIR,
                       remember_dir: str | None = None) -> int:
    """Return the JSONL line number where the last save happened.

    Reads ``last-save.json`` and returns the position recorded for this
    session. Returns 0 if the file is missing, corrupt, or has never seen
    this session.

    Positions are keyed by session ID (issue #140). The pre-#140 file held a
    single session, so it is still honoured when it happens to name this one —
    the first save after an upgrade should resume, not re-summarize the whole
    transcript.

    Args:
        session_id: Session UUID to match against the saved position.
        project_dir: Root directory of the Claude Code project.
        remember_dir: Override for the memory data directory. When None,
            falls back to the REMEMBER_DIR env var or project-relative default.

    Returns:
        Line number (0-indexed) to resume extraction from, or 0.
    """
    path = _last_save_path(project_dir, remember_dir)
    return read_positions(path).get(session_id, 0)


def count_lines(path: str) -> int:
    """Count total lines in a JSONL file.

    Args:
        path: Absolute path to the JSONL file.

    Returns:
        Number of lines in the file.
    """
    count = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for _ in f:
            count += 1
    return count


def sniff_file_envelope_status(path: str) -> tuple[str, bool]:
    """Which host wrote this transcript, plus whether it could be OPENED at
    all (#478).

    Same sniff as ``sniff_file_envelope()`` -- see that docstring -- but
    returned alongside a second fact: "unrecognised" covers two different
    causes, and this call tells them apart. ``unreadable=True`` means the
    file could not even be opened (an ``OSError`` -- a permission error, a
    bad mount, a file that vanished between listing and open); ``False``
    means the file WAS opened and read to exhaustion (or found empty) and
    simply never contained a line naming a known host shape. The envelope
    string itself is always "unrecognised" in both cases, matching
    ``sniff_file_envelope()`` exactly, so a caller that only wants the old
    behaviour is unaffected.

    Returns:
        ``(envelope, unreadable)`` where ``envelope`` is "claude-code",
        "codex", or "unrecognised".
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                return _host.sniff_envelope(obj), False
    except OSError:
        return "unrecognised", True
    return "unrecognised", False


def sniff_file_envelope(path: str) -> str:
    """Which host wrote this transcript, from its own first parseable line.

    Independent of any resume position: incremental extraction can start deep
    into a file, but the envelope is decided once, from line 0, rather than
    guessed from whatever line an old ``skip_lines`` happens to land on --
    every line in one transcript comes from the same host.

    Returns "claude-code", "codex", or "unrecognised" -- the last one also
    covering an unreadable or entirely-empty file, which offers no line to
    sniff at all. Callers that need to tell "could not even open it" apart
    from "opened it, found no known shape" -- #478 -- want
    ``sniff_file_envelope_status()`` instead; this function's contract (a
    single string, both causes collapsed) is kept unchanged for every
    existing caller.
    """
    return sniff_file_envelope_status(path)[0]


_CHANNEL_RE = re.compile(r"^<channel\b[^>]*>(.*)</channel>$", re.DOTALL)


def _channel_text(content) -> str | None:
    """Inner text of a ``<channel>…</channel>`` payload, or None.

    Channel-delivered input (e.g. the Telegram plugin) arrives as a
    ``type: "user"`` record flagged ``isMeta: true`` whose content is a
    ``<channel …>…</channel>`` transport wrapper. It is real human input, so
    it must survive the isMeta filter — this returns the inner message text
    (wrapper stripped) when content is such a payload, else None (issue #128).
    """
    if not isinstance(content, str):
        return None
    match = _CHANNEL_RE.match(content.strip())
    if not match:
        return None
    return match.group(1).strip()


def extract_messages(path: str, skip_lines: int = 0, envelope: str = "claude-code") -> list[tuple[str, str]]:
    """Parse a session JSONL file into role-labeled message tuples.

    Reads each line as JSON, skips metadata messages and system reminders,
    and extracts readable text from both string and block-list content
    formats. Tool use blocks are condensed into short summaries.

    Args:
        path: Absolute path to the session JSONL file.
        skip_lines: Number of lines to skip from the beginning (for
            incremental extraction after a previous save).
        envelope: Which host wrote this transcript -- "claude-code" (the
            default, and the only shape this function understood before
            #443), "codex", or "unrecognised". Callers determine this once
            per file via ``sniff_file_envelope()``, independent of
            ``skip_lines``, and pass it through here; a per-line resniff
            would misfire on a resume that starts past the file's only
            self-identifying line.

    Returns:
        List of ``("HUMAN", text)`` or ``("AGENT", text)`` tuples,
        one per message, in chronological order. Empty for an
        "unrecognised" envelope -- deliberately: the caller (
        ``extract_session``) is the one that reports that state loud,
        via ``ExtractResult.envelope``, rather than this function
        pretending it read something.
    """
    messages: list[tuple[str, str]] = []
    corrupt_count = 0

    if envelope == "unrecognised":
        return messages

    try:
        f = open(path, encoding="utf-8", errors="replace")
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

            if envelope == "codex":
                exchange = _host.codex_exchange(obj)
                if exchange is not None:
                    messages.append(exchange)
                continue

            msg_type = obj.get("type")
            if msg_type not in ("user", "assistant"):
                continue

            content = obj.get("message", {}).get("content", "")
            channel_text = _channel_text(content)
            if obj.get("isMeta", False) and channel_text is None:
                continue

            texts = _extract_texts(content if channel_text is None else channel_text)

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
    remember_dir: str | None = None,
) -> ExtractResult:
    """Main entry point: extract exchanges from a session.

    Args:
        session_id: Specific session to extract. None = latest.
        project_dir: Project root directory.
        count: If set, return only the last N exchanges.
        show_all: If True, extract from line 0.
        remember_dir: Override for the memory data directory (external mode).
            When None, falls back to REMEMBER_DIR env var or project default.

    Returns:
        ExtractResult with formatted exchanges and position.

    Raises:
        FileNotFoundError: If no matching session JSONL file is found.
    """
    path = find_session(session_id, project_dir)
    # The supplied session_id wins over a basename derived from the transcript
    # file. A host may name the file anything (Codex's rollout-<date>-<uuid>);
    # if that leaks into the resume key, get_last_save_line never matches it
    # and every save re-summarizes from line 0 -- the duplicate #140 exists to
    # prevent.
    actual_id = session_id or os.path.basename(path).replace(".jsonl", "")
    total_lines = count_lines(path)
    envelope, envelope_unreadable = sniff_file_envelope_status(path)

    used_skip_lines = 0
    unread_sidecar_unreadable = False
    if show_all:
        messages = extract_messages(path, skip_lines=0, envelope=envelope)
    elif count is not None:
        messages = extract_messages(path, skip_lines=0, envelope=envelope)
        messages = messages[-count:]
    else:
        last_line = get_last_save_line(actual_id, project_dir, remember_dir)
        # #450: a prior run may have advanced the saved position past a span
        # it never actually read (the envelope was "unrecognised" then), to
        # keep #147's retry loop closed. That span is quarantined rather than
        # lost -- start from the quarantine point instead of the saved
        # position whenever one is on record, so a build that can now parse
        # the envelope reads the missed span too. Harmless when this run is
        # STILL unrecognised: extract_messages returns nothing for either
        # skip point, and the quarantine simply stays in force.
        unread_sessions, unread_sidecar_unreadable = read_unread_envelope_status(
            _unread_envelope_path(project_dir, remember_dir)
        )
        unread_from = unread_sessions.get(actual_id)
        used_skip_lines = unread_from if unread_from is not None else last_line
        messages = extract_messages(path, skip_lines=used_skip_lines, envelope=envelope)

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
        envelope=envelope,
        skip_lines=used_skip_lines,
        unread_sidecar_unreadable=unread_sidecar_unreadable,
        envelope_unreadable=envelope_unreadable,
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
            "envelope": result.envelope,
        }))
    else:
        print(result.exchanges)
        print(f"\n__POSITION__:{result.position}")


if __name__ == "__main__":
    main()
