"""Allow `python -m agentseal` as a fallback launcher.

The primary entry point is the `agentseal` console script (installed via
pip). This module enables `python -m agentseal` as a secondary launcher,
which is useful when the Scripts directory isn't on PATH yet.
"""

from .cli import app

if __name__ == "__main__":
    app()
