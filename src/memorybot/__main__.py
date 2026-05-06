"""MemoryBot CLI entry point."""

import sys


BANNER = r"""
  __  __                                 ____        _
 |  \/  | ___ _ __ ___   ___  _ __ _   _| __ )  ___ | |_
 | |\/| |/ _ \ '_ ` _ \ / _ \| '__| | | |  _ \ / _ \| __|
 | |  | |  __/ | | | | | (_) | |  | |_| | |_) | (_) | |_
 |_|  |_|\___|_| |_| |_|\___/|_|   \__, |____/ \___/ \__|
                                   |___/
"""


def main() -> int:
    """Entry point for the `mb` command."""
    print(BANNER)
    print("MemoryBot CLI — coming soon.")
    print()
    print("This is a placeholder release. The full CLI is under construction.")
    print("Learn more: https://www.memorybot.com")
    print()
    print("Installed version: 0.0.2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
