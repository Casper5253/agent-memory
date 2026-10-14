"""Entry point used by hooks, the OpenCode plugin and the `mem` shim. Run with `python -I run.py ...`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentmem.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
