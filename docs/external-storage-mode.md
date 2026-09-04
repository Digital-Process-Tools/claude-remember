## External storage mode

By default, memory data lives in `.remember/` inside each project directory. This works but has a drawback: it pollutes `git status` and siloes memory per repo clone.

**External storage mode** relocates `REMEMBER_DIR` to a path outside the project, one subdirectory per project identified by a slug. The `{slug}` placeholder expands to the same value Claude Code uses for `~/.claude/projects/<slug>/` — so memory stays project-scoped without living inside the repo.

### Enable

Create `~/.remember/config.json`:

```json
{ "data_dir": "~/.remember/{slug}" }
```

On next session start, the plugin:

1. Resolves `REMEMBER_DIR` to `~/.remember/<slug-of-project>/`
2. Auto-migrates any existing `<project>/.remember/` to the new location — once, leaving a `MIGRATED-TO.txt` marker in the old directory
3. Skips writing `.gitignore` (the external directory is not inside a git repo)

### `{slug}` expansion

`data_dir` values starting with `/` or `~` are treated as absolute. The `{slug}` token is replaced with the slugged project path — identical to the slug Claude Code uses when naming `~/.claude/projects/<slug>/`. All non-alphanumeric characters become `-`:

```
~/.remember/{slug}  →  ~/.remember/-home-alice-projects-my-app
```

### Handoff path

When external mode is active, `session-start-hook.sh` emits a `=== HANDOFF ===` block at session start:

```
=== HANDOFF ===
Write next handoff to: /home/alice/.remember/-home-alice-projects-my-app/remember.md
```

The `/remember` skill reads this block to know where to write. If no block is present (legacy mode), it falls back to `{project_root}/.remember/remember.md`.

**This same hint is what makes `handoff_mode: "per_session"` (see [Handoff between sessions](../README.md#handoff-between-sessions-remember) and the config table) work with no change to the `/remember` skill** ([#363](https://github.com/Digital-Process-Tools/claude-remember/issues/363)): in legacy mode with `per_session` on, the hint fires too — even though external mode is off — because `remember.<session_id>.md` no longer matches the skill's own hardcoded legacy fallback, so the hint is the only thing that can still point it at the right file. If `per_session` is on but no usable `session_id` reached the hook, the hint still fires with the shared `remember.md` path — that path is still correct, and an earlier version of this feature withheld the hint outright here, which broke external mode by reintroducing the exact bug the hint exists to prevent — but a second line says the fallback happened, so it is visible rather than read as isolation.

### Per-project identity override

Place an `identity.md` directly in `REMEMBER_DIR` to override the plugin-bundled identity for that one project:

```
~/.remember/<slug>/identity.md
```

If this file exists it takes precedence over `<plugin>/identity.md`. The per-project version is never overwritten by plugin updates.

### Back up your memory

Because `~/.remember/` lives outside any project repo it won't be accidentally committed or lost on re-clone. To keep it safe, track it in a private git repository:

```bash
cd ~/.remember
git init
git remote add origin git@github.com:youruser/remember-backup.git  # private repo
# Write .gitignore BEFORE any git add — this excludes log and tmp dirs.
# Running git add before this step will track log dirs you don't want committed.
cat > .gitignore <<'EOF'
*/logs/
*/tmp/
EOF
git add .gitignore config.json
git commit -m "init: remember config"
git push -u origin main
```

> **Where the hooks keep their own state.** Nothing is written to the store
> root. The backup and restore hooks keep their lock, cooldown stamp, recorded
> remote URL and failure counters inside the repository's git directory
> (`.git/remember/`), which git never tracks, never merges and never reports —
> so no `.gitignore` entry is needed and `git status` in your store stays clean.
> Versions before this one wrote those files beside your memory as
> `.git-backup-*` / `.git-restore-*` / `.last-git-backup-ts`; they are moved
> automatically on the next backup, and a copy you had already committed is left
> alone rather than deleted out of your repository. If you have those names in
> an existing `.gitignore`, they are harmless and can be removed at your leisure.

> **Note:** This first commit only tracks `.gitignore` and `config.json` — there's no memory in the backup yet. Per-project slug directories aren't tracked until the `after_save` hook runs after your next `/remember`. To confirm backup is working, run `/remember` once, then check `cd ~/.remember && git log` for an automatic commit. (If you already have memory to commit now, `git add <slug>/` it explicitly before the first push.)

#### Automatic commits

Once `~/.remember/` is a git repo, the `after_save` hook commits each project's memory subdir on its own schedule — one commit per project save, throttled by `cooldowns.git_backup_seconds` (default 15 min) — and pushes to your configured remote. No further setup is needed beyond credential availability (SSH agent or git credential helper) in the environment Claude Code launches hooks in.

**What is not backed up:** each slug's `logs/` and `tmp/`. Those are per-machine — pipeline logs, lock files, cooldown markers, and the handoff delivery record — and sharing them between machines causes conflicts at best and wrong answers at worst ([#285](https://github.com/Digital-Process-Tools/claude-remember/issues/285)). The hook maintains these exclusions in your store's `.git/info/exclude`, which is per-clone and is never itself committed, so no `.gitignore` of yours is edited and nothing about your machine reaches the remote. Everything else under the slug — every memory file — is backed up.

If you don't want automatic commits, leave `~/.remember/` as a plain directory and commit manually as before.

#### Logs in a backup you made before this version

The exclusion above is new. **0.12.3 and earlier staged each slug's whole subtree with nothing excluded**, on the assumption — written into the backup hook's own comment — that a root-level `.gitignore` covered `logs/` and `tmp/`. The plugin never created that file, and in external-store mode it deleted the only `.gitignore` it did write ([#285](https://github.com/Digital-Process-Tools/claude-remember/issues/285)). The setup snippet under [Back up your memory](#back-up-your-memory) has always told you to write one by hand; **if you did, none of this applies to you.** If you did not, this plugin's session logs were committed and pushed alongside your memory.

Upgrading changes what happens next and nothing about what already happened. The first backup after upgrading untracks `logs/` and `tmp/` in a commit of its own — `untracked <slug>/logs and <slug>/tmp` in the backup log — so they leave the *current* state of the remote and stop being pushed. **They remain in every commit that already carried them.** Untracking a path does not rewrite the commits that hold it, and no later fix on our side can: anyone who clones your backup gets that history and can read the logs out of it.

Whether that matters depends on what your sessions logged, which we cannot see and you can. **Nobody has counted how many stores are affected** — this is here so you can check your own, not as an estimate of anyone else's:

```bash
git -C ~/.remember log --oneline -- '*/logs/*'
```

Any output lists commits carrying log files. No output means there is nothing to decide.

**If you want them gone, read the cost before you run anything.** Removing them means rewriting every commit that touched them and force-pushing the result. Every commit ID from the first affected one onward changes, so **every other clone of this store — your other machine, a mirror, anything that has ever fetched it — diverges permanently.** `git pull` there will refuse to fast-forward; each clone has to be replaced, and anything committed there but not yet pushed is lost with it. A rewrite is also not a guarantee of deletion: hosts keep unreachable objects for a while, and a fork, a cached view or a downstream backup of the remote may keep the old ones indefinitely. If this store lives on one machine and the remote is private and yours alone, the rewrite is cheap. If it does not, that is the trade you are making.

With that understood, using [`git-filter-repo`](https://github.com/newren/git-filter-repo), on a throwaway clone rather than on `~/.remember` itself:

```bash
git clone ~/.remember /tmp/remember-purge
cd /tmp/remember-purge
git filter-repo --invert-paths --path-glob '*/logs/*' --path-glob '*/tmp/*' --force
git remote add origin <your backup remote>   # filter-repo drops the remote deliberately
git push --force origin main
```

Then **re-clone `~/.remember` on every machine that uses it** rather than pulling into it.

Doing nothing is a legitimate answer — these are your own session logs in a repository you own. It should just be a decision rather than something nobody told you.

#### When a push does not go through

A push can fail for two very different reasons, and the backup log tells them apart rather than lumping them together ([#253](https://github.com/Digital-Process-Tools/claude-remember/issues/253)):

| Log line | What it means | What to do |
| --- | --- | --- |
| `pushed <slug>` | Memory is on the remote. | Nothing. |
| `push deferred (will retry next backup)` | The push did not reach the remote at all — offline, VPN down, credential helper asleep. git never judged your commits. | Nothing. The next backup retries and normally succeeds. |
| `ERROR: push REJECTED by the remote — the backup has STOPPED …` | git *did* judge them and said no, almost always because the remote has moved ahead (another machine pushed). **No retry can fix this.** Memory is still being committed locally, but it is not leaving the machine. | Resolve it yourself: `git -C ~/.remember push` shows git's own advice. |

The rejection is deliberately **not** resolved for you. `recent.md` and `archive.md` are rewritten wholesale by consolidation rather than appended, so a conflict in them is real and an automatic merge or rebase could corrupt memory silently. The plugin never runs `fetch`, `pull`, `merge` or `rebase` on your store.

After `git_backup.reject_notice_after` consecutive rejections (default 3), the next prompt also carries a one-line `systemMessage` in your terminal, because a stopped backup that only ever appears in a log file is a stopped backup nobody notices — the reporter of #253 lost twelve days of off-machine memory that way. A deferred push never triggers it.

#### When a commit does not happen

`nothing to commit for <slug>, skip` used to cover two states as well: the store
really had nothing new, or the pathspec matched nothing git tracks. A Windows
install ran twelve days on the second while being told the first
([#263](https://github.com/Digital-Process-Tools/claude-remember/issues/263)) —
its slug differed from the tracked one by the case of the drive letter, NTFS is
case-insensitive so every layer above git was satisfied, and git's pathspecs are
case-sensitive so `git add` matched nothing.

| Log line | What it means | What to do |
| --- | --- | --- |
| `committed <slug>` | Memory is in the local store. | Nothing. |
| `nothing to commit for <slug>, skip` | The store has nothing new since the last backup. | Nothing. |
| `ERROR: this project's memory is tracked as '<other>/' but this session computed '<slug>/' …` | Git tracks this project's memory under a different spelling and cannot match the two. **No retry can fix this** — every save is being committed nowhere. | Rename the tracked directory, in two steps because a case-only rename is a no-op on a case-insensitive filesystem: `git -C ~/.remember mv -- '<other>' '<slug>.tmp' && git -C ~/.remember mv -- '<slug>.tmp' '<slug>'`, then commit. |

As with a rejected push, the rename is deliberately **not** done for you, and it
also carries a one-line `systemMessage` on the next prompt — the condition never
clears itself, so a log line alone is what let the original go unnoticed.

#### Restoring on a second machine (off by default)

Backup pushes. It does not pull. If you use the same store from more than one machine, the second machine reads its own stale memory, commits on top of it, and from then on cannot push at all — which is how the divergence above happens in the first place.

`git_restore.enabled` turns on the other direction. **It is off by default and nothing changes until you set it**:

```jsonc
// ~/.remember/config.json
{ "git_restore": { "enabled": true } }
```

With it on, each session start fast-forwards `~/.remember/` from the backup remote *before* memory is read into context, so the session sees what your other machine wrote.

**It only ever fast-forwards.** No merge, no rebase, no reset, no checkout, no stash — a test fails if any of those verbs ever appears in the hook. If the store has diverged (commits on both sides) it is **refused and reported**, for the same reason a rejected push is not auto-resolved: `recent.md` and `archive.md` are rewritten wholesale by consolidation, so a conflict there is real and a wrong resolution corrupts memory silently. After `git_restore.diverged_notice_after` session starts in that state (default 3) the refusal also reaches you as a `systemMessage`.

**No network runs before your first prompt.** The `git fetch` is detached and its result lands on the *next* session start; the fast-forward itself reads only refs an earlier session already fetched, and is purely local. Measured on the happy path, the whole hook costs **~26 ms** on top of process startup — against ~1.7 s for a warm `git fetch` to GitHub, which is what a synchronous version would have put in front of every prompt. The trade is that a change made on another machine arrives one session later than it could. A restore that lands one session late is still a restore; a session start that hangs waiting on a credential prompt is not.

Because that fetch is unattended, its outcome is recorded and reported: `could NOT check the remote` is a state of its own and is never rendered as "already up to date". The relevant lines in `~/.remember/<slug>/logs/`:

| Log line | What it means |
| --- | --- |
| `restored N commit(s) from …` | The other machine's memory is now on disk, and this session read it. |
| `already up to date with …` | Checked against a fetch that succeeded. Nothing to do. |
| `WARNING: the last background fetch FAILED …` / `… never completed` | **Could not check.** The comparison was against refs as old as the last fetch that did finish. Usually offline or missing credentials; run `git -C ~/.remember fetch` to see git's own error. |
| `ERROR: the memory store has DIVERGED …` | Commits on both sides. Nothing was restored and nothing will be merged or rebased for you. Resolve it by hand. |
| `store busy (backup in progress), skip` | A backup held the lock. Retried next session. |

The restore's state files need no `.gitignore` entry — like the backup's, they
live in `.git/remember/` rather than beside your memory. That is not only
tidiness: `git merge --ff-only` refuses when an untracked file would be
overwritten, so a state file at the store root is a name the restore collides
with the day a remote carries it, and the thing that breaks is the
fast-forward itself.

