"""Crossings — the recorded resolver (discharge constraints against history).

The projected registry expresses a crossing once; the recorded mechanism reads
the answer out of an observed scan instead of forking.  A Prior is where the two
differ: it shifts the chase one scan back.
"""

from __future__ import annotations

from pyrung import Bool
from pyrung.core.analysis.causal.crossings_recorded import (
    resolve_recorded,
    resolve_recorded_branches,
)
from pyrung.core.analysis.crossings.boolean import LatchCrossing
from pyrung.core.analysis.crossings.shift import ShiftCrossing
from pyrung.core.crossing import (
    Cmp,
    CondAttr,
    Eq,
    External,
    Prior,
    Quant,
    ReverseResult,
    eq_target,
    single,
)
from pyrung.core.instruction.advanced import ShiftInstruction
from pyrung.core.instruction.coils import LatchInstruction
from pyrung.core.memory_block import Block
from pyrung.core.tag import TagType


class _State:
    def __init__(self, tags):
        self.tags = tags


class _History:
    """Minimal History stand-in: scan_id -> {tag: value}."""

    def __init__(self, states):
        self._states = states

    def scan_ids(self):
        return sorted(self._states)

    def at(self, scan_id):
        return _State(self._states[scan_id])


# --- same-scan constraints (Eq / Cmp / Mask) read at scan N -------------------


def test_eq_resolves_to_value_change_at_scan_n() -> None:
    history = _History({1: {"X": 0}, 2: {"X": 5}})
    r = resolve_recorded(Eq("X", frozenset({5})), history=history, scan_id=2)
    assert r is not None
    assert (r.kind, r.tag, r.scan_id, r.before, r.after, r.changed) == ("value", "X", 2, 0, 5, True)


def test_cmp_resolves_held_value_as_unchanged() -> None:
    history = _History({1: {"X": 7}, 2: {"X": 7}})
    r = resolve_recorded(Cmp("X", ">=", 3), history=history, scan_id=2)
    assert r is not None and r.changed is False and r.after == 7


def test_tag_bound_cmp_branch_resolves_both_operands() -> None:
    history = _History(
        {
            1: {"Acc": 5, "Preset": 10},
            2: {"Acc": 5, "Preset": 5},
        }
    )

    branches = resolve_recorded_branches(
        single(Cmp("Acc", ">=", "Preset", bound_is_tag=True), exact=True),
        history=history,
        scan_id=2,
    )

    assert len(branches) == 1
    assert [(fact.tag, fact.changed) for fact in branches[0]] == [
        ("Acc", False),
        ("Preset", True),
    ]


def test_branch_resolution_preserves_reverse_exactness() -> None:
    history = _History({1: {"Source": 1}, 2: {"Source": 2}})

    for exact in (True, False):
        branches = resolve_recorded_branches(
            single(Eq("Source", frozenset({2})), exact=exact),
            history=history,
            scan_id=2,
        )

        assert len(branches) == 1
        assert [fact.exact for fact in branches[0]] == [exact]


# --- Prior shifts the chase one scan back -------------------------------------


def test_prior_reads_source_at_previous_scan() -> None:
    # C3@2 was carried from C2@1; the chase moves to C2 at scan 1.
    history = _History({0: {"C2": False}, 1: {"C2": True}, 2: {"C3": True}})
    r = resolve_recorded(Prior("C3", "C2", 1, 0), history=history, scan_id=2)
    assert r is not None
    assert (r.kind, r.tag, r.scan_id) == ("value", "C2", 1)
    assert (r.before, r.after, r.changed) == (False, True, True)


def test_prior_with_no_previous_scan_is_unresolvable() -> None:
    history = _History({2: {"C3": True}})  # scan 2 is the earliest retained
    assert resolve_recorded(Prior("C3", "C2", 1, 0), history=history, scan_id=2) is None


def test_unresolved_prior_invalidates_whole_conjunctive_branch() -> None:
    history = _History({2: {"C3": True, "Gate": True}})
    result = ReverseResult(
        branches=(
            (
                Prior("C3", "C2", 1, 0),
                Eq("Gate", frozenset({True})),
            ),
        ),
        exact=True,
    )

    assert resolve_recorded_branches(result, history=history, scan_id=2) == []


# --- leaves -------------------------------------------------------------------


def test_external_is_a_leaf_stop() -> None:
    history = _History({1: {"R": 1}})
    r = resolve_recorded(External("R"), history=history, scan_id=1)
    assert r is not None and r.kind == "external" and r.tag == "R"


def test_condattr_defers_to_attribute() -> None:
    history = _History({1: {}})
    r = resolve_recorded(CondAttr(expected=True), history=history, scan_id=1)
    assert r is not None and r.kind == "condition" and r.expected is True


def test_quant_is_frontier() -> None:
    history = _History({1: {}})
    r = resolve_recorded(Quant("exists", ("DS1",), ">=", 100), history=history, scan_id=1)
    assert r is not None and r.kind == "frontier"


# --- DNF resolution against real handler output -------------------------------


def test_shift_prior_disjunction_resolves_each_branch() -> None:
    # ShiftCrossing emits (Prior neighbour) OR (Prior held) for a True interior cell.
    bits = Block("C", TagType.BOOL, 1, 8)
    instr = ShiftInstruction(bits.select(1, 8), Bool("D"), Bool("Clk"), Bool("Rst"))
    result = ShiftCrossing().reverse(instr, None, eq_target("C3", True), _ctx_unused())
    history = _History(
        {0: {"C2": False, "C3": False}, 1: {"C2": True, "C3": False}, 2: {"C3": True}}
    )
    branches = resolve_recorded_branches(result, history=history, scan_id=2)
    # branch 0: came from C2@1 ; branch 1: held C3@1
    assert [b[0].tag for b in branches] == ["C2", "C3"]
    assert [b[0].scan_id for b in branches] == [1, 1]


def test_latch_held_branch_resolves_but_condition_branch_defers() -> None:
    result = LatchCrossing().reverse(
        LatchInstruction(Bool("M")), None, eq_target("M", True), _ctx_unused()
    )
    history = _History({0: {"M": False}, 1: {"M": True}})
    branches = resolve_recorded_branches(result, history=history, scan_id=1)
    kinds = [b[0].kind for b in branches]
    assert kinds == ["condition", "value"]  # CondAttr branch, then held Prior


def _ctx_unused():
    from pyrung.core.crossing import CrossingContext

    return CrossingContext()
