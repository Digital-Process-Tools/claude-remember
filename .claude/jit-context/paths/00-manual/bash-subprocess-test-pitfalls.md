---
title: "A bash-subprocess test: Windows reglobs unescaped argv, and a 'one-line | block' extraction regex can run past the intended match if the lazy branch goes first"
description: "#898: a test fed adversarial strings through bash -c SCRIPT runner *inputs and got 25 extra rows on windows-latest -- Git Bash's own argv rebuild glob-expanded an unquoted '*'. Separately, an extraction regex with a one-line-form | block-form alternation tried the lazy block form first and silently matched 209 lines past a one-line function."
match: (scripts/.*\.sh|tests/.*\.py)$
---

Two distinct, windows-only-or-silent-everywhere-else pitfalls found while building
`tests/_bash_runner.py`-style harnesses for cross-platform bash tests (#898):

**1. Git Bash reglobs its own argv on Windows.** Passing adversarial strings through
`bash -c SCRIPT runner *inputs` (argv, not stdin) hits Git Bash's own msys/cygwin
`build_argv`/`globify`: an unquoted no-space argument is glob-expanded against the
cwd (`*` became 25 filenames -- exactly the repo root's entry count), a backslash
becomes a glob escape, and an embedded newline splits into two arguments. ubuntu
and macOS stayed green; only `windows-latest` failed (30/33 legs that round).
**Fix:** `tests/_bash_runner.py`'s `run_bash_file` (script via a temp file, not
inline) + `bash_octal` (inputs via stdin, `\0ooo`-escaped, rebuilt with
`printf -v x '%b'`) -- and assert every row echoes its input exactly, so a
transport bug fails loudly instead of silently comparing something else. Side
trap: bash 3.2's `printf -v x '%b' ""` **unsets** `x` (5.x assigns `""`) -- skip
the empty-input case or test for it explicitly.

**2. An extraction regex with a "one-line form | block form" alternation must try
the specific (one-line) form first.** `^name\(\) \{.*?^\}$|^name\(\) \{[^\n]*\}$`
under `re.DOTALL` tries the lazy block-form branch first; for a genuinely one-line
function it still matches via that branch, with the lazy `.*?^\}$` running all the
way to the *next* line that is exactly `}`, possibly hundreds of lines later,
silently pulling in unrelated code. macOS printed the right verdict anyway (noise
on stderr nobody read); windows-latest printed nothing for the extracted (wrong)
text and failed a plain string-equality assertion. **Fix:** put the specific
alternative first, and add a positive control that fails on the old ordering and
passes on the corrected one -- asserting the extracted text is only the target,
not merely that it "looks right."

If you are writing or editing a test here that shells out to `bash` with
attacker-shaped/adversarial input, or that extracts a shell function's body with a
regex: check both of these before trusting a green run on your own platform.
