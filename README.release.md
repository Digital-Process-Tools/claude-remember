# Claude Remember

Continuous memory for your coding agent. It hooks into your coding agent's lifecycle,
saving each session automatically, compressing it into layered summaries, and loading
that history back into context the next time you start a session. No manual prompting
or copy-pasting notes: the agent begins every session already knowing what it worked
on before.

## What this plugin runs, sends and stores

Summarization shells out to a CLI you already have installed and authenticated --
never a bundled binary, never a third-party service. The nested `claude` call uses
your own existing login, the same one your interactive session already uses: nothing
is typed in, and nothing is read from your operating system's credential storage.
Memory is stored locally under your project by default, and nothing is pushed
anywhere unless you opt into git backup yourself.

If your coding agent does not hand hooks that login, set an optional recovery token
through the plugin's own Configure option in your coding agent's plugin settings.

The full README, including the install guide, the complete trust model, and every
configuration key, lives on this project's default branch.
