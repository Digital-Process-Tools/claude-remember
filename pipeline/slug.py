from __future__ import annotations
import os
SLUG_MAX = 200
_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
def _utf16_code_units(text: str) -> list[int]:
    raw = text.encode("utf-16-le", "surrogatepass")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]
def _to_base36(value: int) -> str:
    if value == 0:
        return "0"
    out = []
    while value:
        value, digit = divmod(value, 36)
        out.append(_BASE36_DIGITS[digit])
    return "".join(reversed(out))
def path_hash(path: str) -> str:
    acc = 0
    for unit in _utf16_code_units(path):
        acc = (acc * 31 + unit) & 0xFFFFFFFF
    if acc >= 0x80000000:
        acc -= 0x100000000
    return _to_base36(abs(acc))
def _fold_drive_letter(path: str) -> str:
    if len(path) >= 2 and path[1] == ":" and "A" <= path[0] <= "Z":
        return path[0].lower() + path[1:]
    return path
def session_dir_slug(path: str) -> str:
    path = _fold_drive_letter(path)
    slug = "".join(
        c if c.isascii() and c.isalnum() else "-" * (2 if ord(c) > 0xFFFF else 1)
        for c in path
    )
    if len(slug) <= SLUG_MAX:
        return slug
    return slug[:SLUG_MAX] + "-" + path_hash(path)
def main(argv: list[str]) -> int:
    args = argv[1:]
    want_hash = False
    if args and args[0] == "--hash":
        want_hash = True
        args = args[1:]
    if len(args) != 1:
        print("usage: python3 -m pipeline.slug [--hash] <path>")
        return 2
    raw = args[0]
    try:
        raw = os.fsencode(raw).decode("utf-8", "replace")
    except (UnicodeError, ValueError):
        pass
    print(path_hash(raw) if want_hash else session_dir_slug(raw))
    return 0
if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))
