"""Launcher for the gitsvn command-line application."""

import sys

from gitsvn import GitSvn, GitSvnError
from gitsvn.cli import main


if __name__ == "__main__":
    sys.exit(main())
