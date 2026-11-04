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

Parts N and O have memory of their own (``APART_PARTS``), apart from the whole
suite; part O is run with CLICK's fixed scan on, and its export says how.

Regenerate the ClickNick workspace's ``src/plc/`` (for Preview Changes) with::

    make conformance-workspace
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
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
    test_fixed_minute,
    test_fixed_scan,
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
    test_unit_minute,
    test_unit_pairs,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.toml")
#: The layout of the parts with memory of their own (``APART_PARTS``).
APART_MANIFEST_PATH = Path(__file__).with_name("manifest_apart.toml")
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



@dataclass(frozen=True)
class ApartPart:
    """A CLICK project on memory of its own, outside the whole suite's layout.

    Its groups' registers and raw memory are ``manifest_apart.toml`` and
    ``memory.OWNED_APART``: they may repeat the suite's, so it never runs in one project
    with them, and it changes no other part's hash however it grows.  With
    *fixed_scan_ms* a person runs it with CLICK's fixed scan on, at each of those times.
    """

    groups: tuple[str, ...]
    #: The fixed scan times (ms) its questions need, the first to run first.
    fixed_scan_ms: tuple[int, ...] = ()


#: Every group on memory of its own, in the order main calls them.
APART_GROUPS = [
    test_unit_pairs.group,
    # Left out with ``GROUP=unit_pairs``: a minute's wait.
    test_unit_minute.group,
    test_fixed_scan.group,
    # Left out with ``GROUP=fixed_scan``: a minute's wait.
    test_fixed_minute.group,
]

APART_PARTS: dict[str, ApartPart] = {
    # Timers in different units with one delay, Done on one scan: the relation, which
    # holds or not whatever a scan takes.
    "N": ApartPart(("unit_pairs", "unit_minute")),
    # The scan each timer is Done on, counted: only fixed scan mode makes that a number.
    "O": ApartPart(("fixed_scan", "fixed_minute"), fixed_scan_ms=(100, 10, 250)),
}

#: Estimated CLICK steps a part may take (``estimate_steps``): CLICK's 8000,
#: less a margin for the estimate's error and the instructions it has not
#: measured.
PART_STEP_BUDGET = 7500


def groups(part: str | None = None) -> list[Group]:
    """Every group of *part* (``None``: the whole suite), in ``ALL_GROUPS`` order."""
    if part is None:
        return list(ALL_GROUPS)
    if part in APART_PARTS:
        return [g for g in APART_GROUPS if g.name in APART_PARTS[part].groups]
    if part not in PARTS:
        known = ", ".join([*PARTS, *APART_PARTS])
        raise ValueError(f"Unknown part {part!r}; choose one of {known}")
    return [g for g in ALL_GROUPS if g.name in PARTS[part]]


def build(part: str | None = None) -> ConformanceProgram:
    """The program for *part* (``None``: the whole suite) on its manifest.

    A fixed scan part counts its settle time in its longest scan.
    """
    apart = APART_PARTS.get(part) if part is not None else None
    if apart is None:
        return build_program(groups(part), load_manifest(MANIFEST_PATH))
    program = build_program(groups(part), load_manifest(APART_MANIFEST_PATH))
    if apart.fixed_scan_ms:
        return replace(program, scan_s=max(apart.fixed_scan_ms) / 1000)
    return program


def build_apart() -> ConformanceProgram:
    """Every group on memory of its own, as one program, for a local target."""
    return build_program(APART_GROUPS, load_manifest(APART_MANIFEST_PATH))


def build_for(endpoint: Endpoint | None, names: list[str] | None = None) -> ConformanceProgram:
    """The build loaded at *endpoint*: the part, or the whole suite, whose layout
    hash is in its ``DS1``.  A local target (``None``) runs the whole suite, or the
    groups on memory of their own when *names* asks for those.

    Raises:
        BuildMismatchError: ``DS1`` holds no build of the suite's layout hash.
    """
    if endpoint is None:
        apart = {g.name for g in APART_GROUPS}
        return build_apart() if names and apart.issuperset(names) else build()
    found = read_program_hash(endpoint)
    known: list[str] = []
    for part in [*PARTS, *APART_PARTS, None]:
        candidate = build(part)
        if candidate.program_hash == found:
            return candidate
        known.append(f"{part or 'whole'} {candidate.program_hash}")
    raise BuildMismatchError(
        f"{endpoint} runs program layout {found}; the suite is layout {', '.join(known)}. "
        f"Export the part again (make conformance-export "
        f"PART={'|'.join([*PARTS, *APART_PARTS])}) and load it."
    )


def fixed_scan_note(part: str | None) -> str | None:
    """What a person sets in CLICK before running *part*; ``None`` when it needs nothing."""
    times = APART_PARTS[part].fixed_scan_ms if part in APART_PARTS else ()
    if not times:
        return None
    each = ", ".join(f"{ms} ms" for ms in times)
    return (
        f"Part {part} runs in fixed scan mode: in the CLICK software turn fixed scan on "
        f"(SC202) and set the scan time (SD13), write the project to the PLC, and run "
        f"`make test-conformance PLC=...` at each of {each}. "
        "See tests/conformance/AGENTS.md, \"Fixed scan parts\"."
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
        choices=sorted([*PARTS, *APART_PARTS]),
        help="only that part's groups, as its own CLICK project (default: the whole suite)",
    )
    args = parser.parse_args(argv)
    program = build(args.part)
    label = f"part {args.part}, layout" if args.part else "layout"
    note = fixed_scan_note(args.part)

    if args.workspace is None:
        out = program.export(args.out_dir)
        print(f"Wrote {out} ({label} {program.program_hash} in DS1)")
        _print_layout(program)
        if note is not None:
            print(note)
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
    if note is not None:
        print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
