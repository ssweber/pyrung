"""Conformance suite runner (in-process) -- the 8-step protocol."""

from __future__ import annotations

from collections.abc import Sequence

from pyrung.conformance._case import Case, CaseResult, Group, expectation_met, format_expect
from pyrung.conformance._slot import make_slot
from pyrung.core import PLC, Program, Rung, call, subroutine


def run(cases: Sequence[Case] | Group) -> list[CaseResult]:
    """Run *cases* in-process on a fixed-step ``PLC``, counting scans exactly.

    Given a :class:`Group`, its cases run inside the group's subroutine, called
    unconditionally from the main program, as on the wire.
    """
    if isinstance(cases, Group):
        group: Group | None = cases
        case_list = list(cases.cases)
    else:
        group = None
        case_list = list(cases)
    if not case_list:
        return []

    slot = make_slot(len(case_list), name=group.title if group is not None else "Slot")

    with Program(strict=False) as logic:
        if group is None:
            for i, c in enumerate(case_list, start=1):
                c.ladder(slot[i])
        else:
            with Rung():
                call(group.subroutine)
            with subroutine(group.subroutine):
                for i, c in enumerate(case_list, start=1):
                    c.ladder(slot[i])

    max_scans = max(c.scans for c in case_list)
    group_name = group.name if group is not None else ""

    with PLC(logic, dt=0.010) as plc:
        plc.step()

        cmd_patch = {slot[i].Cmd.name: 1 for i in range(1, len(case_list) + 1)}
        plc.patch(cmd_patch)
        plc.run(cycles=max_scans)

        results: list[CaseResult] = []
        for i, c in enumerate(case_list, start=1):
            actual: dict[str, int] = {}
            for field_name in c.expect:
                tag = getattr(slot[i], field_name)
                actual[field_name] = plc.current_state.tags.get(tag.name, 0)

            fired = plc.current_state.tags.get(slot[i].Fired.name, 0) != 0
            passed = expectation_met(c.expect, actual) and fired
            results.append(
                CaseResult(
                    case=c,
                    passed=passed,
                    actual=actual,
                    slot_index=i,
                    fired=fired,
                    group=group_name,
                )
            )

    return results


def assert_all_passed(results: list[CaseResult]) -> None:
    """Raise ``AssertionError`` listing stale groups and every case that ran and failed.

    In-process-only skips pass; a stale group fails (its cases did not run).
    """
    lines: list[str] = []
    stale_seen: set[str] = set()
    for r in results:
        if r.stale and r.group not in stale_seen:
            stale_seen.add(r.group)
            lines.append(f"STALE: {r.note}")
    for f in results:
        if f.passed or f.skipped is not None:
            continue
        lines.append(f"FAIL: {_label(f)}: {f.case.sentence} (slot {f.slot_index})")
        lines.append(f"  expected: {format_expect(f.case.expect)}")
        lines.append(f"  actual:   {f.actual}")
        if not f.fired:
            lines.append("  Fired:    did not fire")
        if f.note:
            lines.append(f"  note:     {f.note}")
    if lines:
        raise AssertionError("\n".join(lines))


def _label(r: CaseResult) -> str:
    return f"{r.group}/{r.case.id}" if r.group else r.case.id
