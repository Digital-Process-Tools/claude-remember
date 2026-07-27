---
description: Diagnose the Remember plugin — resolved paths, detected tools, storage mode, and whether capture is actually saving memory.
allowed-tools: Bash
---

Run this exact command and relay its output back to the user **verbatim**, inside a code block, with no summarizing, editing, or omitting of lines:

```
bash "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh"
```

After the code block, do not add your own diagnosis or next steps unless the user asks — the script's report is the answer, and re-deriving it from your own guesses is how a confident wrong answer gets attached to a correct report.

If the report states a problem, quote the `VERDICT` line back in plain language **together with any `FAIL` or `WARN` lines above it**. The verdict names the single most likely cause; the lines above it are the evidence, and when more than one thing is wrong they carry detail the verdict cannot. Never contradict a `FAIL` line — if the verdict and a `FAIL` line seem to disagree, say so plainly rather than picking one, because that disagreement is itself worth reporting.
