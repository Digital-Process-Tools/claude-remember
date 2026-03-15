---
name: remember
description: Save session state for clean continuation next session.
allowed-tools: Write
---

Write a handoff note so the next session can continue cleanly. Use your knowledge of the current session — you were here. Write in first person ("I").

**Path:** `{project_root}/.remember/remember.md` (overwrite). This is at the PROJECT ROOT, NOT relative to this skill file. If the project root is `/Users/foo/myproject`, the file goes to `/Users/foo/myproject/.remember/remember.md`.

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
