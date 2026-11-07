"""The temporary logic a plan prints, run as pyrung source, does what the recording did.

Each case builds a hold, prints its rows and runs the printed rows back as rungs.  Each tag the
hold drives must take the value of its last rule on, read from the state before the scan.  The
printed rows must match the hold on every tag, scan by scan, in each of these:

- the rows as the PLC's temporary logic
- the rows pasted at the top of the program, in the interpreter and in the compiled kernel
- the rows as temporary logic over one ``run()``
"""

from __future__ import annotations

from typing import Any

import pytest

import pyrung
from pyrung import PLC, And, Bool, Int, Program, Rung, copy, math
from pyrung.core import CompiledPLC
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _set_pilot_rungs,
    _set_synth_holds,
    pilot_logic_at,
)
from pyrung.core.condition import CompareEq, CompareNe, _as_condition
from pyrung.core.rung import Rung as BuiltRung
from pyrung.core.runner import _iter_tags_and_edge_names
from pyrung.core.synthesis import bracket_lines, conditional_hold_rung
from pyrung.core.validation.render import operand_name

_DSL = {
    name: getattr(pyrung, name)
    for name in ("rung", "comment", "out", "latch", "reset", "copy", "And", "Or")
}


class _Dotted:
    """A structure name in the text, such as ``HoldStableOn`` in ``HoldStableOn.Done``."""

    def __init__(self, prefix: str, tags: dict[str, Any]):
        self._prefix = prefix
        self._tags = tags

    def __getattr__(self, field: str) -> Any:
        return self._tags[f"{self._prefix}.{field}"]


class _Names(dict):
    def __init__(self, tags: dict[str, Any]):
        super().__init__(_DSL)
        self._tags = tags

    def __missing__(self, name: str) -> Any:
        if name in self._tags:
            return self._tags[name]
        if any(key.startswith(f"{name}.") for key in self._tags):
            return _Dotted(name, self._tags)
        raise KeyError(name)


def _rungs_from_text(lines: tuple[str, ...], built: Any) -> list[Any]:
    """The printed rows run as pyrung source, each name bound to the tag the built rung uses."""
    tags, _edges = _iter_tags_and_edge_names([built])
    names = _Names({operand_name(tag): tag for tag in tags})
    source = "with Program(strict=False) as prog:\n" + "".join(f"    {line}\n" for line in lines)
    names["Program"] = Program
    exec(compile(source, "<printed temporary logic>", "exec"), {}, names)
    return list(names["prog"].rungs)


Sts = Int("Sts")
RS, A, B, C, W = Bool("RS"), Bool("A"), Bool("B"), Bool("C"), Bool("W")
Gate, Light = Bool("Gate"), Bool("Light")
Speed, Level = Int("Speed"), Int("Level")
Flag = Bool("Flag")


def _program() -> Program:
    """Sts counts 0..5 and starts over, so each guard below comes and goes."""
    with Program(strict=False) as prog:
        with Rung(Sts < 5):
            math(Sts + 1, Sts)
        with Rung(Sts >= 5):
            copy(0, Sts)
        # The held tags are the program's own.
        with Rung(RS, A, B, C, W, Gate, Light, Flag, Speed == 9, Level == 9):
            copy(1, Int("Seen"))
    return prog


def _rules_say(rules: list[tuple[Any, Any, Any]], keep: bool, before: Any) -> dict[str, Any]:
    """What the rules give each tag from the state before the scan.  The last rule on wins.

    With no rule on, a Bool goes off unless the rules keep it, and any other tag keeps its value.
    """
    from pyrung.core.analysis.sp_values import _SnapshotView
    from pyrung.core.tag import TagType

    view = _SnapshotView(dict(before.tags), {})
    said: dict[str, Any] = {}
    for dest, value, guard in rules:
        is_bool = dest.type == TagType.BOOL
        kept = before.tags.get(dest.name, dest.default)
        said.setdefault(dest.name, kept if keep or not is_bool else False)
        if _as_condition(guard).evaluate(view):
            said[dest.name] = bool(value) if is_bool else value
    return said


def _assert_text_replays(case: str, scans: int = 14) -> tuple[str, ...]:
    rules, keep = CASES[case]
    built = PLC(_program(), dt=0.01)
    if keep:
        (dest, _value, _guard), *_rest = rules
        hold = conditional_hold_rung(
            dest=dest, rules=[(value, guard) for _d, value, guard in rules]
        )
        _set_synth_holds(built, [hold])
    else:
        _set_pilot_rungs(
            built, [PilotRung(dest.name, value, guard) for dest, value, guard in rules]
        )
    hold = built._synthesis.holds[0]
    lines = bracket_lines(hold)

    twin = PLC(_program(), dt=0.01)
    _set_synth_holds(twin, _rungs_from_text(lines, hold))
    # The rows pasted at the top of the program, run by the interpreter and the compiled kernel.
    pasted = []
    for engine in (PLC, CompiledPLC):
        program = _program()
        program.rungs[:0] = _rungs_from_text(lines, hold)
        pasted.append(engine(program, dt=0.01))
    for scan in range(scans):
        said = _rules_say(rules, keep, built.state)
        built.step()
        assert {name: built.state.tags[name] for name in said} == said, (scan, lines)
        for other in (twin, *pasted):
            other.step()
            assert dict(other.current_state.tags) == dict(built.state.tags), (scan, other, lines)

    at_once = PLC(_program(), dt=0.01)
    _set_synth_holds(at_once, _rungs_from_text(lines, hold))
    at_once.run(cycles=scans)
    assert dict(at_once.state.tags) == dict(built.state.tags), lines
    return lines


CASES: dict[str, tuple[list[tuple[Any, Any, Any]], bool]] = {
    # An input that flips every scan while Sts >= 2.  Each rule reads RS before RS is written.
    "oscillator": (
        [(RS, True, And(Sts >= 2, ~RS)), (RS, False, And(Sts >= 2, RS))],
        False,
    ),
    # A reads B and B reads A.  C has one rule and reads neither.
    "several destinations": (
        [(A, True, ~B), (B, True, A), (A, False, And(A, B)), (C, True, Sts == 3)],
        False,
    ),
    # Three values for one Int.  The last rule on wins.
    "an Int": (
        [(Speed, 3, CompareNe(Speed, 3)), (Speed, 0, CompareEq(Speed, 3)), (Speed, 7, Sts == 4)],
        False,
    ),
    # An Int with no rule on for most scans keeps the value the last one wrote.
    "an Int with no rule on": ([(Level, 3, Sts == 1), (Level, 7, Sts == 4)], False),
    # A Bool given a number gets a copy per rule, under a reset that turns it off first.
    "a number into a Bool": (
        [(Flag, 2, Sts == 1), (Flag, 0, Sts == 2), (Flag, 5, Sts >= 4)],
        False,
    ),
    # One rule that reads the tag it drives.  W is on one scan and off the next while Sts is 1.
    "a rule reading its own tag": ([(W, True, And(Sts == 1, ~W))], False),
    # Two single rules in a cycle.  The second reads the first's tag before it is written.
    "a cycle": ([(Gate, True, ~Light), (Light, True, Gate)], False),
    # Rules that leave a Bool as it was when none is on.
    "a kept Bool": (
        [(W, True, CompareNe(W, True)), (W, False, And(Sts == 2, CompareNe(W, False)))],
        True,
    ),
}


@pytest.mark.parametrize("case", list(CASES))
def test_printed_rows_run_as_the_hold_ran(case: str) -> None:
    _assert_text_replays(case)


def test_each_rule_of_a_tag_with_several_gets_its_own_bit() -> None:
    lines = _assert_text_replays("several destinations")

    assert "  out(__on1__A)" in lines
    assert "  out(__off1__A)" in lines
    # B and C have one rule each, so each keeps one row and no bit.
    assert not any("__B" in line or "__C" in line for line in lines)
    bits_end = max(index for index, line in enumerate(lines) if "out(__" in line)
    assert all("out(__" not in line for line in lines[bits_end + 1 :])
    assert lines[bits_end + 1 :] == (
        "with rung(A):",
        "  out(B)",
        "with rung(__on1__A, ~__off1__A):",
        "  out(A)",
        "with rung(Sts == 3):",
        "  out(C)",
    )


def test_an_int_copies_once_per_rule_in_rule_order() -> None:
    lines = _assert_text_replays("an Int")

    assert lines[-6:] == (
        "with rung(__set1__Speed):",
        "  copy(3, Speed)",
        "with rung(__set2__Speed):",
        "  copy(0, Speed)",
        "with rung(__set3__Speed):",
        "  copy(7, Speed)",
    )


def test_a_single_rule_reading_its_own_tag_keeps_one_row() -> None:
    lines = _assert_text_replays("a rule reading its own tag")

    assert lines == ("with rung(Sts == 1, ~W):", "  out(W)")


def test_a_cycle_puts_the_second_rule_on_a_bit() -> None:
    lines = _assert_text_replays("a cycle")

    assert "  out(__on1__Light)" in lines
    assert "__Gate" not in "\n".join(lines)


def test_a_reached_plan_runs_the_same_from_its_printed_rows(monkeypatch) -> None:
    # The dwell drive installs, replaces and adds temporary logic.  Run with every hold built
    # from its printed rows, it reaches by the same steps, with the same tags on every scan.
    from pyrung.core import synthesis
    from tests.core.analysis import test_pilot_trim

    plc, goal = test_pilot_trim._dwell()
    plan = plc.how(goal)
    assert plan.reachable
    # Every step that puts logic in or takes it out prints it from the recording.
    changes = [step for step in plan.journal if step.kind in ("force", "revoke") and step.rungs]
    assert changes
    assert all(step.logic for step in changes), [step for step in changes if not step.logic]

    built = synthesis.guarded_copy_rung

    def from_text(rules: list[Any]) -> Any:
        hold = built(rules)
        outer = BuiltRung()
        for row in _rungs_from_text(bracket_lines(hold), hold):
            outer.add_branch(row)
        return outer

    monkeypatch.setattr(synthesis, "guarded_copy_rung", from_text)
    plc, goal = test_pilot_trim._dwell()
    texted = plc.how(goal)
    monkeypatch.undo()

    # Rows sharing a condition print under one rung, so the pasted logic has fewer rows and
    # prints a little differently.  The steps and every scan are the same.
    def steps(found: Any) -> list[tuple[Any, ...]]:
        return [(step.kind, step.scan, step.scans, step.inputs) for step in found.journal]

    assert steps(texted) == steps(plan)
    first, last = plan.fork.history.oldest_scan_id, plan.fork.state.scan_id
    assert (texted.fork.history.oldest_scan_id, texted.fork.state.scan_id) == (first, last)
    held = 0
    for scan in range(first, last + 1):
        assert dict(texted.fork.history.at(scan).tags) == dict(plan.fork.history.at(scan).tags)
        ran = pilot_logic_at(plan.fork, scan)
        assert (ran is None) == (pilot_logic_at(texted.fork, scan) is None)
        held += ran is not None and any("out(__" in line for line in bracket_lines(ran))
    # The comparison covered scans that ran rule bits.
    assert held
