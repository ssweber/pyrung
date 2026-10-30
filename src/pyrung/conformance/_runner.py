"""Conformance suite runner (in-process) -- the 8-step protocol -- and reporting."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pyrung.conformance._case import (
    Case,
    CaseResult,
    Group,
    expectation_met,
    format_expect,
    judge,
    system_tag_name,
)
from pyrung.conformance._confirm import Confirmation, is_confirmed
from pyrung.conformance._ladder import case_label, check_disjoint, emit_case
from pyrung.conformance._slot import SLOT_FIELDS, make_slot
from pyrung.core import PLC, Program, Rung, call, subroutine
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME

if TYPE_CHECKING:
    from pyrung.core.state import SystemState
    from pyrung.core.structure import _NamedArrayRuntime

#: Simulated seconds per in-process scan (pyrung's nominal scan).
DT = 0.010
#: Simulated seconds a case's reset may take before the case is judged.
RESET_TIMEOUT_S = 10.0
#: Runs of every case on one PLC.  Each must pass, and the reset between them
#: must leave the state a fresh PLC has.
PASSES = 2


def run(cases: Sequence[Case] | Group) -> list[CaseResult]:
    """Run *cases* in-process on a fixed-step ``PLC``, counting scans exactly.

    Given a :class:`Group`, its cases run inside the group's subroutine, called
    unconditionally from the main program, as on the wire.  Every case runs
    :data:`PASSES` times on the same PLC, with the reset handshake before each
    run.  After the second reset the whole state (tags and instruction memory)
    is compared with the state after the first, on a fresh PLC: anything that
    differs, other than a case's ``keep`` tags, is a leak and fails the case
    that owns it.

    Raises:
        ValueError: Two cases share memory.
    """
    if isinstance(cases, Group):
        group: Group | None = cases
        case_list = list(cases.cases)
    else:
        group = None
        case_list = list(cases)
    if not case_list:
        return []

    group_name = group.name if group is not None else ""
    footprints = check_disjoint([(group_name, c) for c in case_list])
    slot = make_slot(len(case_list), name=group.title if group is not None else "Slot")
    indexes = range(1, len(case_list) + 1)

    with Program(strict=False) as logic:
        if group is None:
            for i, c in enumerate(case_list, start=1):
                emit_case(group_name, c, slot, i)
        else:
            for _ in range(group.calls):
                with Rung():
                    call(group.subroutine)
            with subroutine(group.subroutine):
                for i, c in enumerate(case_list, start=1):
                    emit_case(group_name, c, slot, i)

    owner: dict[str, int] = {}
    for i, c in enumerate(case_list, start=1):
        for tag_name in footprints[case_label(group_name, c)]:
            owner[tag_name] = i
        for field_name in SLOT_FIELDS:
            owner[getattr(slot[i], field_name).name] = i
    kept = {t.name for c in case_list for t in c.keep}

    runs: list[dict[int, tuple[dict[str, int], bool]]] = []
    unready: set[int] = set()
    leaks: dict[int, list[str]] = {}
    with PLC(logic, dt=DT) as plc:
        fresh: SystemState | None = None
        for _ in range(PASSES):
            unready |= _reset(plc, slot, indexes)
            if fresh is None:
                fresh = plc.current_state
            else:
                for key, (now, before) in sorted(_diff(fresh, plc.current_state, kept).items()):
                    i = owner.get(key.rsplit(":", 1)[-1])
                    text = f"{key} = {now!r} (fresh {before!r})"
                    for j in [i] if i is not None else indexes:
                        leaks.setdefault(j, []).append(text)
            runs.append(_fire(plc, slot, case_list, [i for i in indexes if i not in unready]))

    results: list[CaseResult] = []
    for i, c in enumerate(case_list, start=1):
        outcomes = [r.get(i, ({}, False)) for r in runs]
        actual, fired = outcomes[-1]
        met = [f and expectation_met(c.checks, a) for a, f in outcomes]
        expected_miss = c.pending is not None
        notes: list[str] = []
        if i in unready:
            notes.append(
                f"reset did not complete: Ready stayed 0 for {RESET_TIMEOUT_S:g} s of scans"
            )
        for n, (a, f) in enumerate(outcomes[:-1], start=1):
            if (a, f) != (actual, fired) or (not met[n - 1] and not expected_miss):
                notes.append(f"run {n} of {PASSES}: actual {a}" + ("" if f else ", did not fire"))
        if i in leaks:
            notes.append("state left after the reset: " + ", ".join(leaks[i]))
        passed, xfail, pending_note = judge(
            c, met, i not in unready and i not in leaks, actual, pending=True
        )
        if pending_note:
            notes.insert(0, pending_note)
        results.append(
            CaseResult(
                case=c,
                passed=passed,
                actual=actual,
                slot_index=i,
                fired=fired,
                note="; ".join(notes),
                group=group_name,
                xfail=xfail,
            )
        )
    return results


def _reset(plc: Any, slot: _NamedArrayRuntime, indexes: Collection[int]) -> set[int]:
    """Zero every slot, as the wire harness does, and scan until each reports ``Ready``.

    A case that stopped the PLC (a math error) leaves it in STOP: put it back in
    RUN first, as the operator of a real CLICK does by hand.  Returns the slots
    that never reported ``Ready``.
    """
    if plc.mode == "stop":
        plc.start()
    plc.patch({getattr(slot[i], f).name: 0 for i in indexes for f in SLOT_FIELDS})
    waiting = set(indexes)
    for _ in range(round(RESET_TIMEOUT_S / DT)):
        plc.step()
        tags = plc.current_state.tags
        waiting = {i for i in waiting if not tags.get(slot[i].Ready.name, 0)}
        if not waiting:
            break
    return waiting


def _fire(
    plc: Any, slot: _NamedArrayRuntime, case_list: Sequence[Case], indexes: Sequence[int]
) -> dict[int, tuple[dict[str, int], bool]]:
    """Write ``Cmd = 1`` to *indexes*, run the longest case's scans, read their slots.

    A PLC in STOP does not scan: once a case stops it, the harness stops
    stepping (``step()`` would put it back in RUN) and reads what it left.
    """
    if not indexes:
        return {}
    plc.patch({slot[i].Cmd.name: 1 for i in indexes})
    for _ in range(max(case_list[i - 1].scans for i in indexes)):
        if plc.mode == "stop":
            break
        plc.step()
    state = plc.current_state
    tags = state.tags
    out: dict[int, tuple[dict[str, int], bool]] = {}
    for i in indexes:
        c = case_list[i - 1]
        actual = {f: tags.get(getattr(slot[i], f).name, 0) for f in c.expect}
        for address in c.expect_system:
            _, value = plc.debug.system_runtime.resolve(system_tag_name(address), state)
            actual[address] = int(value)
        out[i] = (actual, tags.get(slot[i].Fired.name, 0) != 0)
    return out


def _diff(fresh: SystemState, now: SystemState, kept: Collection[str]) -> dict[str, tuple]:
    """Tags and instruction memory that differ between two states, as ``key -> (now, fresh)``.

    System tags, pyrung's internal tags (a leading ``_``), system memory and
    *kept* tags are skipped, and so is memory keyed by a system or kept tag (a
    clock's edge history ``_prev:sys.clock_100ms``, a timer's ``_frac:TD1``).
    """
    out: dict[str, tuple] = {}
    for name in set(fresh.tags) | set(now.tags):
        if name in kept or name in SYSTEM_TAGS_BY_NAME or name.startswith("_"):
            continue
        before, after = fresh.tags.get(name, 0), now.tags.get(name, 0)
        if before != after:
            out[name] = (after, before)
    for key in set(fresh.memory) | set(now.memory):
        if key == "_dt" or key.startswith("_sys."):
            continue
        keyed_by = key.rsplit(":", 1)[-1]
        if keyed_by in kept or keyed_by in SYSTEM_TAGS_BY_NAME:
            continue
        before, after = fresh.memory.get(key), now.memory.get(key)
        if before != after and (before or after):
            out[key] = (after, before)
    return out


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
        lines.append(f"  expected: {format_expect(f.case.checks)}")
        lines.append(f"  actual:   {f.actual}")
        if not f.fired:
            lines.append("  Fired:    did not fire")
        if f.note:
            lines.append(f"  note:     {f.note}")
    if lines:
        raise AssertionError("\n".join(lines))


def format_report(
    results: Sequence[CaseResult], confirmations: Mapping[str, Confirmation] | None = None
) -> str:
    """One ASCII line per case, then totals.

    Each line has the status (PASS, XFAIL for a ``pending`` case that misses
    CLICK's expectation as it must, FAIL, SKIP, STALE), whether the case has
    passed on a CLICK as it is now (*confirmations*, see ``_confirm.py``), the
    pending step and the probe question; a FAIL or XFAIL line is followed by
    what the case read.
    """
    confirmations = confirmations or {}
    lines: list[str] = []
    counts = {"PASS": 0, "XFAIL": 0, "FAIL": 0, "SKIP": 0, "STALE": 0}
    unconfirmed = 0
    for r in results:
        if r.stale:
            status = "STALE"
        elif r.skipped is not None:
            status = "SKIP"
        elif r.xfail:
            status = "XFAIL"
        else:
            status = "PASS" if r.passed else "FAIL"
        counts[status] += 1
        confirmed = is_confirmed(r, confirmations)
        if not confirmed:
            unconfirmed += 1
        marks = ["confirmed" if confirmed else "unconfirmed"]
        if r.case.pending is not None:
            marks.append(f"pending {r.case.pending}")
        if r.case.probe is not None:
            marks.append(f"probe: {r.case.probe}")
        lines.append(f"{status:<5} {_label(r)} [{'; '.join(marks)}] {r.case.sentence}")
        if status in ("FAIL", "XFAIL"):
            lines.append(f"      actual {r.actual}")
    words = {
        "PASS": "passed",
        "XFAIL": "pending (pyrung differs from CLICK)",
        "FAIL": "failed",
        "SKIP": "skipped",
        "STALE": "stale",
    }
    totals = ", ".join(f"{n} {words[k]}" for k, n in counts.items())
    lines.append(f"{totals}; {unconfirmed} unconfirmed on hardware")
    return "\n".join(lines)


def _label(r: CaseResult) -> str:
    return case_label(r.group, r.case)
