"""Meeting Notetaker -- thin entry point."""
from __future__ import annotations

import sys

from meeting_notetaker.app import main


if __name__ == "__main__":
    sys.exit(main())
