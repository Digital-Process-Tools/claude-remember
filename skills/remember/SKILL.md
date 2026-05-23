---
name: remember
description: Save session state for clean continuation next session.
allowed-tools: Read, Write
---

Write a handoff note so the next session can continue cleanly. Use your knowledge of the current session — you were here. Write in first person ("I").

**Path:** Use the path from the most recent `=== HANDOFF ===` block in this session's context (e.g., `Write next handoff to: /home/user/.remember/myproject-slug/remember.md`). If no `=== HANDOFF ===` block is present, fall back to `{project_root}/.remember/remember.md`. This is at the PROJECT ROOT, NOT relative to this skill file.

**If the file already exists, Read it first before Writing.** The Write tool enforces a read-before-write check on existing files; without a prior Read, the first Write call will fail with "File has not been read yet." A 1-line Read is enough to satisfy the check.

Format:

```
# Handoff

## State
{What's done, what's not. Files, MRs, decisions. 2-4 lines max.}

## Next
{What to pick up. Priority order. 1-3 items.}

## Context
{Non-obvious gotchas, blockers, preferences from this session. Skip if nothing.}
```

Rules:

- Under 20 lines total
- Specific: file paths, MR numbers, branch names
- Forward-looking — the next session doesn't care about the journey
- If nothing meaningful to hand off, write: "No active work."

Say "Saved." when done — nothing else.
