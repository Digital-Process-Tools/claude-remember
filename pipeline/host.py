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
- **The summarizer.** ``pipeline/haiku.py`` shells ``claude -p`` or
  ``codex exec``, chosen by ``pipeline.haiku._choose_summarizer_provider()``
  -- "auto" reads the TRANSCRIPT the host wrote (``transcript_path()`` below
  plus ``pipeline.extract.sniff_file_envelope()``), not ``detect_host()``
  (#465; ``detect_host()`` was the original #460 mechanism, but the env-var
  signature it reads does not survive into the hook process that actually
  runs the summarizer). Two real providers now exist, which was this
  docstring's own bar for extracting an abstraction. A shared interface is
  still not here: each provider's auth model and output shape (Anthropic's
  ``--output-format json`` vs. Codex's ``-o <file>``) stay different enough
  that one would either leak one CLI's shape into the other or hide a
  distinction ``pipeline/haiku.py`` actually needs, so the provider dispatch
  lives beside the CLI calls it dispatches to, and only WHICH host is running
  lives here.
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
    # NOT CODEX_HOME (#463): that is a configuration path Codex *reads*,
    # never a variable it *exports* to a child process, so it is absent from
    # every real codex exec environment and could never have fired. NOT
    # PLUGIN_ROOT either, for the same reason: it is a compatibility alias
    # covered by plugin_root_vars above, not a signature -- and, unlike
    # CODEX_HOME, it is not even always set (Codex only exports it when
    # invoking a plugin's own hook, not for a bare `codex exec`).
    # CODEX_SESSION_ID/CODEX_THREAD_ID are what a live `codex exec` process
    # actually exports on every run, verified against codex-cli 0.150.1 --
    # see tests/fixtures/codex-env-463.txt, captured rather than
    # constructed, since a constructed fixture would have accepted
    # CODEX_HOME just as happily as the code it was meant to catch.
    signature_vars=("CODEX_SESSION_ID", "CODEX_THREAD_ID"),
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
#
# This order also decides what happens when BOTH hosts' native (non-alias)
# signatures are present at once -- a real configuration, not a hypothetical
# one: a Codex session launched from inside a Claude Code session inherits
# CLAUDE_CODE_ENTRYPOINT/CLAUDE_CODE_SESSION_ID from its parent, alongside
# its own freshly-set CODEX_SESSION_ID/CODEX_THREAD_ID (#463). CLAUDE_CODE
# wins in that case, deliberately, and not because it is "more correct" --
# there is no way to tell from flat environment variables alone which
# process is the ancestor and which is the child. Neither host publishes an
# ancestry marker, so "prefer the innermost host" cannot be implemented
# honestly; it would have to guess a direction and would guess wrong for the
# (rarer, but real) reverse nesting of Claude Code launched from inside a
# Codex sandbox.
#
# A third, explicit AMBIGUOUS state was considered instead of silently
# picking one. It was rejected here because, at the time, detect_host() had
# exactly one consumer (pipeline.haiku._choose_summarizer_provider) and that
# consumer's own contract was already binary -- "codex under a detected
# Codex host, claude everywhere else" -- so AMBIGUOUS would have collapsed
# into the same "claude" branch as UNKNOWN with no behavioural difference
# from today's registry-order answer; it would have been a label nothing
# reads, not a decision nothing else could reach.
#
# #465: that consumer no longer calls detect_host() at all -- the env-var
# signature this function reads does not survive into the process that
# actually runs the summarizer (see pipeline/haiku.py's own note on
# _choose_summarizer_provider), so summarizer routing now reads the
# transcript the host wrote instead. detect_host() stays here as a correct,
# directly-tested fact about a process's environment
# (tests/test_codex_signature_463.py) and the tie-break comment above still
# describes real, still-true behaviour of THIS function; it is simply no
# longer wired to the one decision it used to gate. If a consumer that needs
# env-based host identification is ever added back, this is the point to
# revisit AMBIGUOUS, not before.
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


# ─── Transcript line envelopes (#443) ──────────────────────────────────────
#
# A hook hands ``pipeline.extract`` a transcript *path*; this is the other
# half -- what a LINE of that transcript looks like, which is what decides
# whether ``extract_messages()`` can read it at all. Claude Code and Codex
# disagree here in the way the module docstring above warns about: this is
# genuinely host-specific data, so it belongs beside ``Host`` rather than
# branched inside ``extract.py``.
#
# Claude Code: ``{"type": "user"|"assistant"|..., "message": {"content": ...}}``.
# Codex: every line, whatever its own ``type`` says, is
# ``{"timestamp", "ordinal", "type", "payload"}`` -- the role and text live
# one level down, inside ``payload`` (issue #443).


def sniff_envelope(obj: object) -> str:
    """Identify which host wrote one already-parsed transcript line, by shape.

    Called once per file, against its own first parseable line -- never
    against whatever line an incremental resume happens to land on, and never
    guessed from a line's *content*. The envelope is a property of the whole
    session file (one host wrote it start to finish), not of any one line.

    Returns ``"claude-code"``, ``"codex"``, or ``"unrecognised"``. The third
    state matters as much as the first two: a transcript shape this module
    does not know is reported loud rather than silently parsed as though it
    held zero exchanges, which is indistinguishable from a genuinely quiet
    session (#443).
    """
    if not isinstance(obj, dict):
        return "unrecognised"
    # Codex's marker is structural, not a specific `type` value: every line
    # -- session_meta, event_msg, response_item, world_state, turn_context,
    # and whatever a future Codex release adds -- carries a `payload` object.
    if isinstance(obj.get("payload"), dict):
        return "codex"
    if isinstance(obj.get("message"), dict) or obj.get("type") in ("user", "assistant", "summary", "system"):
        return "claude-code"
    return "unrecognised"


def codex_exchange(obj: dict) -> tuple[str, str] | None:
    """``(role, text)`` for one Codex rollout line, or ``None`` to skip it.

    Only an ``event_msg`` line whose payload is an ``item_completed`` event
    naming a ``UserMessage`` or ``AgentMessage`` item counts. Codex also
    writes the same text a second time, inside a ``response_item`` line at
    ``payload.role == "user"``/``"assistant"`` -- but that role also covers
    session scaffolding delivered the same way (the skills-instructions
    preamble, the recommended-plugins list, this plugin's own REMEMBER
    buffer), all of which arrive as ``role: "user"`` too. Reading
    ``response_item`` would count start-up scaffolding as a human turn.
    ``item_completed`` is Codex's own record of what a human actually sent
    and what the agent's final answer was, so it is the one signal that does
    not need a second filter layered on top of it.
    """
    if obj.get("type") != "event_msg":
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type == "UserMessage":
        role = "HUMAN"
    elif item_type == "AgentMessage":
        role = "AGENT"
    else:
        return None
    content = item.get("content")
    if not isinstance(content, list):
        return None
    texts = [
        text.strip()
        for block in content
        if isinstance(block, dict)
        for text in [block.get("text")]
        if isinstance(text, str) and text.strip()
    ]
    if not texts:
        return None
    return role, "\n".join(texts)


def transcript_path(env: Mapping[str, str] | None = None) -> str | None:
    """The transcript path the host handed us, if it is usable.

    Returns ``None`` for anything the caller could not open — unset, blank, a
    directory, a path that is not there. The caller then falls back to
    reconstructing it, which is what every caller did before this existed.

    Validated here rather than at the call site because the value is copied
    from a payload written by the host: it is data, and the one thing worse
    than reconstructing a path is trusting an unusable one and reporting the
    resulting emptiness as a session with nothing in it.

    NOT validated against containment or a session id (#424): this is an
    existence check only, and ``find_session()`` returns whatever this
    returns before its own traversal check ever runs. Callers that read
    ``env`` from a process whose environment could hold a value THEY did not
    set -- an inherited shell, an ambient dotfile -- must clear
    ``TRANSCRIPT_PATH_VAR`` before it reaches them, the way
    ``scripts/post-tool-hook.sh`` and ``scripts/user-prompt-hook.sh`` now do,
    rather than assume this function will catch an untrusted value. It will
    not: it exists to validate a payload the caller already trusts, not to
    decide whether the caller should have trusted it.

    #431 is the decision for every OTHER caller -- ``scripts/save-session.sh``
    run by hand, ``scripts/doctor.sh``, a direct ``python3 -m pipeline.extract``
    -- none of which has a hook preamble to clear anything in. No containment
    check was added here, and the reason is sharper than "it is hard": this
    function is the ONE channel both a legitimately-supplied and an ambient
    value travel through, so a check added here binds both. The legitimate
    value -- what ``session-start-hook.sh``/``session-end-hook.sh`` export
    fresh, from their own validated stdin payload, on every run -- is the
    whole point of #407: trust wherever the host says the transcript lives,
    rather than reconstruct it, because reconstruction is what #263/#174/#157
    got wrong. A containment rule narrow enough to matter (under the project
    directory; under ``CLAUDE_CONFIG_DIR``/``~/.claude``, which is itself a
    relocatable, user-set path and not "never" true of the project directory
    either -- see #166) would also reject a legitimate transcript the host
    handed over that happens to live somewhere else, on a different drive or
    mount, under a session-store layout this module has no business knowing
    (Codex and Gemini CLI are not documented here on purpose -- see the module
    docstring above). Splitting the two channels -- validate only the ambient
    one -- would need a second parameter threaded through every caller of
    ``transcript_path()``/``find_session()`` recording whether THIS call has a
    fresh stdin payload behind it, which is a real fix but a bigger one than
    this issue asked for, not attempted here. So the decision is the second
    option #424 offered: a caller with no preamble of its own inherits the
    ambient environment by design, the same way it already inherits ``$PATH``
    or ``$HOME``, and the hooks -- which run on every tool call whether or not
    a human is watching -- stay the hardened boundary. ``scripts/doctor.sh``
    says so loudly (a WARN naming the value) rather than silently trusting it,
    so the decision is never mistaken for an oversight.
    """
    env = os.environ if env is None else env
    value = (env.get(TRANSCRIPT_PATH_VAR) or "").strip()
    if not value:
        return None
    if not os.path.isfile(value):
        return None
    return value
