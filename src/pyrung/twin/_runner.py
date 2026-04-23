"""Twin harness runner — the 8-step protocol."""

from __future__ import annotations

from pyrung.core import PLC, Program
from pyrung.twin._case import Case, CaseResult
from pyrung.twin._slot import make_slot


def run(cases: list[Case]) -> list[CaseResult]:
    if not cases:
        return []

    slot = make_slot(len(cases))

    with Program(strict=False) as logic:
        for i, c in enumerate(cases, start=1):
            c.ladder(slot[i])

    max_scans = max(c.scans for c in cases)

    with PLC(logic, dt=0.010) as plc:
        plc.step()

        cmd_patch = {slot[i].Cmd.name: 1 for i in range(1, len(cases) + 1)}
        plc.patch(cmd_patch)
        plc.run(cycles=max_scans)

        results: list[CaseResult] = []
        for i, c in enumerate(cases, start=1):
            actual: dict[str, int] = {}
            for field_name in c.expect:
                tag = getattr(slot[i], field_name)
                actual[field_name] = plc.current_state.tags.get(tag.name, 0)

            passed = actual == c.expect
            results.append(CaseResult(case=c, passed=passed, actual=actual, slot_index=i))

    return results


def assert_all_passed(results: list[CaseResult]) -> None:
    failures = [r for r in results if not r.passed]
    if not failures:
        return
    lines: list[str] = []
    for f in failures:
        lines.append(f"FAIL: {f.case.sentence}")
        lines.append(f"  expected: {f.case.expect}")
        lines.append(f"  actual:   {f.actual}")
    raise AssertionError("\n".join(lines))
