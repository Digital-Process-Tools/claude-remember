"""Which agent CLI is hosting this plugin, and what it tells us (#407).

Remember was written against one host and reads that host's environment
directly. Three now exist, and they agree on far less than they appear to:

    | | Claude Code | Codex | Gemini CLI |
    |---|---|---|---|
    | hook stdin `session_id`, `cwd`, `transcript_path` | yes | yes | yes |
    | tool event names | `PreToolUse`/`PostToolUse` | same | `BeforeTool`/`AfterTool` |
    | plugin-root env var | `CLAUDE_PLUGIN_ROOT` | `PLUGIN_ROOT` (+ `CLAUDE_*` alias) | none documented |

The stdin payload is the only part all three arrived at independently. The
environment is the parochial part: Codex's `CLAUDE_PLUGIN_ROOT` is a
compatibility alias it chose to extend and can withdraw, and Gemini documents no
such variable at all.

So this module is deliberately thin, and is not a host abstraction layer. It
holds the two things that genuinely differ — what a host calls its variables,
and how to recognise it — as *data*, so a fourth host is a table entry rather
than a search through the tree. Everything the hosts agree on stays where it is.

Three things are explicitly NOT here, because putting them here would be
inventing a seam rather than recording one:

- **Event names.** Bindings live in each host's own manifest, which is the file
  that has to name them anyway. A mapping table here would be read by nobody.
- **The summarizer.** ``pipeline/haiku.py`` shells ``claude -p``. That is the
  one genuinely host-coupled component, and an interface derived from a single
  implementation would encode that CLI's auth model and output shape as though
  they were neutral. It gets extracted from two real providers or not at all.
- **Path resolution in shell.** ``scripts/resolve-paths.sh`` runs before Python
  is worth starting and mirrors ``PLUGIN_ROOT_VARS`` by hand, the same way
  ``lib-slug.sh`` mirrors ``pipeline/slug.py``. ``test_host_shell_parity``
  fails if the two drift.

The payload fields themselves are read by the hook that owns stdin and passed on
through the environment (the channel #266 settled on); this module never reads
stdin, because it is imported by callers that have none and sourced into hooks
that have already consumed theirs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

# Our own channel, written by whichever hook consumed the payload. Host-neutral
# on purpose: no host publishes the transcript path in the environment, so there
# is no native name to prefer over it.
TRANSCRIPT_PATH_VAR = "REMEMBER_TRANSCRIPT_PATH"
CWD_VAR = "REMEMBER_HOOK_CWD"


@dataclass(frozen=True)
class Host:
    """One agent CLI, described by the names it uses and the mark it leaves.

    ``plugin_root_vars`` and ``project_dir_vars`` are in precedence order: the
    host's own name first, any compatibility alias after it. Preferring the
    native name means nothing depends on an alias outliving the release that
    shipped it.
    """

    name: str
    plugin_root_vars: tuple[str, ...] = ()
    project_dir_vars: tuple[str, ...] = ()
    # Presence of any one of these identifies the host. Ordered most- to
    # least-specific within a host; the registry is ordered across hosts.
    signature_vars: tuple[str, ...] = field(default=())

    def plugin_root(self, env: Mapping[str, str]) -> str | None:
        return _first_set(env, self.plugin_root_vars)

    def project_dir(self, env: Mapping[str, str]) -> str | None:
        return _first_set(env, self.project_dir_vars)


CLAUDE_CODE = Host(
    name="claude-code",
    plugin_root_vars=("CLAUDE_PLUGIN_ROOT",),
    project_dir_vars=("CLAUDE_PROJECT_DIR",),
    # CLAUDE_CODE_* is set by Claude Code and by nothing else. CLAUDE_PLUGIN_ROOT
    # is NOT a signature: Codex sets it too, as an alias.
    signature_vars=("CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID"),
)

CODEX = Host(
    name="codex",
    plugin_root_vars=("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"),
    project_dir_vars=("CLAUDE_PROJECT_DIR",),
    signature_vars=("CODEX_HOME", "PLUGIN_ROOT"),
)

# Gemini CLI documents no environment variables for command hooks at all, so it
# has no signature to match and no variable to read. It is here because it is
# real and because its absence of variables is the point: everything Remember
# needs from it arrives on stdin. It is never the result of detection — it is
# what UNKNOWN already behaves like.
GEMINI = Host(name="gemini-cli")

# The fallback. Not an error: a host we do not recognise still delivers the
# payload, and the payload is the part that matters.
UNKNOWN = Host(name="unknown", plugin_root_vars=(), project_dir_vars=())

# Ordered: the most specific signature is tested first. Codex sets an alias
# Claude Code also sets, so Claude Code must be asked before Codex or an alias
# would decide the answer.
REGISTRY: tuple[Host, ...] = (CLAUDE_CODE, CODEX)

# Every plugin-root variable any known host uses, in registry precedence order,
# de-duplicated. scripts/resolve-paths.sh mirrors this list by hand and
# test_host_shell_parity asserts the two agree.
PLUGIN_ROOT_VARS: tuple[str, ...] = tuple(
    dict.fromkeys(var for host in REGISTRY for var in host.plugin_root_vars)
)


def _first_set(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env.get(name, "")
        if value.strip():
            return value
    return None


def detect_host(env: Mapping[str, str] | None = None) -> Host:
    """Identify the hosting CLI from its environment.

    Returns ``UNKNOWN`` rather than guessing or raising. A host nobody has
    described yet is a normal state here, not a failure: the stdin payload is
    what the pipeline actually needs, and it arrives regardless.
    """
    env = os.environ if env is None else env
    for host in REGISTRY:
        if _first_set(env, host.signature_vars) is not None:
            return host
    return UNKNOWN


def plugin_root(env: Mapping[str, str] | None = None) -> str | None:
    """The plugin install directory, under whichever name this host uses."""
    env = os.environ if env is None else env
    return _first_set(env, PLUGIN_ROOT_VARS)


def transcript_path(env: Mapping[str, str] | None = None) -> str | None:
    """The transcript path the host handed us, if it is usable.

    Returns ``None`` for anything the caller could not open — unset, blank, a
    directory, a path that is not there. The caller then falls back to
    reconstructing it, which is what every caller did before this existed.

    Validated here rather than at the call site because the value is copied
    from a payload written by the host: it is data, and the one thing worse
    than reconstructing a path is trusting an unusable one and reporting the
    resulting emptiness as a session with nothing in it.
    """
    env = os.environ if env is None else env
    value = (env.get(TRANSCRIPT_PATH_VAR) or "").strip()
    if not value:
        return None
    if not os.path.isfile(value):
        return None
    return value
