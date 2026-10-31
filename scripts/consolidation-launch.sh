#!/bin/bash
# consolidation-launch.sh -- Windows hidden-launch wrapper for the
# SessionStart consolidation trigger (#1006).
#
# WHY A SEPARATE FILE, NOT A SOURCE INSIDE session-start-hook.sh: lib-detach.sh
# is not one of compile_hooks.py's four HOOK_SCRIPT_NAMES, so sourcing it
# directly from session-start-hook.sh would inline its whole compiled body
# (4,236 bytes after whole-line-comment stripping, as of this writing) into
# that hook's own compiled output, pushing it well over its 120 KiB release
# budget (check_release_tree.py's HOOK_SCRIPT_MAX_BYTES) with no margin left
# to absorb it (see session-start-hook.sh's own comment at its call site).
# This file sources lib-detach.sh itself, in its OWN process -- its own
# source chain is never inlined into any hook's compiled output, because it
# is not itself one of the four names compile_hooks.py inlines INTO.
#
# session-start-hook.sh invokes this SYNCHRONOUSLY (no nohup/& at that call
# site), unconditionally -- this file, not the caller, decides Windows vs.
# not. The actual detachment happens IN HERE, either via
# _remember_detach_windows's own async WshShell.Run or, on the nohup
# fallback below, via the same `& disown` shape every other detach site
# already uses. Either route returns to the caller almost immediately
# (WshShell.Run's own wait flag is already False; nohup backgrounding
# returns at once), so this one extra fork+exec costs session-start-hook.sh
# a few milliseconds on the staging-backlog branch only -- a branch that
# already pays several forks (two `echo`s, the launch itself) and does not
# fire on an ordinary session start with no backlog.
#
# No PID_FILE double-launch guard is needed here (unlike post-tool-hook.sh's
# save): run-consolidation.sh takes its own atomic lock (lib-lock.sh,
# noclobber) before doing any work, so a watchdog-kill race between the
# hidden route and the nohup fallback below is resolved there, not here --
# the second launcher simply finds the lock held and exits 0.
set -u
_lib_dir="${BASH_SOURCE[0]%/*}"
[ "$_lib_dir" = "${BASH_SOURCE[0]}" ] && _lib_dir="$(pwd)"
source "$_lib_dir/lib-detach.sh"

_detach_rc=1
_remember_is_windows && { _remember_detach_windows /dev/null /dev/null "$@"; _detach_rc=$?; }
if [ "$_detach_rc" -ne 0 ]; then
    nohup "$@" </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
fi
