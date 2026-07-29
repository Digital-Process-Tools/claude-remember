# Verifying the `REMEMBER_OAUTH_TOKEN` fallback

`pipeline/haiku.py` normally lets the nested `claude -p` inherit its
credential from the parent's `CLAUDE_CODE_OAUTH_TOKEN`, kept across the
parent-session-var strip by `_CHILD_ENV_KEEP`. That path is covered by the
suite.

This page is about the fallback for when there is nothing to keep: some
hosts — specifically the **Claude Code desktop / Agent SDK host** — redact
`CLAUDE_CODE_OAUTH_TOKEN` from every spawned tool and hook subprocess before
`os.environ` is even populated. `_inject_configured_oauth_token` then looks
for a token the operator configured directly (`REMEMBER_OAUTH_TOKEN`, or
`haiku.oauth_token` in `config.json`) and injects it if the child env still
lacks a token after that.

**This path cannot be covered by CI.** The redaction is a host boundary, not
something the pipeline or the suite controls, and CI never runs under that
host. The suite mocks the plumbing and proves the precedence order; it
cannot prove the fallback recovers auth on a machine that withholds the
token. The only evidence that it does is a manual run against a real host,
reported in a comment on
[#179](https://github.com/Digital-Process-Tools/claude-remember/issues/179).
This page turns that one-off run into a repeatable procedure so the next
verification isn't starting from scratch, and so its evidence lives here
instead of scrolling out of a comment thread.

## When to re-run this

Any change to auth resolution in `call_haiku`:

- the precedence order in `_child_env` / `_inject_configured_oauth_token`
  (host token vs. configured fallback — a host-provided token must always
  win),
- `_looks_like_token` (the shape check — currently non-empty,
  whitespace-free, `_MIN_TOKEN_LEN` = 20 chars),
- the config layer feeding it: `_config_candidates` / `_configured_oauth_token`
  (env var, `REMEMBER_CONFIG`, `$REMEMBER_DIR/config.json`,
  `~/.remember/config.json`).

CI will pass on all of these changes regardless of whether the fallback
still works, because CI never exercises the condition it exists for.

## 1. Preconditions

- A real, working token: run `claude setup-token` and keep the value.
- A machine/host where `CLAUDE_CODE_OAUTH_TOKEN` is actually withheld from
  spawned subprocesses — the Claude Code desktop app or an Agent SDK host.
  Running this from a plain terminal `claude` session does not reproduce
  the condition; the token is present there.
- The token configured one of two ways:
  - `REMEMBER_OAUTH_TOKEN` env var, or
  - `haiku.oauth_token` in `config.json` (`$REMEMBER_DIR/config.json`, or
    `~/.remember/config.json` if `REMEMBER_DIR` is unset).

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

With the negative precondition confirmed and `REMEMBER_OAUTH_TOKEN` (or
`haiku.oauth_token`) set to a real token:

```python
from pipeline.haiku import call_haiku

result = call_haiku("Reply with exactly the word PONG and nothing else.")
print(result.is_skip, repr(result.text))
```

Expected: no exception, `result.is_skip is False`, and `result.text`
contains the expected reply. That confirms `_inject_configured_oauth_token`
filled in `CLAUDE_CODE_OAUTH_TOKEN` for the nested `claude -p` and it
authenticated end to end.

## 4. The negative case — malformed token

Set `REMEMBER_OAUTH_TOKEN` (or `haiku.oauth_token`) to something shorter
than `_MIN_TOKEN_LEN` (20 chars), e.g. `"too short"`, and repeat the call
above (or call `_inject_configured_oauth_token({})` directly).

Expected in the daily log (`$REMEMBER_DIR/logs/`, written via
`pipeline/log.py`; falls back to stderr only when `REMEMBER_DIR` is unset):

```
WARNING: ignoring REMEMBER_OAUTH_TOKEN — not a plausible OAuth token (want a
whitespace-free string of at least 20 chars, got 9 chars); the nested CLI
will run unauthenticated unless the host provides a token of its own
```

Check specifically:

- the warning names its **source** (`REMEMBER_OAUTH_TOKEN`, or
  `haiku.oauth_token in <path>`),
- it reports a **length** (`9 chars` above), not the value,
- the configured value itself never appears anywhere in the log line
  (`_accept_token` in `pipeline/haiku.py` — see
  [#184](https://github.com/Digital-Process-Tools/claude-remember/issues/184),
  which closed the silent-refusal gap this check exists to prevent).

An empty value (`""`) is the shipped default and must stay silent — no
warning — since it means "not configured", not "misconfigured". Verify that
case produces no log line.

## 5. What to record

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
| 2026-04 (reported on [#179](https://github.com/Digital-Process-Tools/claude-remember/issues/179)) | Windows, Claude Code desktop host | — | 0.8.8 (`0a09b96`) | Positive and negative cases both confirmed against a real host — see the comment thread for the exact log lines. |

Add a row here each time this procedure is re-run.
