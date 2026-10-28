"""The whole conformance suite: every group, on the checked-in manifest.

Export the CLICK project (ladder CSVs and nicknames, for Guided Paste) with::

    make conformance-export OUT=path/to/dir

Regenerate the ClickNick workspace's ``src/plc/`` (for Preview Changes) with::

    make conformance-workspace
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pyrung.conformance import ConformanceProgram, build_program, load_manifest
from tests.conformance import (
    test_block,
    test_coils,
    test_drum,
    test_loop,
    test_math,
    test_out,
    test_rung_semantics,
    test_text,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.toml")
#: The ClickNick workspace linked to the conformance CLICK project.
WORKSPACE_PATH = Path(__file__).with_name("workspace")

#: Every group in the suite.  A new case module adds its ``group`` here.
ALL_GROUPS = [
    test_rung_semantics.group,
    test_math.group,
    test_drum.group,
    test_out.group,
    test_loop.group,
    test_coils.group,
    test_text.group,
    test_block.group,
]


def build() -> ConformanceProgram:
    return build_program(ALL_GROUPS, load_manifest(MANIFEST_PATH))


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
    args = parser.parse_args(argv)
    program = build()

    if args.workspace is None:
        out = program.export(args.out_dir)
        print(f"Wrote {out} (layout {program.program_hash} in DS1)")
        _print_layout(program)
        return 0

    workspace = Path(args.workspace)
    source = program.export_workspace(workspace)
    print(f"Wrote {source} (layout {program.program_hash} in DS1)")
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
