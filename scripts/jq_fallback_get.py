import json
import sys
def main() -> None:
    try:
        with open(sys.argv[1]) as f:
            data = json.load(f)
        path_parts = sys.argv[2].strip(".").split(".")
        val = data
        for k in path_parts:
            if k and isinstance(val, dict):
                val = val.get(k)
            if val is None:
                break
        if val is None:
            return
        print(val if isinstance(val, str) else json.dumps(val))
    except Exception:
        return
if __name__ == "__main__":
    main()
