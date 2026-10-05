from __future__ import annotations
import json
import os
import re
import sys
from .extract import (
    _is_line_number,
    _validate_session_id,
    clear_unread_envelope,
    extract_session,
    mark_unread_envelope,
    read_positions,
)
from .haiku import _parse_response
from .prompts import build_save_prompt, build_ndc_prompt
def _shell_escape(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("shell-bridged values must not contain newlines")
    return value
def cmd_extract(session_id: str, project_dir: str) -> None:
    import tempfile
    remember_dir = os.environ.get("REMEMBER_DIR") or None
    r = extract_session(session_id=session_id, project_dir=project_dir, remember_dir=remember_dir)
    fd, extract_file = tempfile.mkstemp(prefix="remember-extract-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as f:
        f.write(r.exchanges)
    print(f"POSITION={r.position}")
    print(f"HUMAN_COUNT={r.human_count}")
    print(f"ASSISTANT_COUNT={r.assistant_count}")
    print(f"EXCHANGE_COUNT={r.human_count + r.assistant_count}")
    print(f"EXTRACT_FILE={_shell_escape(extract_file)}")
    print(f"ENVELOPE={_shell_escape(r.envelope)}")
    print(f"SKIP_LINES={r.skip_lines}")
    print(f"UNREAD_SIDECAR_UNREADABLE={1 if r.unread_sidecar_unreadable else 0}")
    print(f"ENVELOPE_UNREADABLE={1 if r.envelope_unreadable else 0}")
    print(f"ENVELOPE_CAPPED={1 if r.envelope_capped else 0}")
    print(f"ENVELOPE_HAS_UNMAPPED_STEP={1 if r.envelope_has_unmapped_step else 0}")
def cmd_build_prompt(
    extract_file: str,
    last_entry_file: str,
    time: str,
    branch: str,
    output_file: str,
    max_extract_bytes: int = 0,
) -> None:
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
    with open(memory_file, encoding="utf-8", errors="replace") as f:
        content = f.read()
    prompt = build_ndc_prompt(content)
    with open(output_file, "w", encoding="utf-8", errors="replace") as f:
        f.write(prompt)
def cmd_parse_haiku(output_file: str = "") -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    raw = sys.stdin.read()
    _emit_haiku_result(_parse_response(raw), output_file)
def _emit_haiku_result(r, output_file: str = "") -> None:
    import tempfile
    fd, text_file = tempfile.mkstemp(prefix="remember-haiku-text-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as f:
        f.write(r.text)
    print(f"HAIKU_TEXT_FILE={_shell_escape(text_file)}")
    print(f"IS_SKIP={'true' if r.is_skip else 'false'}")
    print(f"IS_REJECTED={'true' if r.is_rejected else 'false'}")
    print(f"PROVIDER={_shell_escape(r.provider)}")
    print(f"TK_IN={r.tokens.input}")
    print(f"TK_OUT={r.tokens.output}")
    print(f"TK_CACHE={r.tokens.cache}")
    print(f"TK_COST={r.tokens.cost_usd:.6f}")
    if output_file:
        with open(output_file, "w", encoding="utf-8", errors="replace") as f:
            f.write(r.text)
def cmd_call_haiku(prompt_file: str, output_file: str = "", timeout: int = 120) -> None:
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
_POSITION_SLOTS = 32
def cmd_save_position(
    last_save_file: str,
    session_id: str,
    position: int,
    envelope: str | None = None,
    skip_lines: int | None = None,
) -> None:
    _validate_session_id(session_id)
    sessions = read_positions(last_save_file)
    sessions.pop(session_id, None)
    sessions[session_id] = position
    evicted: list[str] = []
    while len(sessions) > _POSITION_SLOTS:
        evicted.append(next(iter(sessions)))
        del sessions[evicted[-1]]
    payload = {"sessions": sessions, "session": session_id, "line": position}
    tmp = f"{last_save_file}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, last_save_file)
    sidecar_dir = os.path.dirname(last_save_file)
    sidecar = os.path.join(sidecar_dir, f"position.{session_id}")
    sidecar_tmp = f"{sidecar}.tmp"
    with open(sidecar_tmp, "w", encoding="utf-8") as f:
        f.write(str(position))
    os.replace(sidecar_tmp, sidecar)
    for evicted_id in evicted:
        try:
            _validate_session_id(evicted_id)
            os.remove(os.path.join(sidecar_dir, f"position.{evicted_id}"))
        except (OSError, ValueError):
            pass
    unread_path = os.path.join(sidecar_dir, "unread-envelope.json")
    if envelope == "unrecognised":
        mark_unread_envelope(unread_path, session_id,
                              skip_lines if skip_lines is not None else position)
    elif envelope is not None:
        clear_unread_envelope(unread_path, session_id)
    for evicted_id in evicted:
        clear_unread_envelope(unread_path, evicted_id)
def cmd_read_position(last_save_file: str, session_id: str) -> None:
    print(read_positions(last_save_file).get(session_id, 0))
def _rotate_to_dated_sibling(path: str, stem: str) -> str | None:
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    from ._tz import today_str
    parent = os.path.dirname(path)
    base = f"{stem}-{today_str()}"
    target = os.path.join(parent, f"{base}.md")
    n = 2
    while os.path.exists(target):
        target = os.path.join(parent, f"{base}-{n}.md")
        n += 1
    os.rename(path, target)
    return target
def _rotate_archive(archive_file: str) -> str | None:
    return _rotate_to_dated_sibling(archive_file, "archive")
def _rotate_recent(recent_file: str) -> str | None:
    return _rotate_to_dated_sibling(recent_file, "recent")
def _eligible_staging(directory: str, filter_today: bool = True) -> list[str]:
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
                    max_prompt_bytes: int = 0, snapshot_dir: str = "",
                    timeout: int = 180) -> None:
    import tempfile
    from .consolidate import consolidate, ConsolidationSkipped, ConsolidationTooLarge
    from .spawn_guard import EXIT_SPAWN_DECLINED, SummarizerSpawnDeclined
    source_dir = snapshot_dir or staging_dir
    staging_contents: dict[str, str] = {}
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
        print(f"STAGING_COUNT={len(staging_contents)}")
        print("CONSOLIDATION_STATUS=skip")
    rotated: str | None = None
    rotated_recent: str | None = None
    def _restore_rotation() -> None:
        if rotated is not None and os.path.exists(rotated):
            os.replace(rotated, archive_file)
        if rotated_recent is not None and os.path.exists(rotated_recent):
            os.replace(rotated_recent, recent_file)
    recent_size = os.path.getsize(recent_file) if os.path.exists(recent_file) else 0
    archive_size = os.path.getsize(archive_file) if os.path.exists(archive_file) else 0
    if max_prompt_bytes > 0:
        staging_size = sum(staging_raw_bytes.values())
        embedded = staging_size + recent_size + archive_size
        if embedded > max_prompt_bytes:
            if embedded - archive_size <= max_prompt_bytes:
                rotated = _rotate_archive(archive_file)
                if rotated is None:
                    _emit_skip()
                    return
                archive_size = 0
            elif staging_size <= max_prompt_bytes:
                rotated_recent = _rotate_recent(recent_file)
                if rotated_recent is None:
                    _emit_skip()
                    return
                recent_size = 0
                if staging_size + archive_size > max_prompt_bytes:
                    rotated = _rotate_archive(archive_file)
                    if rotated is None:
                        _restore_rotation()
                        _emit_skip()
                        return
                    archive_size = 0
            else:
                _emit_skip()
                return
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
                             max_prompt_bytes=max_prompt_bytes, timeout=timeout)
    except ConsolidationTooLarge:
        if rotated_recent is not None:
            _restore_rotation()
            _emit_skip()
            return
        if rotated is None:
            rotated = _rotate_archive(archive_file)
        if rotated is None:
            _restore_rotation()
            _emit_skip()
            return
        try:
            result = consolidate(staging_contents, recent, "",
                                 max_prompt_bytes=max_prompt_bytes, timeout=timeout)
        except ConsolidationSkipped:
            _restore_rotation()
            _emit_skip()
            return
        except Exception:
            _restore_rotation()
            raise
    except SummarizerSpawnDeclined as declined:
        _restore_rotation()
        print(f"consolidate declined: {declined}", file=sys.stderr)
        sys.exit(EXIT_SPAWN_DECLINED)
    except ConsolidationSkipped:
        _restore_rotation()
        _emit_skip()
        return
    except Exception:
        _restore_rotation()
        raise
    fd_r, recent_out = tempfile.mkstemp(prefix="remember-recent-", suffix=".md")
    with os.fdopen(fd_r, "w", encoding="utf-8", errors="replace") as f:
        f.write(result.recent)
    fd_a, archive_out = tempfile.mkstemp(prefix="remember-archive-", suffix=".md")
    with os.fdopen(fd_a, "w", encoding="utf-8", errors="replace") as f:
        f.write(result.archive)
    fd_s, staging_paths_file = tempfile.mkstemp(prefix="remember-staging-paths-", suffix=".bin")
    with os.fdopen(fd_s, "wb") as f:
        for name in staging_contents:
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
            envelope=sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] != "" else None,
            skip_lines=int(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] != "" else None,
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
            timeout=int(sys.argv[7]) if len(sys.argv) > 7 else 180,
        )
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
if __name__ == "__main__":
    main()
