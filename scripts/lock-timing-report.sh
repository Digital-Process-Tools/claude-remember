#!/usr/bin/env bash

set -u

FILE="${1:-}"
if [ -z "$FILE" ]; then
    if [ -n "${REMEMBER_LOCK_TIMING_FILE:-}" ]; then
        FILE="$REMEMBER_LOCK_TIMING_FILE"
    elif [ -n "${REMEMBER_DIR:-}" ]; then
        FILE="${REMEMBER_DIR}/logs/lock-timing.tsv"
    fi
fi

HOWTO="set REMEMBER_LOCK_TIMING=1 in the environment Claude Code launches hooks from, work normally for a day, then re-run this"

if [ -z "$FILE" ]; then
    echo "lock-timing: skipped -- no file given and neither REMEMBER_LOCK_TIMING_FILE nor REMEMBER_DIR is set"
    echo "             $HOWTO"
    exit 2
fi

if [ ! -f "$FILE" ]; then
    echo "lock-timing: skipped -- no file at $FILE"
    echo "             The recorder is off by default: $HOWTO"
    exit 2
fi

if [ ! -r "$FILE" ]; then
    echo "lock-timing: skipped -- $FILE exists but is not readable"
    exit 2
fi

RECORDS=$(grep -c -v '^#' "$FILE" 2>/dev/null || echo 0)
if [ -z "$RECORDS" ] || [ "${RECORDS#*[!0-9]}" != "$RECORDS" ]; then
    RECORDS=0
fi

if [ "$RECORDS" -eq 0 ]; then
    echo "lock-timing: skipped -- $FILE has no records yet (REMEMBER_LOCK_TIMING=1 was set, but no lock was taken while it was)"
    exit 2
fi

CAPPED=""
if [ -e "${FILE}.capped" ] || grep -q '^# CAPPED' "$FILE" 2>/dev/null; then
    CAPPED="yes"
fi

printf 'lock-timing: ok  file=%s  records=%s\n' "$FILE" "$RECORDS"
if [ -n "$CAPPED" ]; then
    printf 'lock-timing: CAPPED -- recording stopped at REMEMBER_LOCK_TIMING_MAX. Every record here is real; the tail after the cap is missing, so read the max below as a floor, not as the maximum.\n'
fi
echo ""

awk '
BEGIN { FS = "\t" }
function pct(arr, n, q,   i) {
    i = int(q * n + 0.999999);
    if (i < 1) i = 1;
    if (i > n) i = n;
    return arr[i];
}
/^#/ { next }
NF < 8 { malformed++; next }
{
    lock = $2; event = $3; outcome = $4;
    prec[lock] = $7;
    if (!(lock in seen)) { seen[lock] = 1; order[++locks] = lock }
    if ($5 != "-") { wn[lock]++; w[lock "\001" wn[lock]] = $5 + 0 }
    if (event == "release" && outcome == "ok" && $6 != "-") {
        hn[lock]++; h[lock "\001" hn[lock]] = $6 + 0
    }
    if (event == "acquire" && outcome == "timeout") to[lock]++
    if (outcome == "unpaired") up[lock]++
}
END {
    printf "%-14s %5s %5s %9s %9s %9s %9s %9s %9s %9s %9s %8s\n",
        "lock", "prec", "n", "held_p50", "held_p90", "held_p99", "held_max",
        "wait_p50", "wait_p90", "wait_p99", "wait_max", "timeouts";
    for (k = 1; k <= locks; k++) {
        lock = order[k];
        n = hn[lock] + 0; m = wn[lock] + 0;
        delete hs; delete ws;
        for (i = 1; i <= n; i++) {
            v = h[lock "\001" i];
            for (j = i - 1; j >= 1 && hs[j] > v; j--) hs[j + 1] = hs[j];
            hs[j + 1] = v;
        }
        for (i = 1; i <= m; i++) {
            v = w[lock "\001" i];
            for (j = i - 1; j >= 1 && ws[j] > v; j--) ws[j + 1] = ws[j];
            ws[j + 1] = v;
        }
        printf "%-14s %5s %5d %9s %9s %9s %9s %9s %9s %9s %9s %8d\n",
            lock, prec[lock], n,
            n ? pct(hs, n, 0.50) : "-", n ? pct(hs, n, 0.90) : "-",
            n ? pct(hs, n, 0.99) : "-", n ? hs[n] : "-",
            m ? pct(ws, m, 0.50) : "-", m ? pct(ws, m, 0.90) : "-",
            m ? pct(ws, m, 0.99) : "-", m ? ws[m] : "-",
            to[lock] + 0;
        if (up[lock] + 0 > 0)
            printf "  note: %d %s release(s) had no matching acquire in the same process -- duration unknown, not counted\n", up[lock], lock;
    }
    if (malformed + 0 > 0)
        printf "  note: %d malformed line(s) skipped\n", malformed;
    print "";
    print "held_ms = acquire..release. For save.lock this includes the summarize Haiku call (#226).";
    print "timeouts = waits that ran out. For save.lock those are NDC commits that skipped and duplicated a span.";
}
' "$FILE"
