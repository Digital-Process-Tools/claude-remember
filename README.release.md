# Claude Remember

Continuous memory for your coding agent. It hooks into your coding agent's lifecycle,
saving each session automatically, compressing it into layered summaries, and loading
that history back into context the next time you start a session. No manual prompting
or copy-pasting notes: the agent begins every session already knowing what it worked
on before.

## What this plugin runs, sends and stores

Summarization shells out to a CLI you already have installed and authenticated --
never a bundled binary, never a third-party service. The summarizer runs a nested
`claude -p` that inherits your environment, including your Claude Code login, exactly
like any process a hook starts; only the parent session's own variables are removed
first, so the nested call does not pass as that session. A nested `codex exec` is
given only an allow-list of your variables, set in the plugin's own config, that names
no credential: if you authenticate Codex through an environment variable rather than
its own login, add that variable's name to the list in your own config. remember
itself reads no credential: nothing is typed in, and nothing is read from your
operating system's credential storage. Memory is stored locally under your project by
default, and nothing is pushed anywhere unless you opt into git backup yourself.

If your coding agent does not hand hooks that login, the nested call simply runs
unauthenticated; this plugin has no recovery path of its own. Log in again with your
coding agent's own CLI and the next save picks it up. To keep some other variable
away from the summarizer, unset it in the environment you start Claude Code from.

The full README, including the install guide, the complete trust model, and every
configuration key, lives on this project's default branch.
