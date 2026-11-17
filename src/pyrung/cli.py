"""Unified ``pyrung`` CLI entry point."""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path


def _find_program(module_path: str) -> tuple:
    """Import a module and find the Program instance.

    Returns ``(program, module)``.

    Accepts ``module:variable`` syntax to select a specific Program
    when a module defines more than one.
    """
    from pyrung.core.program import Program

    variable = None
    if ":" in module_path:
        module_path, variable = module_path.rsplit(":", 1)

    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    mod = importlib.import_module(module_path)

    if variable is not None:
        obj = getattr(mod, variable, None)
        if obj is None or not isinstance(obj, Program):
            msg = f"{module_path} has no Program named '{variable}'."
            raise SystemExit(msg)
        return obj, mod

    found: list[tuple[str, object]] = []
    for attr in sorted(dir(mod)):
        obj = getattr(mod, attr)
        if isinstance(obj, Program):
            found.append((attr, obj))

    if not found:
        msg = (
            f"{module_path} has no Program at module level. Pass the module that "
            "defines your Program, or name it as module:variable."
        )
        raise SystemExit(msg)
    if len(found) > 1:
        names = ", ".join(name for name, _ in found)
        msg = (
            f"Multiple Program instances in {module_path}: {names}\n"
            f"Specify which one with {module_path}:<name>"
        )
        raise SystemExit(msg)
    return found[0][1], mod


def _cmd_check(args: argparse.Namespace) -> None:
    from dataclasses import replace
    from pathlib import Path

    from pyrung.core.validation.config import CheckConfig, find_check_config, load_check_config

    if args.config:
        path = Path(args.config)
        if not path.is_file():
            raise SystemExit(f"No file at {path}. Pass the path of a pyproject.toml to --config.")
        config = load_check_config(path) or CheckConfig()
    else:
        config, _ = find_check_config(Path.cwd())
    if args.select is not None:
        config = replace(config, select=tuple(args.select))
    if args.ignore is not None:
        config = replace(config, ignore=tuple(args.ignore))
    if args.extend_select is not None:
        config = replace(config, extend_select=(*config.extend_select, *args.extend_select))
    selected = config.resolve()
    program, _mod = _find_program(args.module)
    report = program.check(
        select=set(selected),
        dt=args.dt,
    )

    if not report:
        print("No checks selected." if not selected else "No findings.")
        return

    for index, finding in enumerate(report):
        if index:
            print()
        print(finding)
    print()
    print(report.summary())
    if report.has_errors():
        raise SystemExit(1)


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
        description="pyrung: ladder logic in Python",
    )
    sub = parser.add_subparsers(dest="command")

    # -- check --
    check_p = sub.add_parser("check", help="Run static ladder checks")
    check_p.add_argument("module", help="module that defines the Program, as mymod or mymod:name")
    check_p.add_argument("--config", help="Read check settings from this pyproject.toml")
    check_p.add_argument(
        "--extend-select", nargs="+", metavar="RULE", help="Add rule codes or prefixes"
    )
    check_p.add_argument(
        "--select",
        nargs="+",
        metavar="RULE",
        help="Run only these rule codes or categories",
    )
    check_p.add_argument(
        "--ignore",
        nargs="+",
        metavar="RULE",
        help="Skip these rule codes or categories",
    )
    check_p.add_argument("--dt", type=float, default=0.010, help="Scan interval in seconds")
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
