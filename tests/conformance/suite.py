"""The whole conformance suite: every group, on the checked-in manifest.

Export the CLICK project (ladder CSVs and nicknames, for Guided Paste) with::

    make conformance-export OUT=path/to/dir

The whole suite is more than CLICK's 8000 steps, so a CLICK loads it as two
projects, one part each (``PARTS``)::

    make conformance-export PART=A OUT=path/to/a
    make conformance-export PART=B OUT=path/to/b

Each part's main program writes its own layout hash to ``DS1``, and a run
against a real PLC reads it to pick that part's groups (``build_for``).

Regenerate the ClickNick workspace's ``src/plc/`` (for Preview Changes) with::

    make conformance-workspace
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pyrung.conformance import (
    BuildMismatchError,
    ConformanceProgram,
    Endpoint,
    Group,
    build_program,
    load_manifest,
    read_program_hash,
)
from tests.conformance import (
    test_block,
    test_call_twice,
    test_coils,
    test_counters,
    test_drum,
    test_loop,
    test_math,
    test_math_error,
    test_numeric,
    test_out,
    test_rung_semantics,
    test_search,
    test_shift,
    test_solve_order,
    test_system,
    test_text,
    test_timers,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.toml")
#: The ClickNick workspace linked to the conformance CLICK project.
WORKSPACE_PATH = Path(__file__).with_name("workspace")

#: Every group in the suite.  A new case module adds its ``group`` here.
ALL_GROUPS = [
    # First, so its fault-flag cases read SC40/SC43/SC44 before any other
    # group can raise them on the same scan.
    test_system.group,
    test_rung_semantics.group,
    test_math.group,
    test_drum.group,
    test_out.group,
    test_loop.group,
    test_coils.group,
    test_text.group,
    test_block.group,
    test_shift.group,
    test_solve_order.group,
    test_search.group,
    test_counters.group,
    test_numeric.group,
    test_timers.group,
    test_call_twice.group,
]

#: Groups whose case stops pyrung's PLC (a math error), though a CLICK keeps
#: running (pending NOSTOP, see ``test_math_error.py``).  A run against a CLICK
#: takes them with every other group; a run on one of pyrung's own targets
#: gives each a fresh target of its own, since nothing could reset after it.
#: Once pyrung keeps running they join ``ALL_GROUPS`` and this list goes away.
STOP_GROUPS = [test_math_error.group]

#: Group names per CLICK project, split by subroutine name (A: ``Conf_Block``..
#: ``Conf_Numeric``, B: ``Conf_Out``..``Conf_Timers``) to keep each part under
#: CLICK's 8000 steps.  Every group is in exactly one part.  The registers are
#: the manifest's either way, so a group's subroutine is the same in its part
#: as in the whole project.
PARTS: dict[str, tuple[str, ...]] = {
    "A": (
        "block",
        "call_twice",
        "coils",
        "counters",
        "drum",
        "loop",
        "math",
        *(g.name for g in STOP_GROUPS),
        "numeric",
    ),
    "B": ("out", "rungs", "search", "shift", "solve_order", "system", "text", "timers"),
}


def wire_groups(build: ConformanceProgram, endpoint: Endpoint | None) -> list[Group]:
    """The groups of *build* a run without ``GROUP=`` takes at *endpoint*.

    Every loaded group against a real PLC; on one of pyrung's own targets
    (``None``) every group but ``STOP_GROUPS``, which run alone.
    """
    every = ALL_GROUPS if endpoint is None else [*ALL_GROUPS, *STOP_GROUPS]
    return [g for g in every if g.name in build.names]


def groups(part: str | None = None) -> list[Group]:
    """Every group of *part* (``None``: the whole suite), in ``ALL_GROUPS`` order."""
    every = [*ALL_GROUPS, *STOP_GROUPS]
    if part is None:
        return every
    if part not in PARTS:
        raise ValueError(f"Unknown part {part!r}; choose one of {', '.join(PARTS)}")
    return [g for g in every if g.name in PARTS[part]]


def build(part: str | None = None) -> ConformanceProgram:
    """The program for *part* (``None``: the whole suite) on the checked-in manifest."""
    return build_program(groups(part), load_manifest(MANIFEST_PATH))


def build_for(endpoint: Endpoint | None) -> ConformanceProgram:
    """The build loaded at *endpoint*: the part, or the whole suite, whose layout
    hash is in its ``DS1``.  A local target (``None``) runs the whole suite.

    Raises:
        BuildMismatchError: ``DS1`` holds no build of the suite's layout hash.
    """
    if endpoint is None:
        return build()
    found = read_program_hash(endpoint)
    known: list[str] = []
    for part in [*PARTS, None]:
        candidate = build(part)
        if candidate.program_hash == found:
            return candidate
        known.append(f"{part or 'whole'} {candidate.program_hash}")
    raise BuildMismatchError(
        f"{endpoint} runs program layout {found}; the suite is layout {', '.join(known)}. "
        "Export the part again (make conformance-export PART=A or PART=B) and load it."
    )


def _print_layout(program: ConformanceProgram) -> None:
    for g in program.groups:
        print(
            f"  {g.subroutine}: hash {g.group_hash} in DS{g.hash_register}, "
            f"{len(g.cases)} case(s), slots DS{g.register(1)}..DS{g.layout.last_register}"
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.conformance.suite",
        description="Export the conformance program as CLICK ladder CSVs, or as pyrung "
        "source into a ClickNick workspace.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("out_dir", nargs="?", help="write main.csv, subroutines/, nicknames.csv")
    target.add_argument(
        "--workspace",
        nargs="?",
        const=str(WORKSPACE_PATH),
        metavar="DIR",
        help=f"replace DIR/src/plc (default {WORKSPACE_PATH})",
    )
    parser.add_argument(
        "--part",
        choices=sorted(PARTS),
        help="only that part's groups, as its own CLICK project (default: the whole suite)",
    )
    args = parser.parse_args(argv)
    program = build(args.part)
    label = f"part {args.part}, layout" if args.part else "layout"

    if args.workspace is None:
        out = program.export(args.out_dir)
        print(f"Wrote {out} ({label} {program.program_hash} in DS1)")
        _print_layout(program)
        return 0

    workspace = Path(args.workspace)
    source = program.export_workspace(workspace)
    print(f"Wrote {source} ({label} {program.program_hash} in DS1)")
    _print_layout(program)
    if not (workspace / "csv").is_dir():
        print(
            f"Note: {workspace / 'csv'} is missing, so ClickNick has no CLICK snapshot to "
            "diff against yet. Link this folder as the workspace in ClickNick first "
            "(see its README.md)."
        )
    print(
        "Next, in ClickNick: Preview Changes -> Copy to Click -> paste in CLICK -> save -> "
        "write to the PLC. New nicknames: clicknick-cli tag apply."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
