"""pyrung Debug Adapter Protocol entrypoints."""

from __future__ import annotations

from pyrung.dap.adapter import DAPAdapter

__all__ = ["DAPAdapter", "main"]


def main() -> None:
    """Run the pyrung DAP adapter over stdin/stdout."""
    DAPAdapter().run()
