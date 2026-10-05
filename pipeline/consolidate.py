from __future__ import annotations
import re
from .prompts import build_consolidation_prompt, consolidation_template
from .haiku import call_haiku
from .entry_header import ANY_HEADER_ERE
from .types import ConsolidationResult, TokenUsage
_ENTRY_HEADER = re.compile(ANY_HEADER_ERE, re.MULTILINE)
_FENCE_OPENER = re.compile(r"^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+.-]*)\s*$")
_WRAP_TAGS = frozenset({"", "markdown", "md", "text", "txt", "plaintext"})
def _looks_like_a_section(body: str) -> bool:
    return body.startswith(("# Recent", "# Archive", "===RECENT===", "===ARCHIVE==="))
def _strip_wrapping_fence(text: str) -> str:
    t = text.strip()
    lines = t.split("\n")
    opener = _FENCE_OPENER.match(lines[0])
    if not opener:
        return t
    marker, info = opener.group(1), opener.group(2).lower()
    documentish = info in _WRAP_TAGS
    closer = re.compile(r"^\s*" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*$")
    inner = None
    for i in range(1, len(lines)):
        fence = _FENCE_OPENER.match(lines[i])
        if not fence:
            continue
        mark, tag = fence.group(1), fence.group(2)
        if i == len(lines) - 1 and closer.match(lines[i]) and not (inner and inner[1]):
            wrapped = "\n".join(lines[1:i]).strip()
            if documentish or _looks_like_a_section(wrapped):
                return wrapped
            return t
        if inner is not None:
            if not tag and mark[0] == inner[0][0] and len(mark) >= len(inner[0]):
                inner = None
            continue
        inner = (mark, tag)
    body = "\n".join(lines[1:]).strip()
    if inner is not None and not inner[1] and closer.match(inner[0]):
        if not _looks_like_a_section(body):
            return t
    elif not documentish and not _looks_like_a_section(body):
        return t
    return body
class ConsolidationSkipped(Exception):
    pass
class ConsolidationTooLarge(ConsolidationSkipped):
    pass
_MIN_MARKER_LEN = 24
def _instruction_markers() -> tuple[str, ...]:
    markers = []
    for line in consolidation_template().splitlines():
        line = line.strip()
        if not line or "{{" in line or len(line) < _MIN_MARKER_LEN:
            continue
        markers.append(line)
    return tuple(markers)
def _echoes_the_prompt(text: str) -> bool:
    return any(marker in text for marker in _instruction_markers())
def _is_valid_consolidation(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if _echoes_the_prompt(t):
        return False
    if "===RECENT===" in t:
        return True
    return _ENTRY_HEADER.search(t) is not None
def consolidate(
    staging_contents: dict[str, str],
    recent: str,
    archive: str,
    max_prompt_bytes: int = 0,
    timeout: int = 180,
) -> ConsolidationResult:
    prompt = build_consolidation_prompt(staging_contents, recent, archive)
    if max_prompt_bytes > 0:
        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > max_prompt_bytes:
            raise ConsolidationTooLarge(
                f"consolidation prompt too large ({prompt_bytes} bytes > "
                f"{max_prompt_bytes} cap) -- skipping to avoid a context-window "
                f"overflow; staging + memory left untouched"
            )
    result = call_haiku(prompt, timeout=timeout)
    if result.is_skip or not _is_valid_consolidation(result.text):
        raise ConsolidationSkipped(
            "Haiku returned no usable consolidation "
            "(SKIP or missing ===RECENT===/entry headers)"
        )
    if max_prompt_bytes > 0:
        response_bytes = len(result.text.encode("utf-8"))
        if response_bytes > max_prompt_bytes:
            raise ConsolidationSkipped(
                f"consolidation response too large ({response_bytes} bytes > "
                f"{max_prompt_bytes} cap) -- refusing to write it to memory; "
                f"staging + memory left untouched"
            )
    recent_new, archive_new = parse_consolidation_response(result.text)
    return ConsolidationResult(
        recent=recent_new,
        archive=archive_new,
        tokens=result.tokens,
    )
def parse_consolidation_response(text: str) -> tuple[str, str]:
    recent = ""
    archive = ""
    text = _strip_wrapping_fence(text)
    if "===RECENT===" in text and "===ARCHIVE===" in text:
        parts = text.split("===ARCHIVE===", 1)
        recent = _strip_wrapping_fence(parts[0].replace("===RECENT===", ""))
        archive = _strip_wrapping_fence(parts[1])
    elif "===RECENT===" in text:
        recent = _strip_wrapping_fence(text.replace("===RECENT===", ""))
    else:
        recent = _strip_wrapping_fence(text)
    if recent and not recent.startswith("# Recent"):
        recent = "# Recent\n\n" + recent
    if archive and not archive.startswith("# Archive"):
        archive = "# Archive\n\n" + archive
    return recent, archive
