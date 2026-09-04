## Computing the slug outside bash

`~/.claude/projects/<slug>/` is where Claude Code writes session transcripts, and `<slug>` is a pure function of the project path. Anything driving this plugin from another language — PowerShell, Node, Python — eventually needs that name, and the only way to ask for it used to be sourcing `scripts/lib-slug.sh` in a subshell, once per tool call. That cost is exactly why the reporter of [#294](https://github.com/Digital-Process-Tools/claude-remember/issues/294) maintained a PowerShell port of the function — and maintaining that port is how #294 was found. A second implementation of this function disagrees **silently**: a slug that misses names a directory that does not exist, so the pipeline finds no transcript, exits 0, and saves nothing.

Three things exist so that nobody has to keep one.

### 1. Read the slug this session computed

`scripts/session-start-hook.sh` writes it once per session to **`<REMEMBER_DIR>/tmp/session-slug`**. One `key=value` per line:

```
format=1
status=ok
project_dir=/home/alice/projects/my-app
slug=-home-alice-projects-my-app
sessions_dir=/home/alice/.claude/projects/-home-alice-projects-my-app
memory_dir=/home/alice/projects/my-app/.remember
session_id=0f4c…
```

**Three states, not two.** An empty slug is not an absence — it resolves to `~/.claude/projects/` **itself**, a directory that exists and holds every project's transcripts. So the record never spells "I could not answer" as an empty value:

| What you find                                    | What it means                                                                 |
| ------------------------------------------------ | ----------------------------------------------------------------------------- |
| no file                                           | this plugin never wrote one — an older version, or the session-start hook never ran. Nothing is claimed; compute it yourself. |
| `status=unavailable` **and no `slug=` key at all** | the hook ran and could not answer. `reason=` says why. Never treat this as an empty slug. |
| `status=ok` with a non-empty `slug=`              | usable. This is the only case that is.                                         |

**Staleness: compare `project_dir`, and ignore everything else.** One store can be written by more than one project — git worktrees deliberately share a `REMEMBER_DIR` with the main checkout while keeping their own `PROJECT_DIR` — so the last session to start owns this file. A record left by a long-dead session is still **correct**, because the slug is a pure function of the path and age cannot make it wrong. A record left by a *different* project is wrong immediately, however fresh. That is why there is no timestamp here: it would only offer a staleness test that answers the wrong question.

One thing this file cannot do is tell you where it is, in the layout where the slug names its own directory. That is what the index below is for.

### 2. Find the record when the slug names its directory

In [external storage mode](external-storage-mode.md#external-storage-mode) with `{slug}` in `data_dir` — the layout `config.user.example.json` ships, under a `_purpose` that says to copy it — `REMEMBER_DIR` is itself named by the slug, so the record above sits behind the answer it holds ([#297](https://github.com/Digital-Process-Tools/claude-remember/issues/297)). `scripts/session-start-hook.sh` therefore also writes an index at the **store root**, which is the one path in that layout you *can* name: your `data_dir` template, truncated at `{slug}`.

```
data_dir template : ~/.remember/{slug}
store root        : ~/.remember
index             : ~/.remember/tmp/sessions
```

Line 1 is `format=1`. Every later line is one project — tab-separated, `project_dir` last:

```
format=1
status=ok<TAB>slug=-home-alice-my-app<TAB>memory_dir=/home/alice/.remember/-home-alice-my-app<TAB>project_dir=/home/alice/my-app
```

**You derive nothing.** Match `project_dir` against the path you already hold, byte for byte, then read `slug` and `memory_dir` off the row. There is no key to compute, because any key computed from the project path would be a second algorithm over it — which is the thing [#294](https://github.com/Digital-Process-Tools/claude-remember/issues/294) and #296 exist to delete. Matching rather than computing is also why this cannot answer *wrongly*; it can only fail to answer.

**Split on the first three tabs, and no further.** A tab is legal in a POSIX path, and `project_dir` is placed last so that it is the only field that can ever contain one. The other three cannot: `slug` is ASCII by construction, and a row is not written at all if `memory_dir` or `project_dir` contains a tab or a newline.

**Three states, again** — the same three the record has, one level up:

| What you find                                       | What it means                                                                 |
| --------------------------------------------------- | ----------------------------------------------------------------------------- |
| no index file                                        | nothing is claimed. An older version, the default layout, or the hook never ran. |
| an index with **no row** for your `project_dir`      | this store has not seen that project. Explicitly not an answer, and never an empty slug. |
| a row with `status=ok` and a non-empty `slug=`       | usable. This is the only case that is.                                          |

**It exists only where it is needed.** With no `{slug}` in `data_dir` — the default `<project>/.remember/` layout, or a single-directory external store — the store root and `REMEMBER_DIR` are the same directory, `<REMEMBER_DIR>/tmp/session-slug` is already nameable from `project_dir` and the template, and **no index is written**. That is deliberate twice over: the common layout pays nothing for the external one, and there are never two files that could disagree about one slug.

**No timestamps, and no row is ever expired.** Same reasoning as the record: the slug is a pure function of the path, so a row for a directory since deleted can never be *matched* by a caller holding a live `project_dir`, and if that path is recreated the row is still correct. Rows are not pruned by testing whether the directory still exists either — that test would drop correct rows for anything on an unmounted share. The file is bounded at 1000 rows instead, dropping by position, which this rewrite maintains as last-write order. **Position is not a staleness test**; do not read it as one.

**What it costs.** One session-start rewrite under the plugin's lock, measured at **+27 ms min / +28 ms median on a 260 ms session-start hook** (macOS, n=25 interleaved), and **+31 ms at a full 1000-row file** — the cost is the lock and the rewrite, not the size. Nothing at all on the per-tool-call path: `REMEMBER_STORE_ROOT` is resolved by parameter expansion with no subshell, and `post-tool-hook.sh` and `lib-slug.sh` are byte-identical to before.

If you are currently scanning `<store root>/*/tmp/session-slug` and matching on `project_dir`, this is that, with a documented path and one file read instead of a directory listing per lookup.

### 3. Check your implementation against `docs/slug-vectors.json`

If you do compute the slug yourself, **[`docs/slug-vectors.json`](docs/slug-vectors.json) is the contract.** It is a machine-readable list of input paths and the slug this plugin produces for each, covering every shape the test suite parametrizes: the six Windows drive spellings from [#263](https://github.com/Digital-Process-Tools/claude-remember/issues/263), UNC paths, the `\\?\` long-path forms from #294, the 200-character truncation and its base36 hash, non-ASCII paths on both sides of the UTF-16 surrogate boundary, and ill-formed UTF-8.

It is not a prose spec, and that is the point. **The file is generated from the implementation, and the suite regenerates it on every CI run and fails if the checked-in bytes differ** (`tests/test_slug_vectors_294.py`). It cannot drift from `scripts/lib-slug.sh` without our own build going red, so a port that diffs against it is diffing against something we are already holding still — and a divergence you find is a bug report we can act on rather than an argument about which document is current.

Each vector carries the environment its expected value depends on, because otherwise a port with no `cygpath` cannot use the file correctly:

| Field       | Use                                                                                                          |
| ----------- | ------------------------------------------------------------------------------------------------------------ |
| `path`      | the input, when it is valid UTF-8; `null` when it is not                                                      |
| `path_b64`  | the input as raw bytes, base64. Always present, and authoritative                                             |
| `slug`      | the expected result, always ASCII                                                                             |
| `cygpath`   | `agnostic` (same answer either way), `present` (only holds with `cygpath` — an MSYS path being converted), or `absent` |
| `truncated` | the slug passed 200 characters and carries a hash                                                             |
| `requires`  | what must be available to reproduce it with the shell version                                                 |

**If you are porting, the vectors you want are `cygpath: agnostic` and `cygpath: absent`.** Those describe the pure function, which is what a caller holding a native path (`C:\dev\project`) needs. The `cygpath: present` vectors describe what the shell does to an MSYS-shaped path on its way in, and they are generated against a *model* of `cygpath` (`tests/cygpath_stub.py`), not a real one — the file says so itself.

Regenerate after any deliberate change to the slug:

```bash
python3 -m tests.slug_vectors
```

Before you do: every path whose slug moves is a **store rename** for the people on it, and on a case-insensitive filesystem only git can see it happen. That is what [#263](https://github.com/Digital-Process-Tools/claude-remember/issues/263) was.

