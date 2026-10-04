"""#898 round 19: every `case` statement in shipped shell code became an
if/elif ladder of `[ ]` tests (the directory's scanner mis-parses `case`).
This module runs the OLD `case` and the NEW ladder on the same inputs, in the
same bash and the same locale, and requires identical results -- output,
variable state and exit status alike -- so "behaviour preserved" is measured
rather than reasoned.

One entry per distinct shape, not per site: the session-id validators, the
"empty or not all digits" guards, glob arms, multi-pattern `|` arms,
first-match-wins ladders, a catch-all inside a loop (`continue`), the xtrace
`$-` check, prefix/suffix globs and literal dispatch. Each entry also names a
line of its NEW form that must appear in a shipped file, so the snippet tested
here cannot drift away from the code that ships.

"Contains" is written `[ "${x/PAT/}" != "$x" ]` (the first match removed),
never `[ "${x#*PAT}" != "$x" ]`: bash matches a `#*` prefix by trying every
prefix length, which is quadratic -- a 300 KB hook payload took about a
minute per test on bash 3.2 and 5.3 alike, where `case` and `${x/PAT/}` take
no measurable time. test_contains_is_linear_on_a_large_input pins that.

Runs every bash it can find -- the PATH one, plus macOS's /bin/bash 3.2 when
present -- under LC_ALL=C and, where installed, a UTF-8 locale: bracket
ranges and classes are matched by the same bash glob matcher in both forms,
so the two must agree under any one locale (#695).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests._bash_runner import decode_bash_output, resolve_bash

REPO_ROOT = Path(__file__).resolve().parent.parent

PRELUDE = r"""
dq='"'
field=session_id
P=/proj/a
TODAY=2024-01-01
SEEN=/nonexistent-898
L='tracked unavailable symlinked-ancestor'
"""

# Inputs every shape is fed, on top of its own extras.
UNIVERSAL = [
    "", ".", "..", "...", "-", "-x", "--", "abc", "a-b_c.d", "A.Z_09-",
    "a b", " ", "a/b", "/", "/abs", "//", "~", "~/x", "x~", "C:", "c:", "C:/x",
    "C:\\x", "C:x", "\\", "\\x", "x\\", "0", "00", "007", "123", "12a", "a1",
    " 1", "1 ", "a\nb", "a\rb", "\n", "\r", "a\tb", "x*y", "*", "?", "[", "]",
    "[a]", "x:y", ":", ":x", "x:", "é", "\u0130", "caf\u00e9", "%-d", "%_H",
    "%0m", "%^a", "%#x", "%a", "%", "%%-", "1.5", "1,5", "1.", ",", "x.y,z",
]

SHAPES = {
    "session id, dash rejected": {
        "old": r'''case "$x" in
    ''|[.]|[.][.]|-*|*[!A-Za-z0-9._-]*) x="" ;;
esac''',
        "new": r'''if [ -z "${x#.}" ] || [ -z "${x#..}" ] || [ "${x#-}" != "$x" ] \
    || [ "${x/[!A-Za-z0-9._-]/}" != "$x" ]; then
    x=""
fi''',
        "site": ("scripts/post-tool-hook.sh",
              '|| [ "${STDIN_SESSION_ID/[!A-Za-z0-9._-]/}" != "$STDIN_SESSION_ID" ]; then'),
    },
    "session id, dash allowed": {
        "old": r'''case "$x" in
    ''|[.]|[.][.]|*[!A-Za-z0-9._-]*) x="" ;;
esac''',
        "new": r'''if [ -z "${x#.}" ] || [ -z "${x#..}" ] \
    || [ "${x/[!A-Za-z0-9._-]/}" != "$x" ]; then
    x=""
fi''',
        "site": ("scripts/doctor.sh",
              '|| [ "${_DOCTOR_SESSION_ID/[!A-Za-z0-9._-]/}" != "$_DOCTOR_SESSION_ID" ]; then'),
    },
    "seen marker, empty falls to the catch-all": {
        "old": r'''case "$x" in
    [.]|[.][.]|*[!A-Za-z0-9._-]*) : ;;
    *) [ -e "$SEEN/$x" ] && return 0 ;;
esac
echo after''',
        "new": r'''if [ -n "$x" ] && { [ -z "${x#.}" ] || [ -z "${x#..}" ]; }; then
    :
elif [ "${x/[!A-Za-z0-9._-]/}" != "$x" ]; then
    :
else
    [ -e "$SEEN/$x" ] && return 0
fi
echo after''',
        "site": ("scripts/session-start-hook.sh",
              'if [ -n "$1" ] && { [ -z "${1#.}" ] || [ -z "${1#..}" ]; }; then'),
    },
    "empty or a non-digit, with a catch-all": {
        "old": r'''case "$x" in
    (''|*[!0-9]*) echo unreadable ;;
    (*) echo "$x" ;;
esac''',
        "new": r'''if [ -z "$x" ] || [ "${x/[!0-9]/}" != "$x" ]; then
    echo unreadable
else
    echo "$x"
fi''',
        "site": ("scripts/save-session.sh",
              'if [ -z "$_ndc_gen" ] || [ "${_ndc_gen/[!0-9]/}" != "$_ndc_gen" ]; then'),
    },
    "empty, a non-digit or zero": {
        "old": r'''case "$x" in ''|*[!0-9]*|0) x=20 ;; esac''',
        "new": r'''if [ -z "$x" ] || [ "${x/[!0-9]/}" != "$x" ] || [ "$x" = 0 ]; then
    x=20
fi''',
        "site": ("hooks.d/before_session_start/50-git-restore.sh",
              '|| [ "$FETCH_TIMEOUT" = 0 ]; then'),
    },
    "catch-all inside a loop": {
        "old": r'''for _i in 1; do
    case "$x" in
        (''|*[!0-9]*)
            echo skipped
            continue
            ;;
    esac
    echo kept
done''',
        "new": r'''for _i in 1; do
    if [ -z "$x" ] || [ "${x/[!0-9]/}" != "$x" ]; then
        echo skipped
        continue
    fi
    echo kept
done''',
        "site": ("scripts/session-start-hook.sh",
              'if [ -z "$_remember_now" ] || [ "${_remember_now/[!0-9]/}" != "$_remember_now" ]; then'),
    },
    "newline or carriage return": {
        "old": r'''case "$x" in
    *$'\n'*|*$'\r'*) x="" ;;
esac''',
        "new": r'''if [ "${x/$'\n'/}" != "$x" ] || [ "${x/$'\r'/}" != "$x" ]; then
    x=""
fi''',
        "site": ("scripts/user-prompt-hook.sh",
              'if [ "${REMEMBER_HOOK_CWD/$\'\\n\'/}" != "$REMEMBER_HOOK_CWD" ] \\'),
    },
    "xtrace flag in $-": {
        "old": r'''[ "$x" = on ] && set -x
case "$-" in
    (*x*) r=1 ;;
    (*) r=0 ;;
esac
set +x
echo "$r"''',
        "new": r'''[ "$x" = on ] && set -x
if [ "${-/x/}" != "$-" ]; then
    r=1
else
    r=0
fi
set +x
echo "$r"''',
        "extra": ["on", "off"],
        "site": ("scripts/bootstrap-dirs.sh", 'if [ "${-/x/}" != "$-" ]; then'),
    },
    "absolute path, three glob arms": {
        "old": r'''case "$x" in
    /*|~*|[A-Za-z]:[/\\]*) echo abs ;;
    *) echo rel ;;
esac''',
        "new": r'''if [ "${x#/}" != "$x" ] || [ "${x#[~]}" != "$x" ] \
    || [ "${x#[A-Za-z]:[/\\]}" != "$x" ]; then
    echo abs
else
    echo rel
fi''',
        "site": ("scripts/lib-memory-dir.sh", '|| [ "${data_dir#[A-Za-z]:[/\\\\]}" != "$data_dir" ]; then'),
    },
    "a root prefix, whole-word arms": {
        "old": r'''case "$x" in
    ''|/|[A-Za-z]:|[A-Za-z]:[/\\]) return 0 ;;
esac
echo kept''',
        "new": r'''if [ -z "$x" ] || [ "$x" = / ] || [ -z "${x#[A-Za-z]:}" ] \
    || [ -z "${x#[A-Za-z]:[/\\]}" ]; then
    return 0
fi
echo kept''',
        "site": ("scripts/lib-memory-dir.sh", 'if [ -z "$prefix" ] || [ "$prefix" = / ] || [ -z "${prefix#[A-Za-z]:}" ] \\'),
    },
    "strftime flag modifiers": {
        "old": r'''case "$x" in
    *%-*|*%_*|*%0*|*%^*|*%#*) return 1 ;;
esac''',
        "new": r'''if [ "${x/"%-"/}" != "$x" ] || [ "${x/"%_"/}" != "$x" ] || [ "${x/"%0"/}" != "$x" ] \
    || [ "${x/"%^"/}" != "$x" ] || [ "${x/"%#"/}" != "$x" ]; then
    return 1
fi''',
        "site": ("scripts/lib-clock.sh", 'if [ "${1/"%-"/}" != "$1" ] || [ "${1/"%_"/}" != "$1" ] || [ "${1/"%0"/}" != "$1" ] \\'),
    },
    "quoted needle built from variables": {
        "old": r'''case "$x" in *"$dq$field$dq"*) ;; *) return 1 ;; esac
echo found''',
        "new": r'''[ "${x/"$dq$field$dq"/}" != "$x" ] || return 1
echo found''',
        "extra": ['{"session_id": "a"}', '"session_id"', 'session_id', '"session_id', '*"session_id"*'],
        "site": ("scripts/session-end-hook.sh", '[ "${raw/"$dq$field$dq"/}" != "$raw" ] || return 1'),
    },
    "a class inside a negated bracket": {
        "old": r'''case "$x" in *[!:[:space:]]*) return 1 ;; esac
echo blank''',
        "new": r'''if [ "${x/[!:[:space:]]/}" != "$x" ]; then
    return 1
fi
echo blank''',
        "extra": [": ", " : :", "\t:\n"],
        "site": ("scripts/user-prompt-hook.sh", 'if [ "${prefix/[!:[:space:]]/}" != "$prefix" ]; then'),
    },
    "literal dispatch with a space in one arm": {
        "old": r'''case "${x:-python3}" in
    python3) echo p3 ;;
    python) echo p ;;
    py\ -3) echo py3 ;;
    py) echo py ;;
    *) return 127 ;;
esac''',
        "new": r'''if [ "${x:-python3}" = python3 ]; then
    echo p3
elif [ "${x:-python3}" = python ]; then
    echo p
elif [ "${x:-python3}" = "py -3" ]; then
    echo py3
elif [ "${x:-python3}" = py ]; then
    echo py
else
    return 127
fi''',
        "extra": ["python3", "python", "py -3", "py", "py -2", "py\\ -3", "python3 "],
        "site": ("scripts/lib-slug.sh", 'elif [ "${PYTHON:-python3}" = "py -3" ]; then'),
    },
    "list membership": {
        "old": r'''case " $L " in
    (*" $x "*) return 0 ;;
    (*) return 1 ;;
esac''',
        "new": r'''_l=" $L "
[ "${_l/" $x "/}" != "$_l" ]''',
        "extra": ["tracked", "unavailable", "symlinked-ancestor", "untracked", "tracked unavailable", "*"],
        "site": ("scripts/lib-memory-context.sh", '[ "${_ls/" $1 "/}" != "$_ls" ]'),
    },
    "a file-name family, nested": {
        "old": r'''case "$x" in
    remember.md) return 0 ;;
    remember.*.md)
        v="${x#remember.}"
        v="${v%.md}"
        case "$v" in
            ''|*[!A-Za-z0-9._-]*) return 1 ;;
            *) return 0 ;;
        esac
        ;;
    *) return 1 ;;
esac''',
        "new": r'''if [ "$x" = remember.md ]; then
    return 0
fi
r="${x#remember.}"
if [ "$r" = "$x" ] || [ "${r%.md}" = "$r" ]; then
    return 1
fi
v="${r%.md}"
if [ -z "$v" ] || [ "${v/[!A-Za-z0-9._-]/}" != "$v" ]; then
    return 1
fi
return 0''',
        "extra": ["remember.md", "remember.x.md", "remember..md", "remember.md.md",
               "remember.", "remember.a/b.md", "xremember.md", "remember.a b.md",
               "remember.mdx", "remember.x.MD", "remember.md "],
        "site": ("scripts/write-handoff.sh", 'if [ "$_rest" = "$_base" ] || [ "${_rest%.md}" = "$_rest" ]; then'),
    },
    "a fixed-width date": {
        "old": r'''case "$x" in
    ([0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
    (*) x=TODAY ;;
esac''',
        "new": r'''if [ -z "$x" ] || [ -n "${x#[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]}" ]; then
    x=TODAY
fi''',
        "extra": ["2024-01-31", "2024-1-31", "2024-01-31x", "x2024-01-31", "abcd-ef-gh",
               "2024-01-3", "20240-01-31", "2024-01-31\n"],
        "site": ("scripts/save-session.sh", 'if [ -z "$NDC_DAY" ] || [ -n "${NDC_DAY#[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]}" ]; then'),
    },
    "a digit ladder, first arm wins": {
        "old": r'''case "$x" in
    (1) r=one ;;
    ([2-4]) r=few ;;
    (*) r=other ;;
esac
echo "$r"''',
        "new": r'''if [ "$x" = 1 ]; then
    r=one
elif [ "$x" = 2 ] || [ "$x" = 3 ] || [ "$x" = 4 ]; then
    r=few
else
    r=other
fi
echo "$r"''',
        "extra": ["1", "2", "3", "4", "5", "11", "24", "-1"],
        "site": ("scripts/save-session.sh", 'elif [ "$NDC_HEADER_LINE" = 2 ] || [ "$NDC_HEADER_LINE" = 3 ] || [ "$NDC_HEADER_LINE" = 4 ]; then'),
    },
    "a leading dash or any colon": {
        "old": r'''case "$x" in
    -*|*:*) x="" ;;
esac''',
        "new": r'''if [ "${x#-}" != "$x" ] || [ "${x/:/}" != "$x" ]; then
    x=""
fi''',
        "extra": ["main", "-main", "a:b", "refs/heads/x"],
        "site": ("hooks.d/after_save/50-git-backup.sh",
              'if [ "${GIT_BACKUP_BRANCH#-}" != "$GIT_BACKUP_BRANCH" ] || [ "${GIT_BACKUP_BRANCH/:/}" != "$GIT_BACKUP_BRANCH" ]; then'),
    },
    "a quoted directory, then any rest": {
        "old": r'''case "$x" in
    "$P"/*) echo inside ;;
esac''',
        "new": r'''if [ "${x#"$P"/}" != "$x" ]; then
    echo inside
fi''',
        "extra": ["/proj/a/", "/proj/a/x", "/proj/a", "/proj/ab/x", "/proj/a//", "/proj/*/x"],
        "site": ("scripts/bootstrap-dirs.sh", 'if [ "${_mem_bd_glob_dir#"$_mem_bd_glob_proj"/}" != "$_mem_bd_glob_dir" ]; then'),
    },
    "a drive letter and a colon": {
        "old": r'''case "$x" in
    ?:*) echo drive ;;
esac''',
        "new": r'''if [ "${x#?:}" != "$x" ]; then
    echo drive
fi''',
        "site": ("scripts/lib-slug.sh", 'if [ "${path#?:}" != "$path" ]; then'),
    },
    "a separator, two expansions in the arm": {
        "old": r'''case "$x" in
    *[.,]*) s="${x%%[.,]*}"; f="${x#*[.,]}" ;;
    *)      s="$x"; f="000000" ;;
esac
echo "$s/$f"''',
        "new": r'''if [ "${x/[.,]/}" != "$x" ]; then
    s="${x%%[.,]*}"; f="${x#*[.,]}"
else
    s="$x"; f="000000"
fi
echo "$s/$f"''',
        "site": ("scripts/lib-lock.sh", 'if [ "${_r/[.,]/}" != "$_r" ]; then'),
    },
    "suffix arms inside a loop": {
        "old": r'''for f in "$x"; do
    case "$f" in
        (*"today-${TODAY}.md") continue ;;
        (*.done.md) continue ;;
    esac
    echo "kept $f"
done''',
        "new": r'''for f in "$x"; do
    if [ "${f%"today-${TODAY}.md"}" != "$f" ] || [ "${f%.done.md}" != "$f" ]; then
        continue
    fi
    echo "kept $f"
done''',
        "extra": ["a/today-2024-01-01.md", "today-2024-01-01.md", "x.done.md", ".done.md",
               "today-2024-01-01.mdx", "today-2024-01-02.md", "done.md"],
        "site": ("scripts/session-start-hook.sh",
              'if [ "${_remember_staging_file%"today-${TODAY}.md"}" != "$_remember_staging_file" ] \\'),
    },
    "a prefix glob": {
        "old": r'''case "$x" in
    linux*) return 0 ;;
esac
return 1''',
        "new": r'''[ "${x#linux}" != "$x" ] && return 0
return 1''',
        "extra": ["linux", "linux-gnu", "Linux", "darwin", "xlinux"],
        "site": ("scripts/lib-slug.sh", '[ "${_os#linux}" != "$_os" ] && return 0'),
    },
    "a literal list with an empty arm": {
        "old": r'''case "$x" in
    startup|resume|clear|compact|fork) ;;
    *) x="" ;;
esac''',
        "new": r'''if [ "$x" != startup ] && [ "$x" != resume ] && [ "$x" != clear ] \
    && [ "$x" != compact ] && [ "$x" != fork ]; then
    x=""
fi''',
        "extra": ["startup", "resume", "clear", "compact", "fork", "forks", "Startup"],
        "site": ("scripts/session-start-hook.sh", '&& [ "$SESSION_START_SOURCE" != compact ] && [ "$SESSION_START_SOURCE" != fork ]; then'),
    },
    "a 0/1 string": {
        "old": r'''case "$x" in
    *[!01]*|'') return 1 ;;
esac
echo ok''',
        "new": r'''if [ "${x/[!01]/}" != "$x" ] || [ -z "$x" ]; then
    return 1
fi
echo ok''',
        "extra": ["0", "1", "01", "10", "2", "0 1"],
        "site": ("scripts/log.sh", 'if [ "${_exists_raw/[!01]/}" != "$_exists_raw" ] || [ -z "$_exists_raw" ]; then'),
    },
    "a slash inside the needle": {
        "old": r'''case "$x" in
    */remember-config-*) return 0 ;;
    *) return 1 ;;
esac''',
        "new": r'''_cfg_needle=/remember-config-
if [ "${x/"$_cfg_needle"/}" != "$x" ]; then
    return 0
fi
return 1''',
        "extra": ["/tmp/remember-config-1", "remember-config-x", "/remember-config-",
                  "a/remember-configx", "//remember-config--"],
        "site": ("scripts/log.sh", 'if [ "${_cfg_path/"$_cfg_needle"/}" != "$_cfg_path" ]; then'),
    },
    "a needle with an unquoted id in it": {
        "old": r'''S='{"a": 1, "abc-1.2": 3}'
case "$S" in
    *"$dq"$x"$dq":*) echo trusted ;;
    *) echo not ;;
esac''',
        "new": r'''S='{"a": 1, "abc-1.2": 3}'
if [ "${S/"$dq"$x"$dq":/}" != "$S" ]; then
    echo trusted
else
    echo not
fi''',
        "extra": ["a", "abc-1.2", "abc-1", "b", "abc-1.2\"", "a\": 1, \"abc-1.2"],
        "site": ("scripts/post-tool-hook.sh",
                 'if [ "${_SESSIONS_SCOPE/"$_pt_dq"$SESSION_ID"$_pt_dq":/}" != "$_SESSIONS_SCOPE" ]; then'),
    },
    "a non-ASCII byte": {
        "old": r'''h=0
case "$x" in
    *[!$'\001'-$'\177']*) h=1 ;;
esac
echo "$h"''',
        "new": r'''h=0
_hb_glob="[!"$'\001'"-"$'\177'"]"
if [ "${x/$_hb_glob/}" != "$x" ]; then
    h=1
fi
echo "$h"''',
        "extra": ["caf\u00e9", "\u0130", "plain", "\x7f", "a\x01b"],
        "site": ("scripts/lib-slug.sh", 'if [ "${path/$_hb_glob/}" != "$path" ]; then'),
    },
}

_RUNNER = r"""
set -u
%(prelude)s
old_() {
local x="$1"
%(old)s
}
new_() {
local x="$1"
%(new)s
}
for a in "$@"; do
    o=$(old_ "$a" 2>/dev/null; printf ' rc=%%s' "$?")
    n=$(new_ "$a" 2>/dev/null; printf ' rc=%%s' "$?")
    printf '%%s\037%%s\036' "$o" "$n"
done
"""


def _bashes() -> list[str]:
    found = []
    path_bash = resolve_bash()
    if path_bash:
        found.append(path_bash)
    if os.path.exists("/bin/bash") and os.path.realpath("/bin/bash") not in {
            os.path.realpath(b) for b in found}:
        found.append("/bin/bash")
    return found


def _locales(bash: str) -> list[str]:
    out = ["C"]
    try:
        listed = subprocess.run(["locale", "-a"], capture_output=True, text=True,
                                check=False, timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return out
    for cand in ("en_US.UTF-8", "C.UTF-8", "en_US.utf8", "C.utf8"):
        if cand in listed:
            out.append(cand)
            break
    return out


BASHES = _bashes()
pytestmark = pytest.mark.skipif(not BASHES, reason="no bash on this host")


def _run(bash: str, locale: str, shape: dict) -> list[tuple[str, str, str]]:
    inputs = UNIVERSAL + shape.get("extra", [])
    script = _RUNNER % {"prelude": PRELUDE, "old": shape["old"], "new": shape["new"]}
    env = {k: v for k, v in os.environ.items() if not k.startswith("LC_") and k != "LANG"}
    env["LC_ALL"] = locale
    proc = subprocess.run([bash, "-c", script, "runner", *inputs], capture_output=True,
                          env=env, timeout=60, check=False)
    out = decode_bash_output(proc.stdout)
    assert proc.returncode == 0, decode_bash_output(proc.stderr)
    rows = [r for r in out.split("\x1e") if r]
    assert len(rows) == len(inputs), (len(rows), len(inputs), decode_bash_output(proc.stderr))
    return [(i, *r.split("\x1f")) for i, r in zip(inputs, rows)]


@pytest.mark.parametrize("bash", BASHES)
@pytest.mark.parametrize("name", sorted(SHAPES))
def test_new_form_matches_old_case(name, bash):
    shape = SHAPES[name]
    for locale in _locales(bash):
        diffs = [(i, o, n) for i, o, n in _run(bash, locale, shape) if o != n]
        assert not diffs, f"{bash} LC_ALL={locale}: {name}: " + "; ".join(
            f"{i!r}: case gave {o!r}, rewrite gave {n!r}" for i, o, n in diffs)


@pytest.mark.parametrize("bash", BASHES)
def test_harness_sees_a_difference(bash):
    """Positive control: a rewrite that is wrong for some input (here, one
    that forgets the empty arm) is reported, so a green run above is not
    the harness comparing nothing."""
    wrong = {"old": SHAPES["empty or a non-digit, with a catch-all"]["old"],
             "new": r'''if [ "${x/[!0-9]/}" != "$x" ]; then echo unreadable; else echo "$x"; fi'''}
    rows = _run(bash, "C", wrong)
    assert [i for i, o, n in rows if o != n] == [""]


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_rewrite_ships(name):
    """The tested NEW form is the one in the shipped file, not a copy that
    drifted: the named line is present verbatim."""
    rel, line = SHAPES[name]["site"]
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert line in text, f"{rel} does not carry: {line}"


# A 300 KB input with no match is the worst case for a "contains" test, and
# the hook payloads these guards read can be that large (test_post_tool_hook_
# stdin_size). Each shipped form below must finish in well under the time the
# quadratic `${x#*PAT}` form takes (about a minute on this size).
LINEAR_FORMS = {
    "a quoted needle": '[ "${x/"$dq$field$dq"/}" != "$x" ]',
    "a non-digit": '[ "${x/[!0-9]/}" != "$x" ]',
    "a session-id byte": '[ "${x/[!A-Za-z0-9._-]/}" != "$x" ]',
    "a newline": '[ "${x/$\'\\n\'/}" != "$x" ]',
}


@pytest.mark.parametrize("bash", BASHES)
@pytest.mark.parametrize("form", sorted(LINEAR_FORMS))
def test_contains_is_linear_on_a_large_input(form, bash):
    fill = "7" if form == "a non-digit" else "a"
    script = (PRELUDE + f'x=$(printf "%0300000d" 0 | tr 0 {fill})\n'
              + f'if {LINEAR_FORMS[form]}; then echo hit; else echo miss; fi\n')
    proc = subprocess.run([bash, "-c", script], capture_output=True, timeout=20, check=False)
    assert decode_bash_output(proc.stdout).strip() == "miss", decode_bash_output(proc.stderr)
