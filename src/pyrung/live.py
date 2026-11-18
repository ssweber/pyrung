"""Drive a running pyrung debug session from Python.

When the pyrung debug adapter launches a program, it listens for console
commands on a localhost port. These functions send it the same commands that
the ``pyrung live`` CLI and the Debug Console accept::

    from pyrung.live import list_sessions, send_command

    sessions = list_sessions()          # ["logic"]
    ok, text = send_command("logic", "step 5")
"""

from pyrung.dap.grammar import command_grammar
from pyrung.dap.live import list_sessions, send_command

__all__ = [
    "command_grammar",
    "list_sessions",
    "send_command",
]
