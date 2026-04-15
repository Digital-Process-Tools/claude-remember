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
        .replace("{{TIME}}", time)
        .replace("{{BRANCH}}", branch)
        .replace("{{LAST_ENTRY}}", last_entry)
        .replace("{{EXTRACT}}", extract)
    )


def build_ndc_prompt(now_content: str) -> str:
    """Build the NDC (Now-Document Compression) prompt.

    Args:
        now_content: Full contents of now.md to be compressed.

    Returns:
        Complete prompt string ready to send to Haiku.
    """
    template = _read_template("compress-ndc.prompt.txt")
    return template.replace("{{NOW_CONTENT}}", now_content)


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
        staging_section += f"\n--- {filename} ---\n{content}\n"

    return (
        template
        .replace("{{STAGING_FILES}}", staging_section)
        .replace("{{RECENT}}", recent)
        .replace("{{ARCHIVE}}", archive)
    )


