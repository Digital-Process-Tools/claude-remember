---
title: "Which copy of the plugin answered, and which version is current"
description: "Three different things are called 'the version' and they routinely disagree. Reading the wrong one, or reading the statusline marker backwards, has cost this repo whole sessions."
keywords: reload-plugins, plugin version, plugin copy, plugin currency, statusline, oss-workspace, plugin cache
---

Three readings, three different questions. They disagree normally, not exceptionally.

| reading | where | answers |
| --- | --- | --- |
| **clone / tree** | `.claude-plugin/plugin.json` in the checkout | what the source says today |
| **registry / session** | the path inside an injected slash-command's own text | which copy this command resolved from |
| **installed cache** | `~/.claude/plugins/cache/dpt-plugins/oss/<version>/` | every version ever unpacked |

**Old directories in the cache mean nothing.** `claude plugin update` unpacks a new one and leaves
the rest. Ten directories is normal; it is not evidence about what resolves.

**`doctor`'s `plugin copy` line compares content, not version numbers.** The manifest version does
not move between releases, so an installed copy at the tag and a clone a cycle past it declare the
same number. A `SKEW` is a report, not a fault -- it is the normal state between a merge and a
release. What it costs you: plugin prose quoted from the running session may be text the clone no
longer contains.

## `/reload-plugins` vs restart -- not interchangeable

| | moves the registry (which agents/skills/commands resolve) | moves command text already injected in this turn |
| --- | --- | --- |
| `/reload-plugins` | yes | **no** |
| restart | yes | yes |

So a session can hold a registry at one version and instructions from another, and that is not a bug.

- `Agent type not found` -> `/reload-plugins`, then retry. This is the whole fix.
- Never conclude "the agent files do not register" from inside an unreloaded session. #81 spent ten
  cohorts and four retracting comments on exactly that: a description-length hypothesis with a
  measured character boundary, a false regression report, all retracted, cause was a stale registry.

## Statusline markers -- they print different fields

| state | unicode | ascii | colour | the number shown is |
| --- | --- | --- | --- | --- |
| `behind` | `↥` | `>` | yellow | **latest** published |
| `ahead` | `↑` | `+` | green | **installed** |
| `current` | counted in `N✓` | | green | -- |
| `unknown` | `N?` | | dim | nothing was comparable |

`ahead` is the normal state in a clone of the plugin itself (unreleased work), and normal for any session
in the window after a release before the cache refreshes. It is not a problem.

`version_status` folds a stale comparison into `unknown` rather than inventing vocabulary -- so a
`?` means either half was missing **or** the reading was too old to trust.

## The cached `latest`

- Lives at `cache_dir()/<slug>.json` -- outside the repo. Never re-derive that path; call
  `statusline.cache_path`.
- Source is the newest **published GitHub Release**, via `_latest_release` -- *not* the manifest.
  A tag with no Release does not move it.
- `LATEST_REFRESH_AFTER` is the long clock; the board half refreshes far more often. Two clocks.
- `/oss:release` calls `invalidate_latest_cache` the moment a Release is created, because the
  publish is what falsifies the reading.
- **A reading fresh by its own rule can still be wrong.** The falsifying event can land one minute
  after a poll. Do not diagnose this by shortening the interval.

Force a re-read: delete the cache file. `doctor` never does this -- it reports the skew and leaves
the evidence.
