from __future__ import annotations
import os
PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prompts")
def _read_template(name: str) -> str:
    path = os.path.join(PROMPTS_DIR, name)
    with open(path, encoding="utf-8") as f:
        return f.read()
_PLACEHOLDER_BREAK = "\u200b"
def _escape_placeholder_syntax(value: str) -> str:
    return (
        value.replace("{{", "{" + _PLACEHOLDER_BREAK + "{")
        .replace("}}", "}" + _PLACEHOLDER_BREAK + "}")
    )
CONSOLIDATION_TEMPLATE = "consolidate-staging.prompt.txt"
def consolidation_template() -> str:
    return _read_template(CONSOLIDATION_TEMPLATE)
def build_save_prompt(
    time: str,
    branch: str,
    last_entry: str,
    extract: str,
) -> str:
    template = _read_template("save-session.prompt.txt")
    return (
        template
        .replace("{{TIME}}", _escape_placeholder_syntax(time))
        .replace("{{BRANCH}}", _escape_placeholder_syntax(branch))
        .replace("{{LAST_ENTRY}}", _escape_placeholder_syntax(last_entry))
        .replace("{{EXTRACT}}", _escape_placeholder_syntax(extract))
    )
def build_ndc_prompt(now_content: str) -> str:
    template = _read_template("compress-ndc.prompt.txt")
    return template.replace("{{NOW_CONTENT}}", _escape_placeholder_syntax(now_content))
def build_consolidation_prompt(
    staging_contents: dict[str, str],
    recent: str,
    archive: str,
) -> str:
    template = _read_template("consolidate-staging.prompt.txt")
    staging_section = ""
    for filename, content in sorted(staging_contents.items()):
        staging_section += (
            f"\n--- {_escape_placeholder_syntax(filename)} ---\n"
            f"{_escape_placeholder_syntax(content)}\n"
        )
    return (
        template
        .replace("{{STAGING_FILES}}", staging_section)
        .replace("{{RECENT}}", _escape_placeholder_syntax(recent))
        .replace("{{ARCHIVE}}", _escape_placeholder_syntax(archive))
    )
