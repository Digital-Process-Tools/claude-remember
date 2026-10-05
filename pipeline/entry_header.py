from __future__ import annotations
import re
TIME_ERE = r"([0-9]{2}:[0-9]{2}|[0-9]{1,2}:[0-9]{2} (AM|PM))"
ENTRY_HEADER_ERE = rf"^## {TIME_ERE} \|"
ANY_HEADER_ERE = rf"^## ({TIME_ERE} \||Week of |[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})"
_ENTRY = re.compile(ENTRY_HEADER_ERE, re.MULTILINE)
_ANY = re.compile(ANY_HEADER_ERE, re.MULTILINE)
def is_entry_header(line: str) -> bool:
    return _ENTRY.search(line) is not None
def contains_header(text: str) -> bool:
    return _ANY.search(text) is not None
def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--ere":
        if argv[2] == "entry":
            print(ENTRY_HEADER_ERE)
            return 0
        if argv[2] == "any":
            print(ANY_HEADER_ERE)
            return 0
    print("usage: python3 -m pipeline.entry_header --ere {entry|any}")
    return 2
if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))
