# Verifying the userConfig recovery-token fallback (historical, removed #860 round 3)

**This feature no longer exists.** The plugin's own recovery-token setting described
below -- the manifest option, the function that read it, and the config/env-var
predecessors it replaced -- was removed entirely in round 3 of #860: the nested
summarizer call now authenticates only through whatever the host already hands it or
the CLI's own login, with no recovery path of this plugin's own on any host. Everything
past this notice describes code and configuration that is gone; it is kept only as a
record of what was once tested by hand, not as a live procedure.

`pipeline/haiku.py` normally lets the nested `claude -p` inherit its
credential from the parent's `CLAUDE_CODE_OAUTH_TOKEN`, kept across the
parent-session-var strip by `_CHILD_ENV_KEEP`. That path is covered by the
suite.

This page is about the fallback for when there is nothing to keep: some
hosts — specifically the **Claude Code desktop / Agent SDK host** — redact
that token from every spawned tool and hook subprocess before `os.environ`
is even populated. `_inject_configured_oauth_token` then looks for a token
the operator configured directly through the plugin's `oauth_token`
userConfig option (`/plugin` → `remember` → Configure, or `claude plugin
config set remember oauth_token <token>`) and injects it if the child env
still lacks a token after that (#860, round 2 — this used to also accept a
`REMEMBER_OAUTH_TOKEN` env var or a `haiku.oauth_token` config.json key;
neither is read for this any more, on any host).

**This path cannot be covered by CI.** The redaction is a host boundary, not
something the pipeline or the suite controls, and CI never runs under that
host. The suite mocks the plumbing and proves the precedence order; it
cannot prove the fallback recovers auth on a machine that withholds the
token, and it cannot prove a userConfig value actually reaches
`CLAUDE_PLUGIN_OPTION_OAUTH_TOKEN` in a real install either — that mechanism
is reasoned from the Claude Code plugin manifest reference plus a passing
`claude plugin validate .`, not independently observed end to end. The only
evidence this whole path works is a manual run against a real host,
originally reported in a comment on
[#179](https://github.com/Digital-Process-Tools/claude-remember/issues/179)
for the now-removed env var. This page turns that into a repeatable
procedure so the next verification isn't starting from scratch, and so its
evidence lives here instead of scrolling out of a comment thread.

## When to re-run this

Any change to auth resolution in `call_haiku`:

- the precedence order in `_child_env` / `_inject_configured_oauth_token`
  (host token vs. configured fallback — a host-provided token must always
  win),
- `_looks_like_token` (the shape check — currently non-empty,
  whitespace-free, `_MIN_TOKEN_LEN` = 20 chars),
- `_configured_oauth_token` (reads only the plugin's own `userConfig` option
  via `CLAUDE_PLUGIN_OPTION_OAUTH_TOKEN`),
- `_legacy_oauth_config_present` (the presence-only check that still looks
  for a configured-but-ignored legacy value, to log the migration notice).

CI will pass on all of these changes regardless of whether the fallback
still works, because CI never exercises the condition it exists for.

## 1. Preconditions

- A real, working token: run `claude setup-token` and keep the value.
- A machine/host where `CLAUDE_CODE_OAUTH_TOKEN` is actually withheld from
  spawned subprocesses — the Claude Code desktop app or an Agent SDK host.
  Running this from a plain terminal `claude` session does not reproduce
  the condition; the token is present there.
- The token configured through `/plugin` → `remember` → Configure (or
  `claude plugin config set remember oauth_token <token>`), which sets the
  `oauth_token` userConfig option.

## 2. Confirm the negative precondition first

This is the step the issue calls out by name, and it's the one that makes
the rest of the run meaningful: if `CLAUDE_CODE_OAUTH_TOKEN` is *not*
actually absent from the child env, the fallback never engages, and a
"pass" below would be testing nothing.

`_child_env()` only keeps `CLAUDE_CODE_OAUTH_TOKEN` from `os.environ` if
it's there to begin with, so checking the parent process's own environment
is equivalent to checking what the nested call would see before the
fallback runs. From inside the same host-spawned process you're about to
verify from:

```bash
python3 -c "import os; print('CLAUDE_CODE_OAUTH_TOKEN' in os.environ)"
```

Expected: `False`. If it prints `True`, stop — you're not running under the
redaction condition and this run proves nothing about the fallback.

## 3. The positive assertion

With the negative precondition confirmed and the userConfig `oauth_token`
option set to a real token (step 1 above):

```python
from pipeline.haiku import call_haiku

result = call_haiku("Reply with exactly the word PONG and nothing else.")
print(result.is_skip, repr(result.text))
```

Expected: no exception, `result.is_skip is False`, and `result.text`
contains the expected reply. That confirms `_inject_configured_oauth_token`
filled in `CLAUDE_CODE_OAUTH_TOKEN` for the nested `claude -p` from the
`CLAUDE_PLUGIN_OPTION_OAUTH_TOKEN` env var Claude Code exported, and it
authenticated end to end.

## 4. The negative case — malformed token

Set the userConfig `oauth_token` option to something shorter than
`_MIN_TOKEN_LEN` (20 chars), e.g. `"too short"`, and repeat the call above
(or call `_inject_configured_oauth_token({})` directly).

Expected in the daily log (`$REMEMBER_DIR/logs/`, written via
`pipeline/log.py`; falls back to stderr only when `REMEMBER_DIR` is unset):

```
WARNING: ignoring the plugin's userConfig oauth_token option — not a
plausible OAuth token (want a whitespace-free string of at least 20
chars); the nested CLI will run unauthenticated unless the host provides a
token of its own
```

Check specifically:

- the warning names the **setting** (the plugin's userConfig option),
  written as a hardcoded literal rather than interpolated from any
  identifier -- round 1 of this message named it by interpolating the env
  var's own name (`CLAUDE_PLUGIN_OPTION_OAUTH_TOKEN`), and a second CodeQL
  round flagged that too: a value flowing from a constant whose own
  identifier contains "TOKEN" is treated as sensitive regardless of what
  it actually is (#860, round 3),
- an earlier version of this message also reported the configured value's
  **length** (e.g. "got 9 chars") -- CodeQL flagged that as a HIGH "clear
  -text logging of sensitive information" alert (#860, round 1 of the
  fix): a length is still a value DERIVED from the secret, and the taint
  tracker follows that derivation to the log sink regardless of how little
  survives. The message now carries no value-derived content at all, and
  no identifier-derived content either -- only fixed, hand-written text,
- the configured value itself never appears anywhere in the log line
  (`_accept_token` in `pipeline/haiku.py` — see
  [#184](https://github.com/Digital-Process-Tools/claude-remember/issues/184),
  which closed the silent-refusal gap this check exists to prevent).

An empty value (unset) must stay silent — no warning — since it means "not
configured", not "misconfigured". Verify that case produces no log line.

## 5. The migration notice (#860, round 2)

Separately from the three steps above: with the userConfig option unset but
a legacy `REMEMBER_OAUTH_TOKEN` env var (or `haiku.oauth_token` in
`config.json`) still set to anything non-empty, repeat the call in step 3.

Expected: `result.is_skip` is **not** necessarily `False` — the legacy value
authenticates nothing now — but the daily log carries a line naming the
legacy source (`REMEMBER_OAUTH_TOKEN` or `haiku.oauth_token in <path>`) and
pointing at `/plugin` → `remember` → Configure, and that line never contains
the configured value itself. `/remember:doctor`'s own "Legacy recovery-token
config" section should surface the same line.

## 6. What to record

Every run of this procedure, record in the PR/issue that triggered it:

- **OS** (this is host-boundary behaviour; it may differ across hosts/OSes),
- **Python version** (`python3 --version`),
- **Package version** — `.claude-plugin/plugin.json`'s `"version"` field,
  plus the commit (`git rev-parse --short HEAD`),
- **Date**.

That's what lets a future reader judge how stale the last verification is
without re-running it themselves.

### Verification log

| Date | OS | Python | Package | Result |
| ---- | -- | ------ | ------- | ------ |
| 2026-04 (reported on [#179](https://github.com/Digital-Process-Tools/claude-remember/issues/179)) | Windows, Claude Code desktop host | — | 0.8.8 (`0a09b96`) | Positive and negative cases both confirmed against a real host, for the since-removed `REMEMBER_OAUTH_TOKEN` env var — see the comment thread for the exact log lines. Not yet re-run against the userConfig mechanism this page now describes. |

Add a row here each time this procedure is re-run.
