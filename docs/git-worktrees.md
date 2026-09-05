# Git worktrees

Moved verbatim from the README.

Claude Code sets `CLAUDE_PROJECT_DIR` to the *worktree* path for sessions started inside a [git worktree](https://git-scm.com/docs/git-worktree). Memory is deliberately **not** kept in the worktree — it is keyed to the repository's **main checkout** instead, so that:

- it survives `git worktree remove` (a worktree-local `.remember/` would be deleted with the worktree — silently, since it is gitignored with `*`), and
- every worktree of the same repo shares one continuous memory rather than a separate throwaway one.

Concretely, `REMEMBER_DIR` resolves through git's *common dir*: in legacy mode it lands in `<main-checkout>/.remember/`, and in external mode the `{slug}` is computed from the main checkout, so all worktrees map to the same `~/.remember/<slug>/`. Only the memory location is redirected — `CLAUDE_PROJECT_DIR` is left as the worktree path, so session recovery still finds transcripts where Claude Code stored them. Non-worktree checkouts and non-git projects are unaffected.
