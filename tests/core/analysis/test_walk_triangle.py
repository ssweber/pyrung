"""Triangle table (Stage D1): kernels, windows, divest points over walk plans.

Derived once from holds + steps at Path-build time (Fikes-Hart-Nilsson 1972 /
PLANEX).  ``kernel(i)`` is the set of external-input conditions that must hold
at entry to step *i* for steps ``i..n`` to remain valid; the rows a hold spans
are its timing window; the row a hold leaves is a divest point (an emergent
phase boundary discovered by walking, not static analysis).

The table is monitoring/rendering output only — it never asserts
reachability and no walk decision reads it.
"""

from __future__ import annotations

from pyrung import Bool, Program
from pyrung.core.analysis.graph import (
    Path,
    ReachabilityStep,
    TriangleRow,
    TriangleTable,
    _build_triangle_table,
    _value_runs,
)
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Pure builder units
# ---------------------------------------------------------------------------


def test_value_runs_collapse_and_close() -> None:
    # Consecutive same-value writes merge; a change closes the prior run.
    writes = [(1, True), (2, True), (4, False), (6, True)]
    assert _value_runs(writes) == [(1, True, 4), (4, False, 6), (6, True, None)]
    assert _value_runs([]) == []


def test_builder_returns_none_without_holds() -> None:
    steps = (({"A": True}, 1),)
    assert _build_triangle_table(steps, (), ()) is None


def test_builder_spans_kernels_windows_divests() -> None:
    steps = (
        ({"A": True}, 1),
        ({"B": True}, 5),
        ({"A": False}, 2),
    )
    final = (("A", False, "GoalA2"), ("B", True, "GoalB"))
    released = (("A", True, "GoalA"),)
    table = _build_triangle_table(steps, final, released)
    assert table is not None
    assert table.n_steps == 3

    by_key = {(r.name, r.value): r for r in table.rows}
    a_true = by_key[("A", True)]
    assert (a_true.start, a_true.end, a_true.divested) == (1, 3, True)
    assert a_true.scans == 6  # steps 1..2 while the hold is in force
    b_true = by_key[("B", True)]
    assert (b_true.start, b_true.end, b_true.divested) == (2, None, False)
    assert b_true.scans == 7  # steps 2..3, held through end
    a_false = by_key[("A", False)]
    assert (a_false.start, a_false.end, a_false.divested) == (3, None, False)
    assert a_false.scans == 2

    # Kernel semantics: a row enters at start+1, leaves after its end.
    assert table.kernel(1) == frozenset()
    assert table.kernel(2) == frozenset({("A", True)})
    assert table.kernel(3) == frozenset({("A", True), ("B", True)})
    # Post-plan kernel = the must-stay set (Path.holds).
    assert table.kernel(4) == frozenset({("A", False), ("B", True)})

    assert [r.name for r in table.divest_points()] == ["A"]
    narrow = table.narrowest_window()
    assert narrow is not None and (narrow.name, narrow.value) == ("A", False)

    # Divergence resume: highest step whose kernel the tags satisfy.
    assert table.highest_true_kernel({"A": False, "B": True}) == 4
    assert table.highest_true_kernel({"A": True, "B": True}) == 3
    assert table.highest_true_kernel({"A": True, "B": False}) == 2
    assert table.highest_true_kernel({}) == 1


def test_kernel_index_bounds() -> None:
    table = _build_triangle_table((({"A": True}, 1),), (("A", True, "G"),), ())
    assert table is not None
    for bad in (0, table.n_steps + 2):
        try:
            table.kernel(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"kernel({bad}) should raise")


def test_final_hold_without_establishing_step() -> None:
    # A hold surviving from a dropped subtree references no write in the
    # realized steps: span-less row, excluded from kernels, rendered honestly.
    steps = (({"A": True}, 1),)
    table = _build_triangle_table(steps, (("Ghost", True, "G"),), ())
    assert table is not None
    row = table.rows[-1]
    assert (row.name, row.start, row.end) == ("Ghost", None, None)
    assert table.kernel(2) == frozenset()
    assert "no establishing step" in row.render()


def test_released_hold_without_matching_run_is_dropped() -> None:
    # Released-then-re-established at the same value: the surviving hold's row
    # describes the whole span; the released entry adds nothing and gets no row.
    steps = (({"A": True}, 1),)
    table = _build_triangle_table(steps, (("A", True, "GoalLate"),), (("A", True, "GoalEarly"),))
    assert table is not None
    assert len(table.rows) == 1
    assert table.rows[0].goal == "GoalLate"
    assert table.rows[0].divested is False


def test_table_rendering() -> None:
    steps = (({"A": True}, 1), ({"A": False}, 2))
    table = _build_triangle_table(steps, (("A", False, "G2"),), (("A", True, "G1"),))
    assert table is not None
    text = str(table)
    assert "Triangle table (2 step(s), 2 row(s)):" in text
    assert "A=true (for G1): established step 1, divested step 2" in text
    assert "A=false (for G2): established step 2, held through end" in text
    assert "Kernel after plan: A=false" in text
    assert "Narrowest window:" in text


def test_path_without_triangle_renders_unchanged() -> None:
    step = ReachabilityStep(action={"X": True}, source_key=(), dest_key=(), scans=1)
    bare = Path(reachable=True, steps=(step,), total_changes=1, total_scans=1)
    assert bare.triangle is None
    assert "Divests" not in str(bare)


def test_path_divests_rendering_unit() -> None:
    step = ReachabilityStep(action={"X": True}, source_key=(), dest_key=(), scans=1)
    table = TriangleTable(rows=(TriangleRow("Arm", True, "Armed", 1, 2, 1, True),), n_steps=2)
    p = Path(
        reachable=True,
        steps=(step, step),
        total_changes=1,
        total_scans=2,
        triangle=table,
    )
    assert "Divests: Arm at step 2 (was protecting Armed)" in str(p)


# ---------------------------------------------------------------------------
# HoldStore release journal (the divest-row source)
# ---------------------------------------------------------------------------


def test_hold_store_release_journal_and_rollback() -> None:
    store = walk.HoldStore()
    store.protect("A", True, ("GoalA", True))
    store.protect("B", True, ("GoalB", True))
    assert store.released() == ()

    store.release("A")
    assert [(h.name, h.value, h.goal) for h in store.released()] == [("A", True, ("GoalA", True))]
    # Releasing an absent name journals nothing.
    store.release("A")
    assert len(store.released()) == 1

    # Speculative sections roll the journal back with the live holds.
    snap = store.snapshot()
    store.release("B")
    assert len(store.released()) == 2
    store.restore(snap)
    assert len(store.released()) == 1
    assert store.protected_names() == frozenset({"B"})


# ---------------------------------------------------------------------------
# Integration: real walks
# ---------------------------------------------------------------------------


def _shared_gate_program() -> tuple[Program, Bool]:
    from tests.core.analysis.test_walk_holds import _shared_gate_program as build

    return build()


def _seal_release_program() -> tuple[Program, Bool, Bool]:
    from tests.core.analysis.test_walk_holds import _seal_release_program as build

    return build()


def test_triangle_from_prevention_walk() -> None:
    """Shared-gate prevention plan: every surviving hold has a table row."""
    prog, target = _shared_gate_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(target)
    assert path.reachable
    assert path.holds is not None
    table = path.triangle
    assert table is not None
    assert table.n_steps == len(path.steps)

    rows_by_key = {(r.name, r.value): r for r in table.rows}
    for name, value, goal in path.holds:
        row = rows_by_key[(name, value)]
        assert row.goal == goal
        assert row.divested is False
    # At the replayed end state every hold is in force: resume from the end.
    replay = PLC(prog, dt=0.010)
    for step in path.steps:
        replay.patch(step.action)
        for _ in range(step.scans):
            replay.step()
    assert table.highest_true_kernel(dict(replay.state.tags)) == table.n_steps + 1


def test_triangle_divest_point_from_seal_release_walk() -> None:
    """The Arm=true hold's divest row marks the emergent phase boundary."""
    prog, armed, fired = _seal_release_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(armed, fired)
    assert path.reachable
    table = path.triangle
    assert table is not None

    divests = table.divest_points()
    assert [(r.name, r.value) for r in divests] == [("Arm", True)]
    row = divests[0]
    assert row.goal == "Armed"
    assert row.start is not None and row.end is not None and row.start < row.end
    # The hold is required up to entry of the divest step, gone after it.
    assert ("Arm", True) in table.kernel(row.end)
    if row.end + 1 <= table.n_steps + 1:
        assert ("Arm", True) not in table.kernel(row.end + 1)
    assert f"Divests: Arm at step {row.end} (was protecting Armed)" in str(path)


def test_triangle_narrowest_window_reported() -> None:
    """Window characterization: the fragility row is surfaced in the rendering."""
    prog, target = _shared_gate_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(target)
    assert path.reachable
    assert path.triangle is not None
    narrow = path.triangle.narrowest_window()
    assert narrow is not None
    assert narrow.scans >= 1
    assert "Narrowest window:" in str(path.triangle)
