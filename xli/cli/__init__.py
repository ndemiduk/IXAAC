"""xli CLI package (subcommands + REPL).

The package __init__ is intentionally minimal to avoid eager imports of the
full CLI dependency graph.
"""

from .cli import main
