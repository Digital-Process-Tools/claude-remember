"""A stand-in for MSYS/Cygwin `cygpath`, in one place.

`session_dir_slug` calls `cygpath -w` before slugging, so every test that wants
to exercise the Windows half needs one — and there is no Windows and no cygpath
on the runners. Three copies of this string had started to accumulate
(test_windows_drive_slug_263.py, test_windows_long_path_slug_294.py, and now the
conformance vectors), which is the same drift the vectors exist to delete. One
copy, imported.

What it models, and only this:

* `-w` converts separators and folds `/c/…`, `/cygdrive/c/…` and `c:\\…` to the
  native `C:\\…` form with an UPPER-case drive, because that is what real
  `cygpath -w` does — the lower-casing is `session_dir_slug`'s own job (#263);
* past MAX_PATH the result carries the Win32 long-path prefix, `\\\\?\\` for a
  drive path and `\\\\?\\UNC\\` for a UNC one (#294);
* `CYGPATH_STUB_EMPTY=1` makes it exit 0 and print nothing, the failure mode
  that emptied the slug outright.

It is a MODEL, not a proof. The reporter's evidence is that real `cygpath -w`
emits the prefix past MAX_PATH; that cannot be executed here.
"""

from __future__ import annotations

CYGPATH_STUB = r"""#!/usr/bin/env bash
if [ "$1" = "-w" ]; then
    if [ "${CYGPATH_STUB_EMPTY:-0}" = "1" ]; then
        exit 0
    fi
    p="$2"
    p="${p//\//\\}"
    case "$p" in
        \\\\*) ;;
        \\cygdrive\\?\\*)
            d=$(printf '%s' "${p:10:1}" | tr 'a-z' 'A-Z')
            p="$d:\\${p:12}" ;;
        \\?\\*)
            d=$(printf '%s' "${p:1:1}" | tr 'a-z' 'A-Z')
            p="$d:\\${p:3}" ;;
        ?:\\*)
            d=$(printf '%s' "${p:0:1}" | tr 'a-z' 'A-Z')
            p="$d:\\${p:3}" ;;
    esac
    if [ "${#p}" -ge 260 ]; then
        case "$p" in
            \\\\*) p="\\\\?\\UNC\\${p:2}" ;;
            *)     p="\\\\?\\$p" ;;
        esac
    fi
    printf '%s\n' "$p"
    exit 0
fi
printf '%s\n' "$2"
"""
