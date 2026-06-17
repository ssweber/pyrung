"""Crossings Phase 1 — the recorded read-diff core (Tier 1).

``recorded_read_changes`` crosses an opaque writer mechanically: it diffs the
writer's pre-expanded ``data_reads`` footprint across the N-1 → N boundary and
reports which operands changed (triggers) and which are non-zero now (enablers).
No sign reasoning — the burner ``!= 0`` attribution falls out of the observed
operand values.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, calc, out
from pyrung.core.analysis.causal.crossings_recorded import (
    ReadDiff,
    recorded_read_changes,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.memory_block import Block
from pyrung.core.runner import PLC
from pyrung.core.tag import TagType


def _sum_program() -> Program:
    """``Total = DS1 + DS2 + DS3`` (opaque calc-sum), ``Flag`` when nonzero."""
    blk = Block("DS", TagType.INT, 1, 5)
    total = Int("Total")
    flag = Bool("Flag")
    with Program() as prog:
        with Rung(total != 0):
            out(flag)
        with Rung():
            calc(blk.select(1, 3).sum(), total)
    return prog


def _writer_node(prog: Program):
    pdg = build_program_graph(prog)
    return next(n for n in pdg.rung_nodes if "Total" in n.writes)


def test_footprint_is_block_expanded() -> None:
    node = _writer_node(_sum_program())
    assert node.data_reads == frozenset({"DS1", "DS2", "DS3"})


def test_read_diff_names_changed_and_nonzero_operand() -> None:
    prog = _sum_program()
    node = _writer_node(prog)
    plc = PLC(prog, dt=0.010)
    plc.step()  # scan with all operands zero
    plc.patch({"DS2": 5})
    plc.step()  # DS2 flips 0 -> 5, Total -> 5

    scan = plc.history.scan_ids()[-1]
    diff = recorded_read_changes(plc.history, node, scan)

    assert diff.footprint == frozenset({"DS1", "DS2", "DS3"})
    assert diff.changed == [("DS2", 0, 5)]
    assert diff.nonzero_now == ["DS2"]
    assert not diff.empty


def test_steady_nonzero_operand_is_enabler_not_trigger() -> None:
    """An operand that was already nonzero is an enabler (nonzero_now), not a
    trigger (changed) — the burner door-held-open case."""
    prog = _sum_program()
    node = _writer_node(prog)
    plc = PLC(prog, dt=0.010)
    plc.patch({"DS1": 3})
    plc.step()  # DS1 steady-nonzero from here
    plc.patch({"DS2": 5})
    plc.step()  # DS2 flips this scan

    scan = plc.history.scan_ids()[-1]
    diff = recorded_read_changes(plc.history, node, scan)

    assert diff.changed == [("DS2", 0, 5)]
    assert diff.nonzero_now == ["DS1", "DS2"]  # DS1 held nonzero, DS2 newly so


def test_first_scan_has_no_predecessor_diff() -> None:
    """At the first retained scan there is no N-1, so nothing is 'changed' — the
    diff degrades to the non-zero-now enablers (here none; all operands zero)."""
    prog = _sum_program()
    node = _writer_node(prog)
    plc = PLC(prog, dt=0.010)
    plc.step()

    first = plc.history.scan_ids()[0]
    diff = recorded_read_changes(plc.history, node, first)

    assert diff.changed == []  # no N-1 to diff against
    assert diff.nonzero_now == []


def test_prev_scan_id_override_is_used() -> None:
    """An explicit *prev_scan_id* diffs against that scan, not the adjacent one."""
    prog = _sum_program()
    node = _writer_node(prog)
    plc = PLC(prog, dt=0.010)
    plc.step()  # baseline: all zero
    base = plc.history.scan_ids()[-1]
    plc.patch({"DS1": 4})
    plc.step()
    plc.patch({"DS2": 6})
    plc.step()  # now DS1=4, DS2=6

    scan = plc.history.scan_ids()[-1]
    diff = recorded_read_changes(plc.history, node, scan, prev_scan_id=base)

    # Against the all-zero baseline two scans back, both operands changed.
    assert diff.changed == [("DS1", 0, 4), ("DS2", 0, 6)]
    assert diff.nonzero_now == ["DS1", "DS2"]


def test_empty_footprint_is_empty_diff() -> None:
    """A writer with no data reads has nothing to cross."""

    class _NoReads:
        data_reads = frozenset()

    result = recorded_read_changes(_history_stub(), _NoReads(), 0)  # type: ignore[arg-type]
    assert result == ReadDiff(footprint=frozenset())
    assert result.empty


# --------------------------------------------------------------------------
# end-to-end: cause() crosses the opaque calc-sum (today it dead-ends)
# --------------------------------------------------------------------------


def test_cause_crosses_calc_sum_to_changed_operand() -> None:
    prog = _sum_program()
    runner = PLC(prog, dt=0.010)
    runner.step()
    runner.patch({"DS2": 5})
    runner.step()  # Total 0 -> 5 through the opaque calc

    chain = runner.cause("Total")

    assert chain is not None
    assert "DS2" in chain.tags()  # crossed the calc to its changed operand
    calc_step = next(s for s in chain.steps if s.transition.tag_name == "Total")
    assert "DS2" in [t.tag_name for t in calc_step.triggers]


def test_cause_attributes_nonzero_to_truthy_operands() -> None:
    """Burner shape: != 0 attributes to the operand that flipped (trigger) and
    the operand already non-zero (enabler) — no sign reasoning."""
    prog = _sum_program()
    runner = PLC(prog, dt=0.010)
    runner.patch({"DS1": 3})
    runner.step()  # DS1 steady-nonzero
    runner.patch({"DS2": 5})
    runner.step()  # DS2 flips this scan; Total 3 -> 8

    chain = runner.cause("Total")

    assert chain is not None
    calc_step = next(s for s in chain.steps if s.transition.tag_name == "Total")
    assert "DS2" in [t.tag_name for t in calc_step.triggers]
    assert "DS1" in [e.tag_name for e in calc_step.enablers]


def _gated_sum_program() -> Program:
    """Like ``_sum_program`` but the calc is gated by a held ``Enable``."""
    blk = Block("DS", TagType.INT, 1, 5)
    enable = Bool("Enable", external=True)
    total = Int("Total")
    flag = Bool("Flag")
    with Program() as prog:
        with Rung(total != 0):
            out(flag)
        with Rung(enable):
            calc(blk.select(1, 3).sum(), total)
    return prog


def test_cause_crosses_gated_calc_sum() -> None:
    """Case 2: a conditioned writer explained only by its held gate still
    crosses to the operands — the gate is an enabler, the changed operand a
    trigger, folded into one step."""
    prog = _gated_sum_program()
    runner = PLC(prog, dt=0.010)
    runner.patch({"Enable": True})
    runner.step()  # Enable 0 -> 1
    runner.step()  # Enable held (its transition is now > 1 scan old)
    runner.patch({"DS2": 5})
    runner.step()  # DS2 flips, Total -> 5 with the gate a held enabler

    chain = runner.cause("Total")

    assert chain is not None
    calc_step = next(s for s in chain.steps if s.transition.tag_name == "Total")
    assert "DS2" in [t.tag_name for t in calc_step.triggers]
    assert "Enable" in [e.tag_name for e in calc_step.enablers]
    assert "DS2" in chain.tags()


class _HistoryStub:
    def scan_ids(self) -> list[int]:
        return [0]

    def at(self, _scan: int) -> object:
        class _State:
            tags: dict = {}

        return _State()


def _history_stub() -> _HistoryStub:
    return _HistoryStub()
