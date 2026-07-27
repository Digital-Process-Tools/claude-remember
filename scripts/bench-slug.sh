#!/usr/bin/env bash
#
# bench-slug.sh — what session_dir_slug costs, per kind of path.
#
# It exists because a review pointed out that the timing table in lib-slug.sh
# was a claim with nothing behind it. Numbers baked into permanent docs should
# be reproducible by the next person, on their machine, without trusting mine.
#
# It also exists because benching this found two real bugs that reading it did
# not: an `iconv` fork on every non-ASCII path on platforms that can never need
# it, and a glob (`[!$'\000'-...]`) that matched almost everything because bash
# cannot hold a NUL in a string.
#
# The absolute numbers are machine-specific and dominated by subprocess spawn
# cost. The ratios are the point: an ASCII path must stay in the byte table,
# because this function runs on every single tool call.
#
# Usage:
#   scripts/bench-slug.sh [iterations]          # as this platform behaves
#   REMEMBER_UTF8_STRICT=1 scripts/bench-slug.sh   # as Linux behaves

set -u

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_DIR="${PIPELINE_DIR:-$(cd "$_SCRIPT_DIR/.." && pwd)}"
export PIPELINE_DIR

# shellcheck source=/dev/null
source "$_SCRIPT_DIR/lib-slug.sh"

ITERATIONS="${1:-200}"
PYTHON="${PYTHON:-python3}"

# Fail loudly rather than printing an empty table: a typo'd PYTHON= produced a
# run with no rows at all and exit 0, which reads as "no measurable cost".
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    printf 'bench-slug.sh: PYTHON=%s is not executable\n' "$PYTHON" >&2
    exit 2
fi

_bench() {
    local label="$1" path="$2" start end
    start=$("$PYTHON" -c 'import time; print(time.time())')
    local i=0
    while [ "$i" -lt "$ITERATIONS" ]; do
        session_dir_slug "$path" >/dev/null
        i=$((i + 1))
    done
    end=$("$PYTHON" -c 'import time; print(time.time())')
    "$PYTHON" -c "print(f'  {'$label':<18} {($end - $start) * 1000 / $ITERATIONS:6.2f} ms/call')"
}

printf 'session_dir_slug, %s calls each\n' "$ITERATIONS"
printf '  OSTYPE=%s  REMEMBER_UTF8_STRICT=%s\n\n' \
    "${OSTYPE:-unset}" "${REMEMBER_UTF8_STRICT:-0}"

_bench "ascii" "/Users/dev/Documents/some/project"
_bench "valid non-ascii" "/Users/dev/Documents/café/日本語/🎉"
_bench "ill-formed" "$(printf '/tmp/ab\xe0\xa0cd')"

printf '\nA path is only checked where an ill-formed one can exist (Linux), so on\n'
printf 'other platforms all three rows should be equal — see lib-slug.sh.\n'
