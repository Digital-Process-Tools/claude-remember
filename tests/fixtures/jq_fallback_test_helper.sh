#!/bin/bash
# Test helper: define _jq_fallback directly without depending on jq presence.
PYTHON="${PYTHON:-python3}"
_jq_fallback() {
    local _jq_flags=""
    while [[ "$1" == -* ]]; do _jq_flags="$_jq_flags $1"; shift; done
    local _jq_query="$1"
    local _jq_file="$2"
    $PYTHON - "$_jq_file" "$_jq_query" 2>/dev/null << 'PYFALLBACK'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    keys = sys.argv[2].strip('.').split('.')
    val = data
    for k in keys:
        if k and isinstance(val, dict):
            val = val.get(k)
        if val is None:
            break
    if val is None:
        sys.exit(0)
    print(val if isinstance(val, (str, int, float, bool)) else json.dumps(val))
except Exception:
    sys.exit(0)
PYFALLBACK
}
