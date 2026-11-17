"""The one wording for a check that fails only when pyrung itself has a bug."""

from __future__ import annotations


def internal_error(detail: str) -> str:
    """Return the message for a failed internal check, with *detail* kept for the bug report.

    Without the wrapper, a user reads the bare invariant as a fault in their own program.
    """
    return (
        f"Internal error in pyrung: {detail.rstrip('.')}. This is a bug in pyrung. "
        "Please report it."
    )
