"""Allow ``python -m lanpilot``."""

import sys

from lanpilot.cli import main

if __name__ == "__main__":
    sys.exit(main())
