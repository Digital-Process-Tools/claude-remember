"""Entry point for ``python3 -m pipeline``.

Prints usage information listing available submodules. Individual modules
are invoked directly (e.g., ``python3 -m pipeline.extract --session <id>``
or ``python3 -m pipeline.shell extract <id> <dir>``), not through this
dispatcher.

Usage::

    python3 -m pipeline              # prints available modules
    python3 -m pipeline.extract 10   # extract last 10 exchanges
    python3 -m pipeline.shell ...    # shell integration commands
"""

import sys

if len(sys.argv) < 2:
    print("Usage: python3 -m pipeline <module> [args...]", file=sys.stderr)
    print("Modules: extract, haiku, prompts, consolidate", file=sys.stderr)
    sys.exit(1)
