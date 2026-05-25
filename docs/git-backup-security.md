# Git Backup — Security Model

This page only matters if you enable the **git backup** feature (`hooks.d/after_save/50-git-backup.sh`). The default install does not push your memory anywhere — no new attack surface beyond what Claude Code already needs to run on your machine.

If you do enable git backup, read on.

---

## The "is this special?" question

> *"Anyone who can write `~/.remember/config.json` can redirect the backup remote to their own URL and silently exfiltrate every session."*

True. Also true for:

- `~/.ssh/config` — redirect your `git push` to an attacker's host.
- `~/.ssh/authorized_keys` — grant SSH access.
- `~/.bashrc` / `~/.zshrc` — code execution on every shell.
- `~/.claude/**` — change which hooks Claude Code runs.
- `~/.gitconfig` — `[core] sshCommand = ...` runs arbitrary code on every git operation.

If something can write to your home directory as your user, you are already compromised. The threat model "attacker with write-access to `$HOME`" is game over independent of this plugin. Treat `~/.remember/` with the same care you give `~/.ssh/` — that's the bar, and it's not a higher one.

---

## Threats specific to git backup

These are the things that only apply once you enable the feature.

### 1. The remote you push to receives a copy of everything you discuss with Claude Code

That includes project paths, session summaries, identity files, any data the model wrote into memory, and any content you accidentally pasted into a session. If you point the remote at a service you don't fully trust, you're streaming your work history there continuously.

**Mitigation:** point the remote at a private repository you own. GitHub private, self-hosted Gitea, a `git init --bare` on your own server — anything where you control access.

### 2. The configured remote can drift if `config.json` is tampered with

Without protection, an attacker writing `~/.remember/config.json` could swap the remote URL between sessions and the next save would silently push to their host.

**Mitigation built into the plugin:** the backup hook validates the remote URL on every push and aborts if it has changed from the value originally set. To intentionally change the remote, set `git_backup.allow_remote_change` in config (one-shot opt-in). See [`README.md`](../README.md) for the option.

### 3. `hooks.d/` is executed on every session save and start

Same as Claude Code's own hook directory. Anything you (or an installed plugin) drops in `hooks.d/` runs with your user privileges. The plugin cache at `~/.claude/plugins/cache/` is user-writable by design — a malicious plugin can add hooks there.

**Mitigation:** this is install-time trust. Only install plugins you've reviewed. Same rule as `npm install`, `pip install`, or any package manager pulling code that runs on your machine.

---

## Recommended setup

If you want git backup with reasonable defaults:

```bash
# 1. Restrictive permissions (same as ~/.ssh)
chmod 700 ~/.remember
chmod 700 ~/.claude/plugins/cache

# 2. Point backup at a private repo you own
git init --bare ~/backups/claude-remember.git    # or use a private GitHub/Gitea/etc.
# Then set git_backup.remote in ~/.remember/config.json

# 3. Verify the validation guard is active (default: on)
# git_backup.allow_remote_change is false unless you explicitly flip it
```

After this:

- Data leaves your machine only to a repo you control.
- The remote can't silently change without `allow_remote_change`.
- The home-dir attack surface is no worse than `~/.ssh/`.

---

## What you're consenting to (in one sentence)

**Enabling git backup means: every memory save is pushed to the remote you configured.** That's it. Everything above is about making sure "the remote you configured" stays the remote you configured.
