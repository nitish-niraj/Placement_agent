"""PIA shared domain package — the single source for enums, state machines, and utilities.

Mirrors docs/05_Backend_Schema.md §1–§2. Both apps and migrations consume from here;
never re-declare an enum value locally (master plan convention: IDs and values are frozen).
"""

__all__ = ["enums", "states"]
__version__ = "0.1.0"
