"""#859 -- /remember:doctor's `allowed-tools: Bash` front matter pre-approved
*any* shell command for the full run of the command, because the command body
only ever runs one script. The Anthropic directory flags this as "Pre-approves
broad shell access in allowed-tools" on any version it scans, including the
slim `release` tree (#851), which still ships `commands/doctor.md` unchanged.

Fix: scope the grant to the one script, mirroring `skills/remember/SKILL.md`'s
`Bash(${CLAUDE_PLUGIN_ROOT}/scripts/write-handoff.sh:*)`, and make
`scripts/doctor.sh` directly executable (git mode 100755) so the command body
can exec it the same way `write-handoff.sh` already is, instead of going
through a bare `bash "..."` that the narrow allowed-tools pattern cannot match.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR_MD = REPO_ROOT / "commands" / "doctor.md"
DOCTOR_SH = REPO_ROOT / "scripts" / "doctor.sh"


def _front_matter_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no YAML front matter"
    end = text.index("\n---", 4)
    return text[4:end]


def test_doctor_md_allowed_tools_is_scoped_to_its_script():
    """MUST FIRE before the fix: a bare `Bash` grant is unrestricted shell
    access for the whole command run, not scoped to the one script it needs."""
    fm = _front_matter_text(DOCTOR_MD)
    m = re.search(r"^allowed-tools:\s*(.+)$", fm, re.MULTILINE)
    assert m, f"no allowed-tools line in {DOCTOR_MD}"
    value = m.group(1).strip()
    assert value != "Bash", (
        f"{DOCTOR_MD} still grants bare `Bash` (unrestricted shell) -- "
        "scope it to Bash(${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh:*)"
    )
    assert value == "Bash(${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh:*)", value


def test_doctor_md_invokes_the_script_directly_not_via_bare_bash():
    """The command body must exec the script directly (`"${CLAUDE_PLUGIN_ROOT}/...`),
    the same shape `skills/remember/SKILL.md` already uses for write-handoff.sh --
    a scoped `Bash(...:*)` pattern cannot match a `bash "..."` invocation, since
    the first word of the command line is `bash`, not the script path."""
    body = DOCTOR_MD.read_text(encoding="utf-8")
    assert 'bash "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh"' not in body, (
        "doctor.md still invokes doctor.sh via bare `bash \"...\"`, which the "
        "scoped allowed-tools pattern (Bash(${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh:*)) "
        "cannot match"
    )
    assert '"${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh"' in body


def test_doctor_sh_is_executable_in_git():
    """MUST FIRE before the fix: doctor.sh is mode 100644 in git, so a direct
    exec (no `bash` prefix) fails on a platform that honours the executable
    bit. write-handoff.sh is the positive control: it is already 100755 and
    already invoked directly by skills/remember/SKILL.md."""
    import subprocess

    out = subprocess.run(
        ["git", "ls-files", "-s", "scripts/doctor.sh", "scripts/write-handoff.sh"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    modes = {
        line.split()[3]: line.split()[0] for line in out.splitlines() if line.strip()
    }
    assert modes.get("scripts/write-handoff.sh") == "100755", (
        "positive control: write-handoff.sh must stay executable in git"
    )
    assert modes.get("scripts/doctor.sh") == "100755", (
        f"scripts/doctor.sh is {modes.get('scripts/doctor.sh')!r} in git, not "
        "100755 -- a direct exec of the command body needs the executable bit"
    )
