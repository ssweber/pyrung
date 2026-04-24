"""Unified ``pyrung`` CLI entry point."""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path


def _find_program(module_path: str):
    """Import a module and find the Program instance."""
    from pyrung.core.program import Program

    mod = importlib.import_module(module_path)
    for attr in dir(mod):
        obj = getattr(mod, attr)
        if isinstance(obj, Program):
            return obj
    msg = f"No Program instance found in {module_path}"
    raise SystemExit(msg)


def _cmd_lock(args: argparse.Namespace) -> None:
    from pyrung.core.analysis.verification import (
        Intractable,
        program_hash,
        reachable_states,
        write_lock,
    )

    program = _find_program(args.module)
    lock_path = Path(args.output)

    project = args.project or None
    states = reachable_states(
        program,
        project=project,
        max_depth=args.max_depth,
        max_states=args.max_states,
    )
    if isinstance(states, Intractable):
        print(f"Intractable: {states.reason}", file=sys.stderr)
        raise SystemExit(1)

    projection = project or _default_projection_names(program)
    write_lock(lock_path, states, projection, program_hash(program))
    print(f"Wrote {lock_path} ({len(states)} reachable states)")


def _cmd_check(args: argparse.Namespace) -> None:
    from pyrung.core.analysis.verification import check_lock

    program = _find_program(args.module)
    lock_path = Path(args.lock)

    if not lock_path.exists():
        print(f"Lock file not found: {lock_path}", file=sys.stderr)
        raise SystemExit(1)

    diff = check_lock(
        program,
        lock_path,
        max_depth=args.max_depth,
        max_states=args.max_states,
    )
    if diff is None:
        print("OK — program matches lock file")
    else:
        print("CHANGED — program does not match lock file", file=sys.stderr)
        if diff.added:
            print(f"  {len(diff.added)} new reachable state(s)", file=sys.stderr)
        if diff.removed:
            print(f"  {len(diff.removed)} lost reachable state(s)", file=sys.stderr)
        raise SystemExit(1)


def _default_projection_names(program) -> list[str]:
    from pyrung.core.analysis.verification import _default_projection

    return _default_projection(program)


def _cmd_dap(_args: argparse.Namespace) -> None:
    from pyrung.dap import main as dap_main

    dap_main()


def _cmd_live(_args: argparse.Namespace) -> None:
    from pyrung.dap.live import main as live_main

    sys.argv = ["pyrung live", *_args.rest]
    live_main()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pyrung",
        description="pyrung — ladder logic in Python",
    )
    sub = parser.add_subparsers(dest="command")

    # -- lock --
    lock_p = sub.add_parser("lock", help="Compute reachable states and write pyrung.lock")
    lock_p.add_argument("module", help="Python module containing the Program")
    lock_p.add_argument("-o", "--output", default="pyrung.lock", help="Output path")
    lock_p.add_argument("--project", nargs="*", help="Tags to project onto")
    lock_p.add_argument("--max-depth", type=int, default=50)
    lock_p.add_argument("--max-states", type=int, default=100_000)
    lock_p.set_defaults(func=_cmd_lock)

    # -- check --
    check_p = sub.add_parser("check", help="Verify program matches pyrung.lock")
    check_p.add_argument("module", help="Python module containing the Program")
    check_p.add_argument("--lock", default="pyrung.lock", help="Lock file path")
    check_p.add_argument("--max-depth", type=int, default=50)
    check_p.add_argument("--max-states", type=int, default=100_000)
    check_p.set_defaults(func=_cmd_check)

    # -- dap --
    dap_p = sub.add_parser("dap", help="Run the DAP debug adapter")
    dap_p.set_defaults(func=_cmd_dap)

    # -- live --
    live_p = sub.add_parser("live", help="Attach to a running DAP session")
    live_p.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    live_p.set_defaults(func=_cmd_live)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        raise SystemExit(1)
    args.func(args)
