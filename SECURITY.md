# Security Policy

## Supported Versions

Only the latest minor version receives security updates.

| Version | Supported          |
| ------- | ------------------ |
| 0.7.x   | :white_check_mark: |
| < 0.7   | :x:                |

## Trust Model

For the install-time / runtime trust model — what writes to `~/.remember/` can
do, why `hooks.d/` matters, and the opt-in git backup threat surface — see
[`docs/git-backup-security.md`](docs/git-backup-security.md).

## Reporting a Vulnerability

**Do not open public GitHub issues for security vulnerabilities.**

Email **fdavid@digitalprocesstools.com** with:

- A description of the issue
- Steps to reproduce
- Affected version (see `.claude-plugin/plugin.json`)
- Impact assessment if known

You can expect an acknowledgment within 7 days. We will work with you to
understand and resolve the issue, and credit you in the release notes if
desired.
