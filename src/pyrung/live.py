"""Drive a running pyrung debug session from Python.

The pyrung debug adapter listens for console commands on a localhost port
while it runs a program. These functions send the adapter the same commands
that the ``pyrung live`` CLI and the Debug Console accept::

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
