# Releasing

`main` carries everything: the plugin, its 300 test files, the docs and their images, the
maintainer tooling, and a CHANGELOG.md over 600 KiB. The Anthropic plugin directory does not
accept that. It holds any version whose plugin folder has a non-image file of 256 KiB or more,
more than 512 files, or a `.gitattributes` with `export-ignore`, `export-subst` or `filter`.
On v0.36.0 it fetched 474 files and 9.2 MB and then reported "Validation ran out of time"
([#851](https://github.com/Digital-Process-Tools/claude-remember/issues/851)).

So there are two branches people install from:

| Branch | Who reads it | What decides the version they get |
| --- | --- | --- |
| `main` | the DPT marketplace (`dpt-plugins`), manual installs | `version` in `.claude-plugin/plugin.json` on `main` |
| `release` | the Anthropic directory, **once its listing is switched to follow `release`** (see the last section) | the latest commit on `release`, which only the release workflow writes |

`release` is built by [`.github/workflows/release-branch.yml`](../.github/workflows/release-branch.yml)
from a tag. It never shares history with `main`: each release is one commit on top of the
previous release commit, and its message names the tag and the `main` commit it came from.

## The sequence

1. **Fold `changelog.d/` into a `## [x.y.z]` section of CHANGELOG.md and bump every version
   site**: `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, the README version badge,
   CHANGELOG.md (the list lives in `.oss.json`, `version_sites`).
   *Why:* DPT-marketplace installs follow `main`, and the `version` field in `plugin.json` is the
   only thing that tells them there is something new. The tag does not matter to them.

2. **Run the full suite (`pytest`), open the pull request, squash-merge it on green.**
   *Why:* the merge commit is what gets tagged; CI across three OSes is the gate.

3. **Tag that merge commit `vx.y.z` and push the tag with your own credentials**:
   `git tag vx.y.z <merge-sha> && git push origin vx.y.z`, then check it landed with
   `git ls-remote --tags origin vx.y.z`.
   *Why:* **pushing the tag is what publishes to the directory now.** The tag push starts the
   `release branch` workflow, and that workflow is the only thing that writes `release`. No tag,
   no directory update, and the tag is no longer only for the releases page.
   *Why your own credentials:* a tag pushed by another workflow with `GITHUB_TOKEN` does not start
   workflows (GitHub suppresses them to prevent loops). The oss release flow pushes the tag with
   `git push origin <tag>` from the maintainer's checkout, which does start it.

4. **Watch the `release branch` run** (Actions tab, or `gh run list --workflow release-branch.yml`).
   It has two jobs:
   - `verify`, read-only: builds the tree from the tag, runs
     [`check_release_tree.py`](../.github/scripts/check_release_tree.py) (the directory's
     pre-submission checklist), installs the claude CLI and runs
     `claude plugin validate --strict`, then runs every hook in `hooks/hooks.json` once in an
     isolated temp HOME and project, with a fake `claude` that refuses every call
     ([`smoke_release_tree.py`](../.github/scripts/smoke_release_tree.py));
   - `publish`, the only job allowed to write: rebuilds the same tree, refuses to push unless it
     is byte-for-byte the tree `verify` passed, and pushes one commit to `release`.
   *Why two jobs:* the smoke test runs the plugin's own hooks, so it never holds a token that can
   push.

5. **Publish the GitHub release** (`release_publish.py`, which runs `gh release create --verify-tag`).
   *Why it is unaffected:* the release notes are read from `CHANGELOG.md` in the maintainer's
   local `main` checkout (the script's default is `<repo>/CHANGELOG.md`), never from the release
   tree. The release tree's CHANGELOG.md is cut to the latest section, but nothing reads that copy
   except people browsing `release`. Keep `--verify-tag`: without it `gh release create` would
   create a missing tag itself, through the API.

6. **The directory picks up the new `release` commit** and scans it. A version can still sit
   "In review" for a while; that is the directory's queue, not a failed release.

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
python3 .github/scripts/build_release_tree.py --ref v0.36.0 --out /tmp/release-tree
python3 .github/scripts/check_release_tree.py /tmp/release-tree
python3 .github/scripts/smoke_release_tree.py /tmp/release-tree   # needs bash; validate needs `claude`
```

`check_release_tree.py` prints `REVIEW` lines for code that reads a credential (the OAuth token
and API key handling in `pipeline/haiku.py`, for example). Those never fail the check; they are
what a directory reviewer will look at, and README.md should disclose them.

## How the directory decides what to read

The directory never looks at tags. It follows **one branch or tag**, set in the developer portal
(claude.ai/directory/manage, the plugin's page, **Settings, Source, "Tracked branch or tag"**).
Left empty, that field follows the repository's default branch, which is why every merge to
`main` used to show up in the portal's **Versions** tab as a new version to check. The version
label the portal shows next to each commit appears to come from `version` in that commit's
`plugin.json`, not from a tag (several different commits were all labelled `v0.34.0`).

Once the field says `release`:

- merges to `main` are invisible to the directory;
- the tag is never seen by Anthropic either. It only starts our workflow;
- the directory sees one new commit on `release` per release, and scans it.

**Claude Code updates an installed plugin only when `version` in `plugin.json` changes.** The
updater compares manifests, not commits ([#133](https://github.com/Digital-Process-Tools/claude-remember/issues/133)).
A commit with an unchanged version reaches new installs only, never existing ones. `release`
moves only at a tag, and a tag always comes with a version bump, so every `release` commit is an
update that installed copies will pick up.

## One manual step, done once

Switch the listing to follow `release`, in the portal field above. Do it **after** the first
`release` commit exists: run the workflow once (`gh workflow run release-branch.yml -f ref=<latest tag>`),
check on GitHub that `release` holds only the slim tree, then type `release` and save. Until the
switch, the listing keeps following `main`, and every push to `main` keeps landing there as a held
version.

What saving does, per the portal's own text: it scans the latest commit on `release` as a new
version straight away; the version already live stays live while that runs; and a publish request
still waiting is cancelled.

While you are on that page:

- **GitHub push webhook** (Settings, Updates). Without it the portal checks for new commits about
  every 6 hours; with it, it sees a `release` push immediately.
- **"This plugin collects or transmits user data."** The portal's hint asks plugins that call
  remote MCP servers or HTTP hooks to say yes. Whatever you choose, README.md has to describe what
  the plugin sends and where, because the security scan holds behaviour the README does not
  disclose.
- **A listing held for the directory team** ("Needs the directory team" in the portal) does not
  clear itself when a clean version arrives. Contact the directory team from the plugin's contact
  address and say the listing now follows `release`.

## Our own marketplace

The DPT marketplace (`Digital-Process-Tools/claude-marketplace`) declares this plugin as
`{"source": "github", "repo": "Digital-Process-Tools/claude-remember"}`: no ref, so it installs
`main` as it is at that moment. New installs therefore get unreleased merges under the last
released version number, while existing installs only move at a version bump. Pointing that entry
at `release` would make both marketplaces serve the same tagged builds; do it only after the first
`release` commit exists, and only once Claude Code's `github` source is confirmed to accept a
`ref`.

## What the directory actually checks: observed scans

The [pre-submission checklist](https://claude.com/docs/plugins/pre-submission-checklist) is not
the whole story: the portal raised at least one hold (`ALLOWED_TOOLS_BROAD`) that the checklist does
not list. What follows is what the portal reported on this plugin, **as observed on 2026-10-02**
(v0.37.0). Rules changed once already (around Sep 24), so treat this as a dated record: re-read the
**Versions** tab after every release instead of assuming it still holds.

### Reading a scan

Developer portal → the plugin → **Versions** → expand a version (the `>` chevron). Each version
shows Received → Queue → Fetch → Validation → **Directory policy** → Security scan → Review →
Publish. The **Directory policy** step lists every finding, with a title, a code (for example
`ALLOWED_TOOLS_BROAD`) and the file that triggered it. Two kinds matter:

- **Policy hold** (clock icon): the version cannot go live until an Anthropic reviewer clears it.
- **Warning** (triangle icon): shown to the reviewer; does not hold the version by itself.

A **Blocks** finding (red cross) is worse than a hold: fix it before anything else.

Separately from any version, a listing can be marked **"Needs the directory team"** (a flagged
version or a delist). That does not clear when a clean version arrives: contact the directory team
from the plugin's contact address.

### Full tree (`main`) vs `release`, same release

Both rows are v0.37.0. `81ffecb` was scanned while the listing still followed `main`; `e6cf58f`
after it was switched to `release`.

| Code (portal title) | Full tree `81ffecb` (486 files, 9.3 MB) | `release` `e6cf58f` (73 files, 1.3 MB) |
| --- | --- | --- |
| `SECRET_IN_SCRIPT` (Secret in a shipped file) | **Blocks**: fake API keys in `tests/test_haiku.py` | gone: `tests/` is not shipped |
| `ALLOWED_TOOLS_BROAD` (Pre-approves broad shell access in allowed-tools) | Policy hold: `commands/doctor.md` | **Policy hold**, the only one: same file. Fixed in v0.37.1 (#859) |
| Files or downloads the validator couldn't inspect | Policy hold (2) | gone |
| Image or font file that the plugin's code could run | Warning (3): the PNGs under `docs/` | gone |
| `MCP_FORWARDS_CREDENTIAL_ENV` (Uses a credential from the user's machine) | Warning (21) | Warning (3): `README.md`, `scripts/session-start-hook.sh`, `.claude-plugin/plugin.json` |
| `RUNTIME_FETCH_EXEC` (Contains a download-and-run command) | Warning (6) | Warning (2): `pipeline/shell.py`, `scripts/log.sh` |
| CLAUDE.md at the plugin root isn't loaded | Warning | gone |
| `ICON_MISSING` (No icon) | Warning | Warning |
| `USES_HOOKS` (Uses hooks) | Info | Info: permanent for a hooks plugin, "Nothing to do" |
| Security scan | Passed | Passed |

On the earlier v0.36.0 (`a92a072`, full tree, 474 files / 9.2 MB) the validator never finished:
`VALIDATION_INCOMPLETE` and `VALIDATION_NOT_EVALUATED`, both policy holds ("usually because the
plugin folder is very large"). That is the failure the `release` branch exists to remove.

### The rules, one by one

- **`ALLOWED_TOOLS_BROAD`: not in the published checklist.** Portal wording: "A skill or command
  lists a broad shell entry in `allowed-tools`, such as bare `Bash`, `Bash(*)`, or a wildcard right
  after a shell, an interpreter, a package manager or runner, or `curl`, as in `Bash(python3:*)`."
  Accepted form, also quoted: "For the plugin's own script, name the file and keep the braces:
  `Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/run.py:*)`. A relative path or a wildcard in the path
  is still held." Ours was `allowed-tools: Bash` in `commands/doctor.md`; the skill already used the
  accepted form. `check_release_tree.py` now fails on the broad forms (#859).
- **`SECRET_IN_SCRIPT`** blocks on anything that looks like a literal credential, including fake
  keys in test fixtures. A deny-list that drops `tests/` removes it. If a test fixture must ship,
  build the key at runtime instead of writing it literally.
- **`MCP_FORWARDS_CREDENTIAL_ENV`** fires where the plugin reads a credential from the user's
  machine. It also fired on `README.md`, i.e. on the disclosure itself, and on a comment that only
  contains the word "credentials". The portal's own text: "If the credential is for that host's own
  vendor, you can leave it as it is and a reviewer confirms that." Ours is the user's own Claude
  Code / Codex login, so we left it. The fix that clears it is to ask for the value through a
  `userConfig` entry in `plugin.json` with `sensitive: true` and use `${user_config.KEY}`. Do not
  make it disappear by removing the disclosure: the security scan holds undisclosed behaviour.
- **`RUNTIME_FETCH_EXEC`** flags text that downloads and runs code, and the portal says it looks at
  "a hook, a server or settings command, a script, or text such as a skill or README". On our tree
  the two files contain no download at all; *inferred, not confirmed*: it matched comment text (a
  docstring showing `eval "$(...)"` and a comment mentioning curl), reworded in v0.37.1. Check the
  v0.37.1 scan before relying on this.
- **`ICON_MISSING`**: put a square PNG, 512 to 2048 px, under 2 MB, at `.claude-plugin/icon.png`
  (or set `icon` in `plugin.json`; SVG and WebP are not accepted). **It is set only once**: "the
  first time the plugin is saved or submitted… Adding or changing it later does not change the
  icon." Choose it deliberately, before it matters, and keep it outside any denied path: an icon
  under `docs/` vanishes from `release`.
- **`USES_HOOKS`** is information only and stays for any plugin that ships hooks.

### For another plugin repository, in addition to the reuse steps above

1. Read your own **Versions** tab first: the full-tree scan of your latest commit already lists
   every rule your plugin trips, before you build anything.
2. Grep every shipped skill, command and agent for `allowed-tools`. Anything broader than a named
   `${CLAUDE_PLUGIN_ROOT}` script is a hold.
3. Make sure fake credentials live only in denied paths (`tests/`).
4. Decide the icon before the first submission, and ship it inside the plugin folder.
5. Disclose in README.md everything the plugin runs, sends and stores; expect that disclosure to
   raise warnings, and leave it in.

## Reusing this in another plugin repository

claude-jit-context and claude-supertool hit the same directory holds and plan to reuse this.
The three scripts read everything repository-specific from `.github/release-branch.json`
(`repo`, `default_branch`, `deny`, `budget`, `changelog`, `rewrite_links`), and the smoke test
reads its hook list from `hooks/hooks.json`. To adopt it:

1. Copy `.github/scripts/{build,check,smoke}_release_tree.py`, `.github/release-branch.json` and
   `.github/workflows/release-branch.yml`.
2. Rewrite the deny-list from that repository's own tree. Check every candidate against what the
   plugin loads at runtime (hooks, scripts they call, skills, commands, manifests) before denying
   it, and keep `LICENSE` and `README.md`: the directory blocks without them.
3. Build from the latest tag and run the check and the smoke test locally (section above). Look
   at the `REVIEW` lines and make sure README.md discloses each of them.
4. Port the tests (`tests/test_release_branch_*_851.py`) and adjust their fixtures.
5. Push a tag, confirm `release` exists, then do the portal steps above.

Known limits of the shared scripts: the CHANGELOG cut assumes Keep a Changelog `## [x.y.z]`
headings; the smoke test builds payloads only for the common hook events; and the launcher,
credential and image-reference checks are pattern-based, so a reviewer may still see something
they miss.
