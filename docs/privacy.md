# Privacy Policy — Remember

Last updated: 2026-09-12. Publisher: Digital Process Tools (Florian David).

## What Remember does with data

Remember is a plugin for agent CLIs (Claude Code, Codex, Antigravity). It keeps a
record of your coding sessions so the next session can continue where the last one
stopped.

## Data collected

- **Session content.** Remember reads the transcript of your session with the host
  CLI and writes summaries and handoff notes to files on your machine, under
  `.remember/` in the project directory or under `~/.remember/`. This content can
  include anything you typed or the assistant produced during the session.
- **Nothing else.** Remember collects no account information, no telemetry, no
  analytics, no device identifiers.

## Where data goes

- All files stay on your machine. Digital Process Tools operates no server and
  receives no data from Remember.
- Summaries are produced by calling the host CLI's model (for example `claude -p`).
  That call is governed by the privacy policy of the model provider you already use
  (Anthropic, OpenAI, or Google), under your own account. Remember adds no other
  third party.
- The skills-only edition published in the OpenAI plugin directory sends nothing
  anywhere: it only writes a local note file.

## Sharing

Remember shares no data with anyone. Digital Process Tools has no access to your
files.

## Retention and controls

- Files persist until you delete them. Remove `.remember/` or `~/.remember/` to
  erase everything Remember has stored.
- Capture can be disabled or scoped per project through the plugin configuration
  (see [configuration.md](configuration.md)), or by uninstalling the plugin.
- Add `.remember/` to `.gitignore` if you do not want notes committed to a
  repository.

## Children

Remember is a developer tool and is not directed at children under 13.

## Changes and contact

Changes to this policy are recorded in this file's git history. Questions:
open an issue at https://github.com/Digital-Process-Tools/claude-remember/issues
or email florian.david.info@gmail.com.
