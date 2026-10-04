from __future__ import annotations
import json
import glob
import os
import re
import sys
from . import host as _host
from .slug import session_dir_slug
from .types import ExtractResult
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(_SCRIPT_DIR)))
def _session_dir(project_dir: str) -> str:
    slug = session_dir_slug(project_dir)
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        home = os.environ.get("HOME") or os.path.expanduser("~")
        config_dir = home + "/.claude"
    return config_dir.rstrip("/\\") + "/projects/" + slug
def _is_line_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()
def read_positions(last_save_file: str) -> dict[str, int]:
    try:
        with open(last_save_file, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    sessions = data.get("sessions")
    if isinstance(sessions, dict):
        return {k: int(v) for k, v in sessions.items() if _is_line_number(v)}
    if isinstance(data.get("session"), str) and _is_line_number(data.get("line")):
        return {data["session"]: int(data["line"])}
    return {}
def _last_save_path(project_dir: str, remember_dir: str | None = None) -> str:
    effective = remember_dir or os.environ.get("REMEMBER_DIR") or (project_dir.rstrip("/\\") + "/.remember")
    return effective.rstrip("/\\") + "/tmp/last-save.json"
def _unread_envelope_path(project_dir: str, remember_dir: str | None = None) -> str:
    effective = remember_dir or os.environ.get("REMEMBER_DIR") or (project_dir.rstrip("/\\") + "/.remember")
    return effective.rstrip("/\\") + "/tmp/unread-envelope.json"
def read_unread_envelope_status(path: str) -> tuple[dict[str, int], bool]:
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
    return read_unread_envelope_status(path)[0]
def _write_unread_envelope(path: str, sessions: dict[str, int]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sessions, f)
    os.replace(tmp, path)
def mark_unread_envelope(path: str, session_id: str, from_line: int) -> None:
    sessions = read_unread_envelope(path)
    if session_id in sessions:
        return
    sessions[session_id] = from_line
    _write_unread_envelope(path, sessions)
def clear_unread_envelope(path: str, session_id: str) -> None:
    sessions = read_unread_envelope(path)
    if session_id not in sessions:
        return
    del sessions[session_id]
    _write_unread_envelope(path, sessions)
def _validate_session_id(session_id: str) -> None:
    if (
        "/" in session_id
        or "\\" in session_id
        or ".." in session_id
        or ":" in session_id
    ):
        raise ValueError(f"invalid session_id: {session_id}")
def find_session(session_id: str | None = None,
                 project_dir: str = _DEFAULT_PROJECT_DIR) -> str:
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
    newest, newest_mtime = files[0], os.path.getmtime(files[0])
    for candidate in files[1:]:
        mtime = os.path.getmtime(candidate)
        if mtime > newest_mtime:
            newest, newest_mtime = candidate, mtime
    return newest
def get_last_save_line(session_id: str,
                       project_dir: str = _DEFAULT_PROJECT_DIR,
                       remember_dir: str | None = None) -> int:
    path = _last_save_path(project_dir, remember_dir)
    return read_positions(path).get(session_id, 0)
def count_lines(path: str) -> int:
    count = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for _ in f:
            count += 1
    return count
_ENVELOPE_SNIFF_SCAN_CAP = 50
def sniff_file_envelope_status(path: str) -> tuple[str, bool, bool]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            scanned = 0
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                envelope = _host.sniff_envelope(obj)
                if envelope != "unrecognised":
                    return envelope, False, False
                scanned += 1
                if scanned >= _ENVELOPE_SNIFF_SCAN_CAP:
                    return "unrecognised", False, next(f, None) is not None
    except OSError:
        return "unrecognised", True, False
    return "unrecognised", False, False
def sniff_file_envelope(path: str) -> str:
    return sniff_file_envelope_status(path)[0]
_CHANNEL_RE = re.compile(r"^<channel\b[^>]*>(.*)</channel>$", re.DOTALL)
def _channel_text(content) -> str | None:
    if not isinstance(content, str):
        return None
    match = _CHANNEL_RE.match(content.strip())
    if not match:
        return None
    return match.group(1).strip()
def extract_messages(
    path: str,
    skip_lines: int = 0,
    envelope: str = "claude-code",
    stats: dict | None = None,
) -> list[tuple[str, str]]:
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
            if envelope == "antigravity":
                if stats is not None and _host.antigravity_step_is_unmapped(obj):
                    stats["antigravity_unmapped_steps"] = (
                        stats.get("antigravity_unmapped_steps", 0) + 1
                    )
                exchange = _host.antigravity_exchange(obj)
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
    path = find_session(session_id, project_dir)
    actual_id = session_id or os.path.basename(path).replace(".jsonl", "")
    total_lines = count_lines(path)
    envelope, envelope_unreadable, envelope_capped = sniff_file_envelope_status(path)
    used_skip_lines = 0
    unread_sidecar_unreadable = False
    antigravity_stats: dict = {}
    if show_all:
        messages = extract_messages(path, skip_lines=0, envelope=envelope, stats=antigravity_stats)
    elif count is not None:
        messages = extract_messages(path, skip_lines=0, envelope=envelope, stats=antigravity_stats)
        messages = messages[-count:]
    else:
        last_line = get_last_save_line(actual_id, project_dir, remember_dir)
        unread_sessions, unread_sidecar_unreadable = read_unread_envelope_status(
            _unread_envelope_path(project_dir, remember_dir)
        )
        unread_from = unread_sessions.get(actual_id)
        used_skip_lines = unread_from if unread_from is not None else last_line
        messages = extract_messages(
            path, skip_lines=used_skip_lines, envelope=envelope, stats=antigravity_stats
        )
    envelope_has_unmapped_step = antigravity_stats.get("antigravity_unmapped_steps", 0) > 0
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
        envelope_capped=envelope_capped,
        envelope_has_unmapped_step=envelope_has_unmapped_step,
    )
def main() -> None:
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
            pass
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
