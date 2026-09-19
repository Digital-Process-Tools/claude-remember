"""Template loading and variable substitution for pipeline prompts.

Each pipeline stage (save, consolidate, NDC) has a corresponding text
template in the ``prompts/`` directory. This module loads those templates
and substitutes ``{{PLACEHOLDER}}`` variables with runtime values.

Templates are plain text files with mustache-style placeholders::

    prompts/
        save-session.prompt.txt          # {{TIME}}, {{BRANCH}}, {{LAST_ENTRY}}, {{EXTRACT}}
        compress-ndc.prompt.txt          # {{NOW_CONTENT}}
        consolidate-staging.prompt.txt   # {{STAGING_FILES}}, {{RECENT}}, {{ARCHIVE}}
"""

from __future__ import annotations

import os


PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prompts")


def _read_template(name: str) -> str:
    """Read a prompt template file from the prompts/ directory.

    Args:
        name: Filename of the template (e.g., "save-session.prompt.txt").

    Returns:
        Raw template string with ``{{PLACEHOLDER}}`` markers intact.
    """
    path = os.path.join(PROMPTS_DIR, name)
    with open(path, encoding="utf-8") as f:
        return f.read()


_PLACEHOLDER_BREAK = "\u200b"  # zero-width space


def _escape_placeholder_syntax(value: str) -> str:
    """Break up any '{{' / '}}' pairs in an untrusted substituted value.

    A value inserted into a prompt template can itself contain the literal
    two-character sequence '{{' or '}}' -- transcript text quoting the
    template's own placeholder syntax, for instance. Left unescaped, that
    sequence can spell out one of the four real placeholder tokens
    ('{{TIME}}', '{{BRANCH}}', '{{LAST_ENTRY}}', '{{EXTRACT}}') in the final
    prompt, which is indistinguishable from a genuine unsubstituted
    placeholder to a downstream guard that greps the assembled prompt for
    those exact strings (scripts/save-session.sh:700). Since that guard
    aborts the save without advancing the read cursor, an attacker who gets
    one of these tokens into a transcript can permanently stall memory
    capture for the session (#722).

    Inserting a zero-width space between the two braces keeps the value
    visually unchanged (a human or Haiku reading the prompt still sees
    what looks like "{{TIME}}") while breaking the exact byte match the
    guard -- and any other single-pass '{{TOKEN}}' scan -- depends on. This
    is applied only to substituted VALUES, never to template text itself,
    so a genuine unsubstituted placeholder in a template is still fully
    detectable.
    """
    return (
        value.replace("{{", "{" + _PLACEHOLDER_BREAK + "{")
        .replace("}}", "}" + _PLACEHOLDER_BREAK + "}")
    )


CONSOLIDATION_TEMPLATE = "consolidate-staging.prompt.txt"


def consolidation_template() -> str:
    """The raw consolidation template, placeholders unsubstituted.

    Read by ``consolidate._instruction_markers`` to work out which lines are
    *instructions* rather than content, so an echo of the prompt can be told
    apart from a real consolidation. Derived rather than hardcoded, so the two
    cannot drift — a second copy of the template's wording would be the same
    duplicated-rule class as the slug (#144) and the entry header (#177).
    """
    return _read_template(CONSOLIDATION_TEMPLATE)


def build_save_prompt(
    time: str,
    branch: str,
    last_entry: str,
    extract: str,
) -> str:
    """Build the save-summary prompt with session context substituted.

    Args:
        time: Current timestamp string (e.g., "14:32").
        branch: Current git branch name.
        last_entry: The most recent entry from today's staging file,
            used to help Haiku avoid repeating itself.
        extract: Formatted session exchanges from the extractor.

    Returns:
        Complete prompt string ready to send to Haiku.
    """
    template = _read_template("save-session.prompt.txt")
    return (
        template
        .replace("{{TIME}}", _escape_placeholder_syntax(time))
        .replace("{{BRANCH}}", _escape_placeholder_syntax(branch))
        .replace("{{LAST_ENTRY}}", _escape_placeholder_syntax(last_entry))
        .replace("{{EXTRACT}}", _escape_placeholder_syntax(extract))
    )


def build_ndc_prompt(now_content: str) -> str:
    """Build the NDC (Now-Document Compression) prompt.

    Args:
        now_content: Full contents of now.md to be compressed.

    Returns:
        Complete prompt string ready to send to Haiku.
    """
    template = _read_template("compress-ndc.prompt.txt")
    return template.replace("{{NOW_CONTENT}}", _escape_placeholder_syntax(now_content))


def build_consolidation_prompt(
    staging_contents: dict[str, str],
    recent: str,
    archive: str,
) -> str:
    """Build the consolidation prompt with all file contents inlined.

    Assembles staging file contents into a labeled section and substitutes
    all placeholders in the consolidation template.

    Args:
        staging_contents: Mapping of ``{filename: content}`` for each
            staging file to consolidate.
        recent: Current content of recent.md (may be empty on first run).
        archive: Current content of archive.md (may be empty on first run).

    Returns:
        Complete prompt string ready to send to Haiku.
    """
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


