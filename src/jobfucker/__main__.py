"""Support ``python -m jobfucker`` (and the ``poe app`` task).

Delegates to the typer CLI entry point so the console script ``jobfucker`` and
``python -m jobfucker`` share one implementation.
"""

from __future__ import annotations

from jobfucker.cli import main

if __name__ == "__main__":
    main()
