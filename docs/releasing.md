# Releasing

`main` carries everything: the plugin, its 300 test files, the docs and their images, the
maintainer tooling, and a CHANGELOG.md over 600 KiB. The Anthropic plugin directory does not
accept that. Its pre-submission checklist holds any version whose plugin folder has more than
512 files, or a file of 256 KiB or more that is not an image or font. A `.gitattributes` with
`export-ignore`, `export-subst` or `filter` (any content-rewriting attribute, including in a
`.gitattributes` above the plugin folder) is worse than a hold: validation stops with "Couldn't
validate that repository". On v0.36.0 the directory fetched 474 files and 9.2 MB and then
reported "Validation ran out of time"
([#851](https://github.com/Digital-Process-Tools/claude-remember/issues/851)).

So there are two branches people install from:

| Branch | Who reads it | What decides the version they get |
| --- | --- | --- |
| `main` | the DPT marketplace (`dpt-plugins`), manual installs | `version` in `.claude-plugin/plugin.json` on `main` |
| `release` | the Anthropic directory, which has followed `release` since 2026-10-02 (see "Switching the listing to `release`" below) | the latest commit on `release`, which only the release workflow writes |

`release` is built by [`.github/workflows/release-branch.yml`](../.github/workflows/release-branch.yml)
from a tag. It never shares history with `main`: each release is one commit on top of the
previous release commit, and its message names the tag and the `main` commit it came from.

**The `publish` job refuses to push a tree whose `plugin.json` version is not strictly greater
than `release`'s current one** ([#856](https://github.com/Digital-Process-Tools/claude-remember/issues/856)):
pushing a patch tag for an older line (say `v0.36.2`) after `v0.37.0` is already on `release` would
otherwise land `0.36.2` on top of it, and every install pinned to `release` would see the version
drop. If you really do need to publish an older line on purpose, re-run the workflow via
`workflow_dispatch` with the `allow_version_regression` input set.

## The sequence

1. **Fold `changelog.d/` into a `## [x.y.z]` section of CHANGELOG.md and bump every version
   site**: `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, the README version badge,
   CHANGELOG.md (the list lives in `.oss.json`, `version_sites`).
   *Why:* DPT-marketplace installs follow `main`, and the `version` field in `plugin.json` is the
   only thing that tells them there is something new. The tag does not matter to them.

2. **Run the full suite (`pytest`) and commit the release on `main`.** In this repository that is
   a direct `chore(release): x.y.z` commit on `main`, not a pull request: v0.35.0 to v0.37.0 were
   all made that way, and `.oss.json` leaves `merge_method` unset (`null`).
   *Why:* that commit is what gets tagged; CI across three OSes is the gate.

3. **Tag that commit `vx.y.z` and push the tag with your own credentials**:
   `git tag vx.y.z <release-commit-sha> && git push origin vx.y.z`, then check it landed with
   `git ls-remote --tags origin vx.y.z`.
   *Why:* **pushing the tag is what publishes to the directory now.** The tag push starts the
   `release branch` workflow, and that workflow is the only thing that writes `release`. No tag,
   no directory update, and the tag is no longer only for the releases page.
   *Why your own credentials:* a tag pushed by another workflow with `GITHUB_TOKEN` does not start
   workflows (GitHub suppresses them to prevent loops). The oss release flow pushes the tag with
   `git push origin <tag>` from the maintainer's checkout, which does start it.

4. **Watch the `release branch` run** (Actions tab, or `gh run list --workflow release-branch.yml`).
   It has two jobs:
   - `verify`, read-only: installs PyYAML, builds the tree from the tag, runs
     [`check_release_tree.py`](../.github/scripts/check_release_tree.py) (the directory's
     pre-submission checklist), installs the pinned claude CLI (`cli_version` in
     [`.github/release-branch.json`](../.github/release-branch.json), 2.1.287 as of 2026-10-02 --
     moved out of the workflow's own env block in #866 so another repository reusing this tooling
     only edits config) and runs `claude plugin validate --strict`, then runs
     every hook in `hooks/hooks.json` once in an isolated temp HOME and project, with a fake
     `claude` that refuses every call
     ([`smoke_release_tree.py`](../.github/scripts/smoke_release_tree.py)).
     **If the npm install of the CLI fails, the run does not fail**: validate is SKIPPED and the
     only trace is a `::warning::` annotation, so read the run rather than its status. The pin
     must be 2.1.281 or later: earlier CLIs warn "Unknown field" on the directory listing fields
     in `plugin.json`, and `--strict` makes that a failure (observed 2026-10-02: 2.1.280 fails on
     our `plugin.json` with `privacyPolicyUrl` and `termsOfServiceUrl`, 2.1.287 passes);
   - `publish`, the only job allowed to write: rebuilds the same tree, refuses to push unless it
     is byte-for-byte the tree `verify` passed, and pushes one commit to `release`.
   *Why two jobs:* the smoke test runs the plugin's own hooks, so it never holds a token that can
   push.

5. **Publish the GitHub release** (`scripts/release_publish.py` from the oss plugin, not a file in
   this repository; it runs `gh release create --verify-tag`).
   *Why it is unaffected:* the release notes are read from `CHANGELOG.md` in the maintainer's
   local `main` checkout (the script's default is `<repo>/CHANGELOG.md`), never from the release
   tree. The release tree's CHANGELOG.md is cut to the latest section, but nothing reads that copy
   except people browsing `release`. Keep `--verify-tag`: without it `gh release create` would
   create a missing tag itself, through the API.

6. **The directory picks up the new `release` commit** (at once through the push webhook,
   otherwise within about 6 hours) and scans it. For v0.38.0 the webhook delivery got 200 OK and
   the version was in the **Versions** tab 3 to 4 minutes after the push. A version with a
   **Policy hold** waits for an Anthropic reviewer. And while the listing itself is flagged ("A
   scan flagged this plugin for review; newer versions won't go live until a reviewer clears it"),
   no newer version goes live, however clean, until a reviewer clears it: v0.38.0 was
   **Approved** with no hold and still waited (see "Needs the directory team" below). Read the
   **Versions** tab (below) rather than assuming a waiting version is only in a queue.

## What the release tree contains

[`build_release_tree.py`](../.github/scripts/build_release_tree.py) reads the tag straight from
git (`git ls-tree` and `git cat-file`; never the working tree, and never `git archive`, which
would need `export-ignore`). Then:

- **It drops the deny-list** in [`.github/release-branch.json`](../.github/release-branch.json):
  `tests/`, `docs/`, `.github/`, `.claude/`, `.oss/`, `changelog.d/`, `trap.d/`, `CLAUDE.md`,
  `CONTRIBUTING.md`, `conftest.py`, `pyproject.toml`, `.oss.json`, `.supertool.json` and the
  test-only scripts. It is a deny-list on purpose: a file nobody listed still ships, and the check
  catches it loudly if it is too big. With an allow-list, a forgotten runtime file would vanish
  from every user's install with no error anywhere. A new dev-only top-level file or directory
  therefore needs adding here.
- **It cuts CHANGELOG.md** to the latest released `## [x.y.z]` section, skipping `[Unreleased]`
  even when it has entries, plus that section's link and a link to the full file on `main`.
- **It rewrites links** in every shipped `.md` file that point at a removed path (the README's
  `docs/` links and its logo) to absolute URLs on `main`: `raw.githubusercontent.com` for images,
  `github.com/.../blob/main` for everything else. Links to files that still ship are left alone.

From v0.36.0 that gives 73 files and 1.3 MB, down from 474 files and 9.2 MB.

## When the workflow fails

Nothing is pushed. `release` stays on the previous release, so the directory keeps serving
that, and people on `main` are not affected at all. Fix the cause on `main`, then run the
workflow again for the same tag: **Actions, `release branch`, Run workflow, ref = `vx.y.z`**, or

```bash
gh workflow run release-branch.yml -f ref=vx.y.z
```

A manual run takes the workflow, the scripts and `.github/release-branch.json` from the branch
you run it on (normally `main`) and the plugin files from the ref you name. That is how a
tooling fix reaches a tag that is already cut. If the problem is in the plugin itself, it needs a
new patch release instead. Re-running the same tag when `release` already holds that exact tree
pushes nothing.

## Building and checking locally

```bash
python3 .github/scripts/build_release_tree.py --ref vX.Y.Z --out /tmp/release-tree   # the latest tag
python3 .github/scripts/check_release_tree.py /tmp/release-tree
python3 .github/scripts/smoke_release_tree.py /tmp/release-tree   # needs bash; validate needs `claude`
```

The scripts come from your checkout; the plugin files come from the tag. A tag cut before the
check was tightened can fail it: with #859's `allowed-tools` check in place, a tree built from
v0.36.0 or v0.37.0 fails on `commands/doctor.md`. That is expected, and re-dispatching such an
old tag through the workflow would fail the same way.

`check_release_tree.py` prints `REVIEW` lines for code that reads a credential (the OAuth token
and API key handling in `pipeline/haiku.py`, for example). Those never fail the check. Treat them
as a starting list for what README.md must disclose, not as a prediction of what the portal will
flag: on `e6cf58f` the portal's credential warning named `README.md`,
`scripts/session-start-hook.sh` and `.claude-plugin/plugin.json`, and none of those is a `REVIEW`
line (the check skips `.md` files for this).

## How the directory decides what to read

The directory never looks at tags. It follows **one branch or tag**, set in the developer portal
(claude.ai/directory/manage, the plugin's page, **Settings, Source, "Tracked branch or tag"**).
Left empty, that field follows the repository's default branch, which is why every merge to
`main` used to show up in the portal's **Versions** tab as a new version to check. The version
label the portal shows next to each commit appears to come from `version` in that commit's
`plugin.json`, not from a tag (several different commits were all labelled `v0.34.0`).

Since the field says `release` (2026-10-02):

- merges to `main` are invisible to the directory;
- the tag is never seen by Anthropic either. It only starts our workflow;
- the directory sees one new commit on `release` per release, and scans it.

**Claude Code updates an installed plugin only when `version` in `plugin.json` changes.** The
updater compares manifests, not commits ([#133](https://github.com/Digital-Process-Tools/claude-remember/issues/133)).
A commit with an unchanged version reaches new installs only, never existing ones. The release
flow bumps the version before it tags, so a normal release is an update installed copies pick up.
Nothing enforces that, though: a manual re-run for a tooling fix (see "When the workflow fails")
can write a new `release` commit carrying the same version, and existing installs never receive
it.

## Switching the listing to `release` (done 2026-10-02)

For this repository this is done. `release` was created by the v0.37.0 tag push (run
37004187570, event `push`), the listing was switched to it on 2026-10-02, and its first scan is
`e6cf58f`.

For another repository, in this order:

1. Create `release` first: push a tag, or `gh workflow run release-branch.yml -f ref=<tag>`.
2. Check on GitHub that `release` holds only the slim tree.
3. In the portal field above (**Settings → Source → "Tracked branch or tag"**), type `release` and
   **Save**.

The portal's text on saving, verbatim: "Saving a change scans the latest commit on the new branch
or tag as a new version. A version that is already live stays up while that happens, and a
publish request that is still waiting is cancelled." Per the submission docs, the branch cannot be
changed while the plugin is with a reviewer. Until the switch, the listing keeps following the
default branch, and every push to it lands in the portal as a new version.

While you are on that page, set up the **GitHub push webhook** (**Settings → Updates → Set up**;
connected for this repository). Portal text: "With a push webhook, GitHub tells the directory the
moment you push. With or without one, a scheduled check looks for new commits about every 6
hours." Setting it up needs admin rights on the repository, and the portal shows the webhook
secret only once. The other Settings fields are covered under "Listing details" below.

## Our own marketplace

The DPT marketplace (`Digital-Process-Tools/claude-marketplace`) declares this plugin as
`{"source": "github", "repo": "Digital-Process-Tools/claude-remember"}`: no ref, so it installs
`main` as it is at that moment. New installs therefore get unreleased merges under the last
released version number, while existing installs only move at a version bump. Pointing that entry
at `release` would make both marketplaces serve the same tagged builds, and both preconditions now
hold: `release` exists, and Claude Code's `github` source accepts `ref` (a branch or tag) and `sha`
(code.claude.com, "Host and maintain a marketplace"). **Switching the entry in
Digital-Process-Tools/claude-marketplace to `"ref": "release"` is a pending follow-up, not done**
(as of 2026-10-02).

## What the directory actually checks: observed scans

The [pre-submission checklist](https://claude.com/docs/plugins/pre-submission-checklist) is not
the whole story: the portal raised at least one hold (`ALLOWED_TOOLS_BROAD`) that the checklist does
not list. What follows is what the portal reported on this plugin, **as observed on 2026-10-02**
(v0.37.0, then v0.38.0). Rules changed once already (around Sep 24), so treat this as a dated
record: re-read the **Versions** tab after every release instead of assuming it still holds.

### Reading a scan

Developer portal → the plugin → **Versions** → expand a version (the `>` chevron). Each version
shows Received → Queue → Fetch → Validation → **Directory policy** → Security scan → Review →
Publish. The **Directory policy** step lists every finding, with a title, a code (for example
`ALLOWED_TOOLS_BROAD`) and the file that triggered it. Two kinds matter:

- **Policy hold** (clock icon): the version cannot go live until an Anthropic reviewer clears it.
- **Warning** (triangle icon): shown to the reviewer; does not hold the version by itself.

A **Blocks** finding (red cross) is worse than a hold: fix it before anything else.

Separately from any version, a listing can be marked **"Needs the directory team"** (a flagged
version or a delist). Before v0.38.0 this section said, *inferred from the toast's wording*, that
the mark does not clear by itself when a clean version arrives. **v0.38.0 confirmed it (observed
2026-10-02):** the version had no policy hold, its status badge read **Approved** with a
**Publish update** button, and its Review step said "No reviewer action recorded for this
version". Its Publish step still said "Waits until a reviewer clears the hold on this plugin.
This plugin's versions wait for an Anthropic reviewer. Your Publish click is recorded as a
request for the reviewer." Clicking **Publish update** showed the toast "Needs the directory
team. This listing is held for the directory team (a flagged version, or a delist they applied).
Contact the directory team to publish or relist it."

So approving a version and holding the plugin are two separate things: an Approved version does
not go live while the plugin carries a reviewer hold, and only the directory team clears that.
Contact them as described under "Contacting Anthropic about a plugin" below, and say what changed
(for us: the listing now follows `release`, and the holds went from 1 to 0).

### Full tree (`main`) vs `release`, and the release after

The first two columns are v0.37.0: `81ffecb` was scanned while the listing still followed `main`,
`e6cf58f` after it was switched to `release`. The third is v0.38.0: `e3cfd5b` is the `release`
commit built from tag v0.38.0 (`4cda472` on `main`), the first release carrying #859's fixes.

| Code (portal title) | Full tree `81ffecb` (486 files, 9.3 MB) | `release` `e6cf58f` (73 files, 1.3 MB) | v0.38.0 `release` `e3cfd5b` (73 files, 1.3 MB) |
| --- | --- | --- | --- |
| `SECRET_IN_SCRIPT` (Secret in a shipped file) | **Blocks**: fake API keys in `tests/test_haiku.py` | gone: `tests/` is not shipped | gone |
| `ALLOWED_TOOLS_BROAD` (Pre-approves broad shell access in allowed-tools) | Policy hold: `commands/doctor.md` | **Policy hold**, the only one: same file | **gone**: observed cleared by #859's scoped `allowed-tools` |
| Files or downloads the validator couldn't inspect | Policy hold (2) | gone | gone |
| Image or font file that the plugin's code could run | Warning (3): the PNGs under `docs/` | gone | gone |
| `MCP_FORWARDS_CREDENTIAL_ENV` (Uses a credential from the user's machine) | Warning (21) | Warning (3): `README.md`, `scripts/session-start-hook.sh`, `.claude-plugin/plugin.json` | Warning (3), unchanged |
| Unrecognized field in plugin.json | not raised | not raised | **Warning (4), new**: fields not yet read from the portal (see the rule below) |
| `RUNTIME_FETCH_EXEC` (Contains a download-and-run command) | Warning (6) | Warning (2): `pipeline/shell.py`, `scripts/log.sh` | **Warning (3)**, up from 2; files not yet read from the portal |
| CLAUDE.md at the plugin root isn't loaded | Warning | gone | gone |
| `ICON_MISSING` (No icon) | Warning | Warning | Warning |
| `USES_HOOKS` (Uses hooks) | Info | Info: permanent for a hooks plugin, "Nothing to do" | Info |
| Policy holds | see the rows above | 1 | **0** |
| Security scan | Passed | Passed | Passed, "took 3 min" |
| Directory policy verdict, status | not recorded | not recorded | "Meets directory policy, with warnings"; status **Approved** |

The rest of the v0.38.0 run, as the portal showed it: Received "Arrived by a push to the tracked
branch"; Fetch "Fetched and unpacked: 73 files, 1.3 MB"; Validation "Passed with warnings"; Review
"No reviewer action recorded for this version". It did not go live: the plugin-level hold
described under "Reading a scan" above still applies.

On the earlier v0.36.0 (`a92a072`, full tree, 474 files / 9.2 MB) the validator never finished:
`VALIDATION_INCOMPLETE` and `VALIDATION_NOT_EVALUATED`, both policy holds; the portal's wording
was "Validation ran out of time". That is the failure the `release` branch exists to remove.

### The rules, one by one

- **`ALLOWED_TOOLS_BROAD`: not in the published checklist.** Portal wording: "A skill or command
  lists a broad shell entry in `allowed-tools`, such as bare `Bash`, `Bash(*)`, or a wildcard right
  after a shell, an interpreter, a package manager or runner, or `curl`, as in `Bash(python3:*)`."
  Accepted form, also quoted: "For the plugin's own script, name the file and keep the braces:
  `Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/run.py:*)`. A relative path or a wildcard in the path
  is still held." Ours was `allowed-tools: Bash` in `commands/doctor.md`; the skill already used the
  accepted form. `check_release_tree.py` now fails on the broad forms (#859). **Observed cleared
  on v0.38.0 (`e3cfd5b`)** with `allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh:*)`
  and the script run directly (no `bash` prefix, `doctor.sh` committed as mode 100755): a named
  plugin script with no interpreter in front is accepted too.
- **`SECRET_IN_SCRIPT`** blocks on anything that looks like a literal credential, including fake
  keys in test fixtures. A deny-list that drops `tests/` removes it. If a test fixture must ship,
  build the key at runtime instead of writing it literally. **Fixed in #866**: our Anthropic-key
  pattern used to need 20 or more characters after `sk-ant-`, so the fixture
  `sk-ant-api03-example` in `tests/test_haiku.py` did not match it and only the deny-list kept it
  out of `release`; `check_release_tree.py` now matches any `sk-ant-` prefix, so the fixture is
  caught even if a future change ever drops the deny-list entry.
- **`MCP_FORWARDS_CREDENTIAL_ENV`** fires where the plugin reads a credential from the user's
  machine. The checklist puts this row under "Held for a reviewer", but on our scans the portal
  showed it as a warning; we do not know which one wins. It also fired on `README.md`, i.e. on the
  disclosure itself, on a comment that only contains the word "credentials", and on
  `.claude-plugin/plugin.json`, which contains no credential text at all. (**Fixed in #866**:
  `check_release_tree.py`'s own `REVIEW` line used to skip every `.md` file, so README.md's own
  match was never named in our own output even though the portal flagged it; it now scans `.md`
  too, REVIEW-only, same as every other text file.) The portal's own text:
  "If the credential is for that host's own vendor, you can leave it as it is and a reviewer
  confirms that." Ours is the user's own Claude Code / Codex login, so we left it -- but
  [#860](https://github.com/Digital-Process-Tools/claude-remember/issues/860) was reopened on
  2026-10-03 because the checklist still classes `MCP_FORWARDS_CREDENTIAL_ENV` as "Held for a
  reviewer", not a warning the maintainer can just confirm away forever, and the reopening's own
  rule is that the directory report should be as green as possible. Outcome, built in #860:
  - **Moved**: the optional recovery token (`REMEMBER_OAUTH_TOKEN` / `haiku.oauth_token`) is now
    primarily a `userConfig` option (`oauth_token`, `sensitive: true`) in `plugin.json`. Verified
    with a real `claude plugin validate .` run (not just reasoned from docs) that the schema is
    valid; the hook-env mechanism itself (`CLAUDE_PLUGIN_OPTION_OAUTH_TOKEN`, per the
    code.claude.com plugin manifest reference's "Reference a saved value" / "Fields that run
    through a shell" sections) is reasoned from docs, not independently observed against a live
    install reading it, since that needs a real `/plugin` configure round-trip this environment
    cannot drive. `REMEMBER_OAUTH_TOKEN` / `haiku.oauth_token` keep working (migration: no existing
    install loses its recovery token silently) but now log a `DEPRECATED:` line every time either
    is actually used, which `/remember:doctor` surfaces (#860).
  - **Not moved, and not movable**: `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` and
    `CODEX_API_KEY` -- the host CLI's own credentials, read straight from the environment -- stay
    exactly as they were. They are not ours to move into `userConfig`: they belong to the user's
    existing Claude Code / Codex login, and the default, nothing-configured path must keep working
    through them. This is the #869 lane's own, still-correct finding: the finding's own condition
    (reads a credential from the user's machine) keeps holding on `README.md`'s disclosure and on
    `scripts/session-start-hook.sh` (the `SessionStart` hook that triggers the pipeline which
    forwards `CLAUDE_CODE_OAUTH_TOKEN`) for exactly this reason, and the portal's own text already
    accepts this case as it stands ("If the credential is for that host's own vendor, you can
    leave it as it is and a reviewer confirms that").
  - **`.claude-plugin/plugin.json`** -- the anomaly #869 could not explain (it had no credential
    text at all) -- now declares the `userConfig` entry the checklist asks for, which may well be
    what clears *that one* file's match; unconfirmed, since reading the portal's own per-file
    verdict needs a real directory scan this environment cannot drive either.
  - Codex (`.codex-plugin`) has no `userConfig` mechanism, so its recovery path is unchanged:
    still `REMEMBER_OAUTH_TOKEN` / `haiku.oauth_token`, and still not deprecated there -- the
    deprecation warning in `pipeline/haiku.py` fires regardless of which host called it, but the
    doctor notice and the README's migration note both say Codex has no replacement to move to.
  Do not make the warning disappear by removing the disclosure: the security scan holds
  undisclosed behaviour. #869's prose above is kept as the record of what was tried and argued
  first; #860 reopened specifically because that argument, while correct about the host-vendor
  credentials, was being used to justify shipping the one part of the pattern (the plugin's own
  recovery token) that genuinely was ours to move.
- **`RUNTIME_FETCH_EXEC`** flags text that downloads and runs code, and the portal says it looks at
  "a hook, a server or settings command, a script, or text such as a skill or README". On v0.37.0
  (`e6cf58f`) it named `pipeline/shell.py` and `scripts/log.sh`, which contain no download at all.
  We inferred that it matched comment text (a docstring showing `eval "$(...)"` and a comment
  mentioning curl), and #859 reworded those two comments expecting the warning to go. **That
  inference was wrong, or at best is unconfirmed: rewording the comments did not clear it.** On
  v0.38.0 (`e3cfd5b`) the count went *up*, from 2 to 3, and which third file matched is **still
  unknown**: the file/line detail only shows behind the expanded row in the portal's web UI, which
  neither this write-up nor #864's follow-up investigation had access to.

  **#864's source-only follow-up** (no portal access, so this narrows the guess rather than
  confirming it): the v0.37.0→v0.38.0 shipped-tree diff touches five code files besides the two
  already flagged -- `commands/doctor.md` (2 insertions/2 deletions, #859's `allowed-tools` fix),
  `scripts/doctor.sh` (mode bit only, no content change), `scripts/lib-memory-context.sh`
  (162 insertions/2 deletions, `git diff --stat` churn 164, #842/#845), `scripts/session-start-hook.sh`
  (63 insertions/11 deletions, `git diff --stat` churn 74, #842/#845) and `config.example.json`
  (28 insertions, likely denied from `release`, not checked). Grepping only the **added** (`+`)
  lines of the two largest diffs -- `git diff v0.37.0 v0.38.0 -- scripts/lib-memory-context.sh
  scripts/session-start-hook.sh | grep -E '^\+' | grep -iE '\b(curl|wget|download|fetch|eval|exec)\b'`
  -- found **nothing** in either, so the earlier guess that #842/#845's SessionStart budget code is
  the new third match has no textual support in the lines that version actually added -- it should
  not be assumed true just because it is the largest diff. (Grepping the *whole file as shipped*
  rather than just the diff gives a different, misleading answer: `scripts/session-start-hook.sh`
  contains `exec` nine times, e.g. `exec 3>&1`, but every one of those pre-dates v0.38.0 and is
  already present verbatim in v0.37.0's copy of the file, so it cannot explain a count that only
  rose between those two versions.) `scripts/log.sh` is the one file here with **real** `eval` calls
  on validated input (`eval "$name=$value"`, `eval "$_assign"`), not just the word in a comment --
  the strongest literal candidate for why it, specifically, keeps matching even after #859's
  reword, but this is still a guess, not a confirmed trigger.

  **#864's actual fix** (same issue, second lane, no portal access either): removed both real
  `eval` calls from `scripts/log.sh`'s config-flatten cache loader. Rather than keep `%q` as
  the on-disk format and hand-decode it without `eval` (the first version of this fix, dropped
  mid-review as a fragile byte-by-byte bash loop for no gain over `eval` -- this cache's
  publisher and loader are both owned by this same file, so there was no reason to keep %q's
  shape at all), the format changed to a trivial one this file fully controls: one `NAME` TAB
  `VALUE` record per line, escaping only backslash/newline/tab, decoded with a single
  `printf -v NAME '%b' VALUE` call -- a pure byte-level format directive, never a re-parse of
  the text as shell source. The cache's on-disk path was bumped
  (`remember-config-cache-v2-...`) so an older build's `%q`-based cache is simply never opened.
  Renamed `safe_eval` (in `scripts/log.sh` and every caller) to `assign_kv`, since the old
  name's own text contained the substring `eval` regardless of what its body called. Reworded
  `CHANGELOG.md`'s v0.38.0 entry to drop the literal `curl`/`wget`/`eval "$(...)"` text. After
  this fix, `grep -rniE 'eval|curl|wget'` finds nothing in `pipeline/shell.py`,
  `scripts/log.sh`, or the `CHANGELOG.md` text the slim `release` branch actually ships (the
  latest released section only -- `build_release_tree.py` trims the rest). Whether this
  reaches 0 `RUNTIME_FETCH_EXEC` findings, rather than a lower but nonzero count, is
  unconfirmed without the next portal scan: the matcher's exact mechanism (literal `eval`
  token? command substitution shape? something else?) was never confirmed by either #859's or
  this lane's source-only analysis.

  **Still needs a human with portal access**: expand the "Contains a download-and-run command" row
  under Versions → v0.38.0 (`e3cfd5b`) → Directory policy, and record the file(s) and line(s) shown
  there; update this entry once that is known. Until then, do not spend more effort guessing or
  rewording comments for this row -- #859 already tried that once and the count went up, not down.
  It is a warning, and the portal's own text says "Where it only appears in documentation, nothing
  needs to change".

  **#866** added an automated, REVIEW-only signal for the two shapes #864/#875 actually learned
  to watch for -- `eval` fed by a command substitution (`eval "$(...)"`) and a downloader piped
  straight into a shell (`curl ... | sh`) -- across every shipped text file, not just
  hooks/scripts. This is our own heuristic, not a reimplementation of the portal's matcher (still
  unconfirmed, per the paragraph above): it exists so a REVIEW line names the shape before the
  next portal scan does, not to predict what the portal will find.

  **Observed, 2026-10-02 (#872):** the portal's "Contains a download-and-run command" row **names
  files, not lines** -- on v0.38.0 (`e3cfd5b`) it named `CHANGELOG.md`, `pipeline/shell.py` and
  `scripts/log.sh`, with no line number for any of the three, and all three were false positives
  (#864's closing comment). So a file named in this row is not itself evidence of a real
  download-and-run shape; the file still has to be read to tell.
- **Unrecognized field in `plugin.json`** (warning, 4 findings, new on v0.38.0). Portal text: "If
  you expected the field to do something, check its spelling against the plugins reference.
  Otherwise it can stay." *Inferred, not yet confirmed from the expanded row:* the four are
  `documentationUrl`, `supportUrl`, `privacyPolicyUrl` and `termsOfServiceUrl`, the only top-level
  keys v0.38.0 added to `plugin.json` (#859, #861). That contradicts the code.claude.com plugin
  manifest reference, which says the directory reads exactly these fields for the listing (see
  "Listing details" below). We keep them: the warning itself says they can stay, and the
  reference says they feed the listing. The question is with the directory team.
  **Observed, 2026-10-02 (#872):** the expanded row settles this. For each of
  `documentationUrl`, `privacyPolicyUrl`, `supportUrl` and `termsOfServiceUrl` the portal now
  says "The plugin directory reads '<field>' for the listing; Claude Code itself ignores it at
  load time. No action needed." -- the four findings above are these four fields, confirmed, not
  just inferred, and the right action is still to keep them.
- **`ICON_MISSING`**: the portal accepts a square PNG **or JPEG**, 512 to 2048 px, under 2 MB,
  through `icon` in `plugin.json` (SVG and WebP are not accepted). Keep it inside the plugin
  folder and outside any denied path: an icon under `docs/` vanishes from `release`. **Observed,
  2026-10-02 (#872): the listing icon is set only once.** The exact remedy text: "That file may
  become the listing icon only once: the first time the plugin is saved or submitted in the
  developer portal. Adding or changing it later does not change the icon." remember was saved
  before it shipped an icon, and its portal **Details** page shows the icon as "Set by an
  Anthropic reviewer" (Approved). Shipping one now would only clear the `ICON_MISSING` warning;
  it would not change the listing icon. **The lesson for a new plugin: ship the icon before the
  first portal submission** (see #865). For another repository: put the icon in the plugin
  folder **before** the first save or submit.
- **`USES_HOOKS`** is information only and stays for any plugin that ships hooks.

### For another plugin repository, in addition to the reuse steps below

1. Read your own **Versions** tab first: the full-tree scan of your latest commit already lists
   every rule your plugin trips, before you build anything. A plugin never submitted has no
   Versions tab: use **Submit new → Plugin bundle → Validate** in the portal for the same report.
2. Grep every shipped skill, command and agent for `allowed-tools`. A bare `Bash`, `Bash(*)`, or
   a wildcard after a shell, interpreter, package manager or runner, or `curl` is a hold. A
   specific command (`Bash(git status:*)`) or a named `${CLAUDE_PLUGIN_ROOT}` script is not.
   `check_release_tree.py` checks a space-delimited `allowed-tools` string the same as a
   comma-delimited one (#866, trap.d/859); a `..`-escaped path past `${CLAUDE_PLUGIN_ROOT}` or a
   wrapper command (`env bash ...`, `sudo curl ...`) in front of an otherwise-scoped entry is
   **not yet** covered -- read it by hand until that lands.
3. Make sure fake credentials live only in denied paths (`tests/`).
4. Put the icon in the plugin folder before the first save or submission (see `ICON_MISSING`).
5. Disclose in README.md everything the plugin runs, sends and stores; expect that disclosure to
   raise warnings, and leave it in.
6. If the plugin itself was ever flagged, a clean, **Approved** version still needs the directory
   team: it waits for them however clean it is (observed on v0.38.0, see "Reading a scan").
   Contact them as soon as the first clean version is Approved, with the before and after
   numbers (ours: policy holds 1 to 0, security scan passed, status Approved).

## Listing details: what comes from where

Sourced on 2026-10-02.

- **The listing fields in `plugin.json`.** Per the code.claude.com plugin manifest reference, the
  directory reads `icon`, `documentationUrl`, `supportUrl`, `privacyPolicyUrl` and
  `termsOfServiceUrl` from `plugin.json`, and Claude Code itself ignores them. The four URLs must
  be `https`. `claude plugin validate` accepts them without a warning only from 2.1.281, hence the
  workflow's pin. #859 added the four URLs to our `plugin.json`, shipped in v0.38.0. On that
  version's scan the portal raised "Unrecognized field in plugin.json" (4 findings), almost
  certainly these four (*inferred*; see the rule above). We keep them.
- **Name and short description.** From claude.com, "Manage your listing after publishing": "A
  plugin listing's name and short description come from plugin.json and the README of the version
  that's live. To change them, edit those files and publish a new version… If an Anthropic
  reviewer edited either field during review, the listing keeps the reviewer's text." The
  **Settings** tab changes the surfaces, the contact email and the "collects or transmits user
  data" answer.
- **Observed for remember:** the **Details** page showed Short description "No value" and
  Keywords "No value", although `plugin.json` has carried `description` and `keywords` since
  March, before the May 7 submission and in the live v0.29.1. So the portal does not fill
  Keywords from `plugin.json`'s `keywords`, at least for this listing; why is unresolved, and the
  question is with the directory team. Homepage, Repository and License were filled.
- **"This plugin collects or transmits user data"** (Settings). The portal's hint names remote MCP
  servers and HTTP hooks. remember switched it on, pointing at [privacy.md](privacy.md), because it
  sends conversation text to the user's own `claude -p` / codex; that is our judgement, not a rule.
  Whatever you choose, README.md has to describe what the plugin sends and where: the security
  scan holds behaviour the README does not disclose.

### Contacting Anthropic about a plugin

Per claude.com, "Track your directory submission": open the plugin's menu in the portal and choose
**Get help** or **Contact Anthropic**, or email `directory@anthropic.com` ("replies can be
delayed"). Include the listing name, the organisation and the status the portal shows. The
portal's **Contact Anthropic** opens a mail to `directory@anthropic.com` with the subject "Plugin
submission".

For remember, the maintainer sent that mail on 2026-10-02, after the v0.38.0 scan, asking the
directory team to clear the plugin-level hold. No reply had arrived as of writing.

## Reusing this in another plugin repository

claude-jit-context and claude-supertool hit the same directory holds and plan to reuse this. Not
everything repository-specific lives in `.github/release-branch.json`:

- `build_release_tree.py` reads `repo`, `default_branch`, `deny`, `changelog`, `rewrite_links` and
  the optional `link_ref` from it; `check_release_tree.py` reads only `budget`. In `budget`,
  `max_file_bytes` and `max_files` are the directory's rules, but `max_total_bytes` (3 MiB) is this
  repository's own ceiling, not a directory rule: set your own.
- `smoke_release_tree.py` reads its hook list from `hooks/hooks.json`. The env-var names it hands
  the hooks for the fake `claude`/`codex` binaries, the prefixes it drops from the smoke
  environment, and the binaries it fakes all come from `.github/release-branch.json`'s `smoke`
  block (`bin_env_vars`, `drop_env_prefixes`, `fake_bins`) -- edit that block, not the script
  (#866). `hook-errors.log` is still read by name, not from config.
- `release-branch.yml` reads the pinned `claude` CLI version from `.github/release-branch.json`'s
  `cli_version`, not from a workflow env entry -- edit that, and keep it at 2.1.281 or later (the
  floor is documented next to it in `_cli_version_why`) (#866).
- Rewritten links point at the head of the default branch (or `link_ref`), not at the tag, so a
  released README's links into removed paths can drift from the version that shipped.

To adopt it:

1. Copy `.github/scripts/{build,check,smoke}_release_tree.py`, `.github/release-branch.json` and
   `.github/workflows/release-branch.yml`, and adapt the points above.
2. Rewrite the deny-list from that repository's own tree. Check every candidate against what the
   plugin loads at runtime (hooks, scripts they call, skills, commands, manifests) before denying
   it, and keep `LICENSE` and `README.md`: the directory blocks without them.
3. Build from the latest tag and run the check and the smoke test locally (section above). Use
   the `REVIEW` lines as a starting list for the README disclosure, then compare with what the
   portal actually flags.
4. Port the tests (`tests/test_release_branch_*_851.py`) and adjust their fixtures.
5. Make sure `github-actions[bot]` can push `release` (no ruleset or branch protection blocking
   it), never make `release` the default branch, and treat push rights on `v*` tags as publish
   rights: whoever can push such a tag publishes to the directory.
6. Push a tag, confirm `release` exists, then do the portal steps in "Switching the listing to
   `release`" above.

Known limits of the shared scripts: the CHANGELOG cut assumes Keep a Changelog `## [x.y.z]`
headings; the smoke test builds payloads only for the common hook events; and the launcher,
credential and image-reference checks are pattern-based, so a reviewer may still see something
they miss.
