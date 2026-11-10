"""The whole conformance suite: every group, on the checked-in manifest.

Export the CLICK project (ladder CSVs and nicknames, for Guided Paste) with::

    make conformance-export OUT=path/to/dir

The whole suite is more than CLICK's 8000 steps, so a CLICK loads it as nine
projects, one part each, named for what they cover (``PARTS``)::

    make conformance-export PART=coils OUT=path/to/coils
    make conformance-export PART=copy OUT=path/to/copy
    make conformance-export PART=math OUT=path/to/math
    make conformance-export PART=drum1 OUT=path/to/drum1  (and drum2, drum3)
    make conformance-export PART=control OUT=path/to/control
    make conformance-export PART=system OUT=path/to/system
    make conformance-export PART=boot OUT=path/to/boot

Each part's main program writes its own layout hash to ``DS1``, and a run
against a real PLC reads it to pick that part's groups (``build_for``).

The parts ``units``, ``fixed_scan``, ``text_flasher``, ``follow_flags``, ``follow_values``,
``follow_timing``, ``follow_extra``, the ``follow2_*`` parts and ``modbus_raw`` have memory of
their own (``APART_PARTS``), apart from the whole suite;
``fixed_scan`` is run with CLICK's fixed scan on, and its export says how.

Regenerate the ClickNick workspace's ``src/plc/`` (for Preview Changes) with::

    make conformance-workspace
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
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
from tests.conformance import test_modbus_raw
from tests.conformance.coils import (
    test_coil_phase,
    test_coil_range,
    test_coil_timing,
    test_coils,
    test_deb_zero,
    test_flasher_off,
    test_flasher_td,
    test_out,
    test_out_more,
    test_td_write,
)
from tests.conformance.conditions import (
    test_compare,
    test_text_compare,
)
from tests.conformance.copy import (
    test_block,
    test_copy_more,
    test_pack_point,
    test_past_bank_write,
    test_past_clean,
    test_real_bits,
    test_real_text,
    test_text,
    test_txt_end,
    test_word_floor,
    test_word_store,
)
from tests.conformance.drum_shift_search import (
    test_bad_jump,
    test_drum,
    test_drum_close,
    test_drum_ds,
    test_drum_idle,
    test_drum_jump,
    test_drum_pins,
    test_drum_preset,
    test_drum_rule,
    test_drum_settle,
    test_drum_trace,
    test_search,
    test_search_more,
    test_shift,
)
from tests.conformance.math import (
    test_agg_more,
    test_math,
    test_math_error,
    test_math_more,
    test_math_terms,
    test_numeric,
    test_real_sum,
    test_store_probe,
    test_term_zero,
)
from tests.conformance.program_control import (
    test_call_more,
    test_call_twice,
    test_for_count,
    test_for_dd_minus,
    test_for_more,
    test_for_signed,
    test_loop,
    test_loop_call,
    test_ret_branch,
)
from tests.conformance.rungs import (
    test_cont_box,
    test_paste_risk,
    test_row_solve,
    test_rung_semantics,
    test_solve_order,
)
from tests.conformance.system import (
    test_boot_read,
    test_faults,
    test_flag_carry,
    test_flag_more,
    test_flag_probe,
    test_flag_steps,
    test_system,
)
from tests.conformance.timers_counters import (
    test_counters,
    test_fixed_scan,
    test_reset_pins,
    test_timers,
    test_tof_rewrite,
    test_unit_minute,
    test_unit_pairs,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.toml")
#: The layout of the parts with memory of their own (``APART_PARTS``).
APART_MANIFEST_PATH = Path(__file__).with_name("manifest_apart.toml")
#: Cases that passed on a real CLICK, as they are now (``record_confirmations``).
CONFIRMED_PATH = Path(__file__).with_name("confirmed.toml")
#: What the probes with no answer yet read on a real CLICK (``probe_kit.record_readings``).
READINGS_PATH = Path(__file__).with_name("readings.toml")
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
    # The grey-area probes (probe_kit.py): each reads a flag right after its own op, or
    # waits for a scan it starts off (SC46), so nothing they raise reaches a group above.
    test_word_store.group,
    test_real_bits.group,
    test_real_sum.group,
    test_drum_pins.group,
    test_drum_pins.branch_group,
    test_drum_pins.limits_group,
    test_math_more.group,
    test_math_more.hex_group,
    test_agg_more.group,
    test_copy_more.group,
    test_copy_more.block_group,
    test_out_more.group,
    test_for_more.group,
    test_call_more.group,
    test_boot_read.boot_group,
    test_boot_read.rtc_group,
    test_boot_read.wide_group,
    test_coil_range.group,
    test_compare.group,
    test_boot_read.flag_group,
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

#: Group names per CLICK project, by instruction chapter (``docs/instructions/``), split to
#: keep each part under ``PART_STEP_BUDGET``.  Every group is in exactly one part.  The
#: registers are the manifest's either way, so a group's subroutine is the same in its part as
#: in the whole project, and moving a group between parts changes no case's hash.  Groups run
#: in ``ALL_GROUPS`` order, so main calls them in that order, and a group a CLICK may refuse at
#: paste comes last in its part, so its subroutine and call rung are the last to delete.
PARTS: dict[str, tuple[str, ...]] = {
    # Contacts, coils, timers and counters.  ``compare`` is last: the editor may refuse its
    # 2-char literal.
    "coils": (
        "out",
        "coils",
        "counters",
        "timers",
        "out_more",
        "coil_range",
        "compare",
    ),
    # ``past_write`` is last: it writes past the DD bank on a CLICK, so its subroutine and
    # call rung can be left out.  Nothing depends on it.
    "copy": (
        "text",
        "block",
        "word_store",
        "real_bits",
        "copy_more",
        "block_more",
        "past_write",
    ),
    # ``store_probe`` is last: it raises fault flags no group below may read.
    "math": (
        "math",
        "numeric",
        "math_error",
        "real_sum",
        "math_more",
        "hex_math",
        "agg_more",
        "store_probe",
    ),
    # The drums are too many for one project.  ``drum_limits`` may be refused at paste.
    "drum1": ("drum", "drum_trace", "drum_rule", "drum_jump", "drum_limits"),
    "drum2": ("drum_settle", "drum_close", "drum_idle", "drum_pins"),
    "drum3": ("shift", "search", "drum_branch"),
    # How a rung is solved, and program control.  ``loop_call`` runs its rungs in main, after
    # every call rung.
    "control": (
        "rungs",
        "loop",
        "solve_order",
        "call_twice",
        "ret_branch",
        "loop_call",
        "row_solve",
        "cont_box",
        "for_count",
        "paste_risk",
        "for_more",
        "call_more",
    ),
    # ``system`` runs first (see test_system.py), then what reads a fault flag after it.
    "system": ("system", "faults", "flag_probe"),
    # Not by chapter: a person boots the PLC between its runs, ``rtc_set`` moves the clock and
    # ``for_wide`` may make a scan of over a second, so it holds no wall-clock case.
    "boot": ("boot_read", "rtc_set", "for_wide", "flag_scan"),
}


@dataclass(frozen=True)
class ApartPart:
    """A CLICK project on memory of its own, outside the whole suite's layout.

    Its groups' registers and raw memory are ``manifest_apart.toml`` and
    ``memory.OWNED_APART``: they may repeat the suite's, so it never runs in one project
    with them, and it changes no other part's hash however it grows.  With
    *fixed_scan_ms* a person runs it with CLICK's fixed scan on, at that time.
    """

    groups: tuple[str, ...]
    fixed_scan_ms: int | None = None


#: Every group on memory of its own, in the order main calls them.
APART_GROUPS = [
    test_unit_pairs.group,
    # Left out with ``GROUP=unit_pairs``: a minute's wait.
    test_unit_minute.group,
    test_fixed_scan.group,
    test_text_compare.group,
    test_flasher_td.group,
    # The groups that read a flag run first in their part.
    test_flag_more.flag_boxes,
    test_flag_more.flag_halves,
    test_math_terms.group,
    test_search_more.group,
    test_real_text.group,
    # Writes past the DD bank: deletable, last but one of its part.
    test_past_clean.group,
    test_for_dd_minus.group,
    test_bad_jump.group,
    test_drum_preset.group,
    test_deb_zero.group,
    test_coil_timing.group,
    test_td_write.group,
    test_word_floor.group,
    test_flasher_off.group,
    test_txt_end.group,
    # The 2026-11-06 round's follow-up, the follow2_* parts.
    test_flag_steps.flag_steps,
    test_flag_steps.flag_reads,
    # Alone in its part: its op is the scan's last Math, its reads the next scan's first rungs.
    test_flag_carry.group,
    test_term_zero.group,
    test_pack_point.group,
    test_drum_ds.drum_ds,
    test_drum_ds.jump_return,
    test_coil_phase.deb_fall,
    test_coil_phase.flash_zero,
    test_tof_rewrite.group,
    # Stops the PLC on a 200 ms watchdog: run alone, with the watchdog at 10000 ms.
    test_for_signed.group,
    # A reset held on while its rung is on.
    test_reset_pins.group,
    # Raw Modbus requests (test_modbus_raw.py drives it).
    test_modbus_raw.group,
]

APART_PARTS: dict[str, ApartPart] = {
    # Timers in different units with one delay, Done on one scan: the relation, which
    # holds or not whatever a scan takes.
    "units": ApartPart(("unit_pairs", "unit_minute")),
    # The scan a CLICK counts, for curiosity: only fixed scan mode makes that a number.
    "fixed_scan": ApartPart(("fixed_scan",), fixed_scan_ms=100),
    # What = and not-equal make of a TXT, a blank above all: a project of its own, since no
    # block of the suite has a slot left, and a blank needs TXT that no project has written.
    # And what a flasher's frozen off TD does when a lowered preset ends its on phase.
    "text_flasher": ApartPart(("text_compare", "flasher_td")),
    # The 2026-11-05 round's follow-up, for the fixes of the conform-fix arc, in three parts
    # since CLICK's editor freezes on a paste much over 150 rungs: what SC43 and SC44 follow;
    "follow_flags": ApartPart(("flag_boxes", "flag_halves")),
    # the values the round read as none of the candidates, or left unread;
    "follow_values": ApartPart(
        ("math_terms", "search_more", "real_text", "past_clean", "for_dd_minus")
    ),
    # and timing: a drum's jump to a bad step and when it reads a step's preset, a debounce's
    # off side and a flasher's on phase of 0; and, for room, a Word store below -32768.
    "follow_timing": ApartPart(
        ("bad_jump", "drum_preset", "deb_zero", "coil_timing", "word_floor")
    ),
    # A fourth, of what the others left: a flasher's off phase of 0 or below, a TD written
    # into a running debounce and flasher, and a Real's text running past TXT1000.
    "follow_extra": ApartPart(("flasher_off", "td_write", "txt_end")),
    # The 2026-11-06 round's follow-up, for the probes the convergence audit asked for: what
    # leaves a fault flag on, each flag read by a coil right after its step;
    "follow2_flags": ApartPart(("flag_steps",)),
    # a flag after a box that did not run, a quiet math error's 0 in a larger formula, and a
    # decimal point packed into a DD;
    "follow2_values": ApartPart(("flag_reads", "term_zero", "pack_point")),
    # SC40 and SC46 across a scan boundary, alone so nothing runs between the op and the reads;
    "follow2_carry": ApartPart(("flag_carry",)),
    # a DS drum preset held from the reset, and a bad jump on the drum rung's return;
    "follow2_drums": ApartPart(("drum_ds", "jump_return")),
    # a debounce's off count at its fall, flashers on presets of 0, a TOF's TD written to 0.
    "follow2_coils": ApartPart(("deb_fall", "flash_zero", "tof_rewrite")),
    # For counts whose runs stop the PLC on a 200 ms watchdog, alone.
    "follow2_for": ApartPart(("for_signed",)),
    # The 2026-11-11 round: a down counter's and an RTON's reset held on while its rung is on.
    "follow3_reset": ApartPart(("reset_pins",)),
    # Values staged for raw Modbus reads, and the echo of raw writes, for test_raw_requests.
    "modbus_raw": ApartPart(("modbus_raw",)),
}

#: Estimated CLICK steps a part may take (``estimate_steps``): CLICK's 8000,
#: less a margin for the estimate's error and the instructions it has not
#: measured.
PART_STEP_BUDGET = 7500

#: Rungs a ``follow_*`` or ``follow2_*`` part may export (``R,`` rows of the CSVs): CLICK's
#: editor freezes on a larger paste.  ``test_build.py`` holds each part to it.
FOLLOW_RUNG_LIMIT = 150


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
    """The program for *part* (``None``: the whole suite) on its manifest."""
    manifest = APART_MANIFEST_PATH if part in APART_PARTS else MANIFEST_PATH
    return build_program(groups(part), load_manifest(manifest))


def build_apart() -> ConformanceProgram:
    """Every group on memory of its own, as one program, for a local target."""
    return build_program(APART_GROUPS, load_manifest(APART_MANIFEST_PATH))


def build_for(endpoint: Endpoint | None, names: list[str] | None = None) -> ConformanceProgram:
    """The build loaded at *endpoint*: the part, or the whole suite, whose layout
    hash is in its ``DS1``.  A local target (``None``) runs the whole suite, or the
    groups on memory of their own when *names* asks for those, in their own part when
    one part holds them all.

    Raises:
        BuildMismatchError: ``DS1`` holds no build of the suite's layout hash.
    """
    if endpoint is None:
        apart = {g.name for g in APART_GROUPS}
        if not names or not apart.issuperset(names):
            return build()
        for part, held in APART_PARTS.items():
            if set(held.groups).issuperset(names):
                return build(part)
        return build_apart()
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
    ms = APART_PARTS[part].fixed_scan_ms if part in APART_PARTS else None
    if ms is None:
        return None
    return (
        f"Part {part} runs in fixed scan mode: in the CLICK software turn fixed scan on "
        f"(SC202) and set the scan time (SD13) to {ms} ms, write the project to the PLC, and "
        f"run `make test-conformance PLC=...`. "
        'See tests/conformance/AGENTS.md, "Fixed scan part".'
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
