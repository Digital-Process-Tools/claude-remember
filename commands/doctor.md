---
description: Diagnose the Remember plugin — resolved paths, detected tools, storage mode, and whether capture is actually saving memory.
allowed-tools: Bash
---

Run this exact command and relay its output back to the user **verbatim**, inside a code block, with no summarizing, editing, or omitting of lines:

```
bash "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh"
```

After the code block, do not add your own diagnosis or next steps unless the user asks — the script's report and its VERDICT line are the answer. If the report's VERDICT line states a problem, you may quote that one line back in plain language, but do not re-derive or second-guess the finding.
