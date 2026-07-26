"""``python -m hop`` — the same entry point as the installed ``hop`` command.

Worth having its own file: someone who has cloned the repository rather than
installed the package has no ``hop`` on their PATH, and telling them to run
``python -m hop`` is shorter than explaining pip.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
