"""The whole conformance suite: every group, on the checked-in manifest.

Export the CLICK project (ladder CSVs and nicknames, for Guided Paste) with::

    make conformance-export OUT=path/to/dir

The whole suite is more than CLICK's 8000 steps, so a CLICK loads it as six
projects, one part each (``PARTS``; there is no part D)::

    make conformance-export PART=A OUT=path/to/a
    make conformance-export PART=B OUT=path/to/b
    make conformance-export PART=C OUT=path/to/c
    make conformance-export PART=E OUT=path/to/e
    make conformance-export PART=F OUT=path/to/f
    make conformance-export PART=G OUT=path/to/g

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
    test_cont_box,
    test_counters,
    test_drum,
    test_drum_close,
    test_drum_idle,
    test_drum_jump,
    test_drum_rule,
    test_drum_settle,
    test_drum_trace,
    test_faults,
    test_flag_probe,
    test_for_count,
    test_loop,
    test_loop_call,
    test_math,
    test_math_error,
    test_numeric,
    test_out,
    test_past_bank_write,
    test_paste_risk,
    test_ret_branch,
    test_row_solve,
    test_rung_semantics,
    test_search,
    test_shift,
    test_solve_order,
    test_store_probe,
    test_system,
    test_text,
    test_timers,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.toml")
#: Cases that passed on a real CLICK, as they are now (``record_confirmations``).
CONFIRMED_PATH = Path(__file__).with_name("confirmed.toml")
#: The ClickNick workspace linked to the conformance CLICK project.
WORKSPACE_PATH = Path(__file__).with_name("workspace")

#: Every group in the suite.  A new case module adds its ``group`` here.
ALL_GROUPS = [
    # First, so its fault-flag cases read SC40/SC43/SC44 before any other
    # group can raise them on the same scan.
    test_system.group,
    # Right after system: its probes read SC40/SC43/SC44/SC46 after its own
    # instructions, so only system's can raise them first on a scan.
    test_faults.group,
    test_rung_semantics.group,
    test_math.group,
    test_drum.group,
    test_drum_trace.group,
    test_drum_rule.group,
    test_drum_jump.group,
    test_drum_settle.group,
    test_drum_close.group,
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
    test_ret_branch.group,
    test_loop_call.group,
    test_row_solve.group,
    test_cont_box.group,
    test_for_count.group,
    test_paste_risk.group,
    # A drum in a For body may be refused at paste: after the groups CLICK has run.
    test_drum_idle.group,
    # Last, so no other group's rung reads SC46 on a scan a Math error raised it.
    test_math_error.group,
    # After every group: its probes raise SC43 and SC46 on purpose, and a flag
    # stays up to the end of the scan, so any group after it that reads a flag
    # would see them (text/to_binary read SC43 on a scan a store probe raised it).
    test_store_probe.group,
    # After the store probes too: they raise flags on purpose (and leave them up to
    # the end of the scan), so no group that reads one may run after them.
    test_flag_probe.group,
    # The last group: it writes past the DD bank on a CLICK and may be left out.
    test_past_bank_write.group,
]

#: Group names per CLICK project, split to keep each part under
#: ``PART_STEP_BUDGET``: A and B the instructions, C how a rung is solved
#: (solve order, edges, loops) with the paste probes, E the drum traces and the
#: fault-flag probes, G the S6 drum probes.  Every group is in exactly one part.  The registers
#: are the manifest's either way, so a group's subroutine is the same in its
#: part as in the whole project.
PARTS: dict[str, tuple[str, ...]] = {
    "A": (
        "block",
        "counters",
        "drum",
        "math",
        "math_error",
        "numeric",
    ),
    "B": ("out", "search", "shift", "system", "text", "timers"),
    # Groups run in ``ALL_GROUPS`` order, so main calls them in that order: the
    # groups no CLICK has run yet and that it may refuse at paste (``cont_box``,
    # a box on a continued wire; ``paste_risk``, operands it may not take) are
    # last, so a refused group's subroutine and its call rung are the last to
    # delete.  ``loop_call`` runs its rungs in main, after every call rung.
    "C": (
        "call_twice",
        "coils",
        "loop",
        "row_solve",
        "rungs",
        "solve_order",
        "ret_branch",
        "loop_call",
        "cont_box",
        "paste_risk",
        "for_count",
    ),
    # A drum's state scan by scan and the fault-flag probes, which outgrew
    # the budget left in parts A and C.
    "E": ("drum_trace", "faults"),
    # The S6 drum probes: the drum rule on time and time & event drums, out of
    # part E when ``drum_settle`` left it no room.  ``drum_idle`` (a drum not
    # reached; a For body may be refused at paste) is the part's last group.
    "G": ("drum_rule", "drum_jump", "drum_settle", "drum_close", "drum_idle"),
    # The S6 store probes (RAND bounds and destinations, Inf and NaN into text
    # and Words, a Real into a TXT, a clamping Fill): a project of their own,
    # since part E has no room left and the editor may refuse a RAND bound form
    # at paste (``rand_bound_forms``, the group's last case).
    # The fault-flag probes (flag_probe) follow, and ``past_write``, which
    # writes past the DD bank on a CLICK, is the project's last group so it can be
    # left out: nothing depends on it.
    "F": ("store_probe", "flag_probe", "past_write"),
}

#: Estimated CLICK steps a part may take (``estimate_steps``): CLICK's 8000,
#: less a margin for the estimate's error and the instructions it has not
#: measured.
PART_STEP_BUDGET = 7500


def groups(part: str | None = None) -> list[Group]:
    """Every group of *part* (``None``: the whole suite), in ``ALL_GROUPS`` order."""
    if part is None:
        return list(ALL_GROUPS)
    if part not in PARTS:
        raise ValueError(f"Unknown part {part!r}; choose one of {', '.join(PARTS)}")
    return [g for g in ALL_GROUPS if g.name in PARTS[part]]


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
        f"Export the part again (make conformance-export PART={'|'.join(PARTS)}) and load it."
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
