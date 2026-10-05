import sys
if len(sys.argv) < 2:
    print("Usage: python3 -m pipeline <module> [args...]", file=sys.stderr)
    print("Modules: extract, haiku, prompts, consolidate", file=sys.stderr)
    sys.exit(1)
