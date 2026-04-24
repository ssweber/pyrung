"""Unified ``pyrung`` CLI entry point."""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path


def _find_program(module_path: str):
    """Import a module and find the Program instance.

    Accepts ``module:variable`` syntax to select a specific Program
    when a module defines more than one.
    """
    from pyrung.core.program import Program

    variable = None
    if ":" in module_path:
        module_path, variable = module_path.rsplit(":", 1)

    mod = importlib.import_module(module_path)

    if variable is not None:
        obj = getattr(mod, variable, None)
        if obj is None or not isinstance(obj, Program):
            msg = f"'{variable}' in {module_path} is not a Program instance"
            raise SystemExit(msg)
        return obj

    found: list[tuple[str, object]] = []
    for attr in sorted(dir(mod)):
        obj = getattr(mod, attr)
        if isinstance(obj, Program):
            found.append((attr, obj))

    if not found:
        msg = f"No Program instance found in {module_path}"
        raise SystemExit(msg)
    if len(found) > 1:
        names = ", ".join(name for name, _ in found)
        msg = (
            f"Multiple Program instances in {module_path}: {names}\n"
            f"Specify which one with {module_path}:<name>"
        )
        raise SystemExit(msg)
    return found[0][1]


def _cmd_lock(args: argparse.Namespace) -> None:
    from pyrung.core.analysis.prove import (
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

    if project is None:
        from pyrung.core.analysis.pdg import build_program_graph

        graph = build_program_graph(program)
        public = [name for name, tag in graph.tags.items() if tag.public]
        if public:
            print(f"Projecting to public tags: {', '.join(sorted(public))}", file=sys.stderr)
        else:
            print(
                f"No public tags — projecting to terminals: {', '.join(projection)}",
                file=sys.stderr,
            )

    write_lock(lock_path, states, projection, program_hash(program))
    print(f"Wrote {lock_path} ({len(states)} reachable states)")


def _cmd_check(args: argparse.Namespace) -> None:
    from pyrung.core.analysis.prove import check_lock

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
    from pyrung.core.analysis.prove import _default_projection

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
