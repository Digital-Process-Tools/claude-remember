# Trusting the nested model call

`pipeline/haiku.py` spawns a nested `claude -p` and reads its stdout as the
model's reply. Everything downstream — the session entry written to staging,
the consolidated `recent.md`, the compressed `now.md` — is that string.

This document is the rule that governs how that string may be validated. It
exists because breaking it destroyed a user's permanent memory record
([#202](https://github.com/Digital-Process-Tools/claude-remember/issues/202)).

## The rule

> **A validity check on nested-model output must be able to reject an echo of
> its own input.** Positive-only checks cannot, and must not be used alone.

If your check is a list of things that must be *present*, ask one question
before you ship it: *is every one of those things also in the prompt?* If the
answer is yes, the check passes on a verbatim quotation of the prompt, and a
quotation of the prompt is exactly what several real failure modes produce.

Adding another positive pattern does not fix this. It reproduces it with more
steps.

## Why an echo arrives at all

The nested call's stdout is not guaranteed to be the model speaking.

The concrete case: hooks. `claude -p` inherits the user's hooks unless they are
isolated. A `UserPromptSubmit` hook that **blocks** does not make the call fail
— the CLI writes its block message to stdout, quotes the prompt back under
`Original prompt:`, reports `subtype: success`, and **exits 0**. Verified on
claude-code 2.1.219:

```
UserPromptSubmit operation blocked by hook:
[<command>]: <the hook's stderr>


Original prompt: <the entire prompt, verbatim>
```

No exit code, status field, or usage figure distinguishes that from an answer.
Only the text does.

Hooks are one door. The general shape is "something that is not the model's
reply arrived on stdout", and the next instance of it will not be a hook.

## Why positive checks fail specifically here

The consolidation prompt embeds the staging files *verbatim* and prints the
required `===RECENT===` / `===ARCHIVE===` envelope as part of its
output-format instructions. So a quotation of that prompt contains:

- `===RECENT===` and `===ARCHIVE===`, and
- real `## HH:MM | branch` entry headers, and
- everything else that "looks like memory"

…because all of it *is* memory, quoted. `_is_valid_consolidation` asked "does
this look like memory?", got yes, and wrote a hook's refusal into `recent.md`.
The next run quoted the poisoned file into a new prompt and it nested one level
deeper each time: 29,153 bytes, 689 lines, 7 levels, with `archive.md` replaced
by the literal `[archive.md content here]` placeholder out of the template.

## What actually separates a reply from an echo

Not content — the echo's content is the input's content, and the input is real
memory. The separator is the **instructions**: prose addressed to the model,
which the model has no reason to reproduce and which an echo cannot avoid.

That is what `consolidate._echoes_the_prompt` tests, and it derives its markers
from `prompts/consolidate-staging.prompt.txt` rather than restating them, so
editing the template cannot silently retire the guard.

## Fail in the safe direction

Rejecting a *good* consolidation costs a skipped run: staging and memory are
both left untouched, and the next run retries. Accepting a *bad* one costs the
permanent record, irreversibly, and the source staging files get retired behind
it. Those are not comparable, so the guard is tuned to over-reject.

## Two layers, and why neither is enough alone

| Layer | Where | Covers | Can it be degraded away? |
| ----- | ----- | ------ | ------------------------ |
| Hook isolation (`--setting-sources ''`) | `haiku.py`, argv | the hook vector, for every pipeline stage | **yes** — see below |
| CLI-notice rejection | `haiku.py`, `_parse_response` | the CLI's own voice, every stage | no |
| Echo rejection | `consolidate.py` | any quotation of the consolidation prompt | no |

Isolation is the only one that can be lost, and it can be lost for reasons
outside our control:

- an older CLI has no `--setting-sources`, and commander exits non-zero on an
  unknown option;
- excluding every setting source also excludes `apiKeyHelper` and the `env`
  block, which is how enterprise / Bedrock / proxy installs authenticate.

In both cases the call fails before reaching the API, so it cost nothing, and
`call_haiku` retries once without isolation and logs a warning. **Isolation
fails open into the guards, never closed into an outage** — because a fix that
turns a corruption bug into "no saves ever again" is a worse bug than the one
it fixed. That is the trap
[#204](https://github.com/Digital-Process-Tools/claude-remember/issues/204)
was filed about.

This is the whole argument for keeping the layers independent: the one that can
be switched off by someone else's configuration must not be the one holding the
memory record.
