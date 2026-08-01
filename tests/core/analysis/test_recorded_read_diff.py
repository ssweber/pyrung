"""Crossings Phase 1 — the recorded read-diff core (Tier 1).

``recorded_read_changes`` crosses an opaque writer mechanically: it diffs the
writer's pre-expanded ``data_reads`` footprint across the N-1 → N boundary and
reports which operands changed (triggers) and which are non-zero now (enablers).
No sign reasoning — the burner ``!= 0`` attribution falls out of the observed
operand values.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, calc, copy, out
from pyrung.core import call, subroutine
from pyrung.core.analysis.causal import recorded as recorded_module
from pyrung.core.analysis.causal.crossings_recorded import (
    ReadDiff,
    recorded_read_changes,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.memory_block import Block
from pyrung.core.program import branch, rung
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


def test_recorded_context_caches_pdg_read_sets(monkeypatch) -> None:
    prog = _sum_program()
    pdg = build_program_graph(prog)
    context = recorded_module._RecordedCauseContext(
        logic=prog.rungs,
        history=object(),
        rung_firings_fn=lambda _scan_id: {},
        pdg=pdg,
    )
    calls = {"footprint": 0, "rung_reads": 0}
    original_footprint = recorded_module._writer_footprint
    original_rung_reads = recorded_module._rung_static_reads

    def counted_footprint(*args):
        calls["footprint"] += 1
        return original_footprint(*args)

    def counted_rung_reads(*args):
        calls["rung_reads"] += 1
        return original_rung_reads(*args)

    monkeypatch.setattr(recorded_module, "_writer_footprint", counted_footprint)
    monkeypatch.setattr(recorded_module, "_rung_static_reads", counted_rung_reads)

    assert context.writer_footprint("Total", 1, None) == frozenset({"DS1", "DS2", "DS3"})
    assert context.writer_footprint("Total", 1, None) == frozenset({"DS1", "DS2", "DS3"})
    assert context.rung_static_reads(1, None) == frozenset({"DS1", "DS2", "DS3"})
    assert context.rung_static_reads(1, None) == frozenset({"DS1", "DS2", "DS3"})
    assert calls == {"footprint": 1, "rung_reads": 1}


def test_read_diff_names_changed_and_nonzero_operand() -> None:
    prog = _sum_program()
    node = _writer_node(prog)
    plc = PLC(prog, dt=0.010)
    plc.step()  # scan with all operands zero
    plc.patch({"DS2": 5})
    plc.step()  # DS2 flips 0 -> 5, Total -> 5

    scan = plc.history.scan_ids()[-1]
    diff = recorded_read_changes(plc.history, node.data_reads, scan)

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
    diff = recorded_read_changes(plc.history, node.data_reads, scan)

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
    diff = recorded_read_changes(plc.history, node.data_reads, first)

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
    diff = recorded_read_changes(plc.history, node.data_reads, scan, prev_scan_id=base)

    # Against the all-zero baseline two scans back, both operands changed.
    assert diff.changed == [("DS1", 0, 4), ("DS2", 0, 6)]
    assert diff.nonzero_now == ["DS1", "DS2"]


def test_empty_footprint_is_empty_diff() -> None:
    """A writer with no data reads has nothing to cross."""
    result = recorded_read_changes(_history_stub(), frozenset(), 0)  # type: ignore[arg-type]
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


def test_cause_crosses_subroutine_aggregate_writer() -> None:
    """A calc-sum inside a called subroutine is crossed; the step names the
    subroutine writer (node-aware) and the caller gate is an enabler."""
    blk = Block("DS", TagType.INT, 1, 5)
    enable = Bool("Enable", external=True)
    total = Int("Total")
    flag = Bool("Flag")

    @subroutine("Agg")
    def agg() -> None:
        with rung():
            calc(blk.select(1, 3).sum(), total)

    with Program() as prog:
        with Rung(total != 0):
            out(flag)
        with Rung(enable):
            call(agg)

    runner = PLC(prog, dt=0.010)
    runner.patch({"Enable": True})
    runner.step()
    runner.step()  # Enable held
    runner.patch({"DS2": 5})
    runner.step()  # Total -> 5 inside the subroutine

    chain = runner.cause("Total")

    assert chain is not None
    calc_step = next(s for s in chain.steps if s.transition.tag_name == "Total")
    assert calc_step.subroutine == "Agg"
    assert "DS2" in [t.tag_name for t in calc_step.triggers]
    assert "Enable" in [e.tag_name for e in calc_step.enablers]
    assert "DS2" in chain.tags()


def test_cause_crosses_branch_writer() -> None:
    """A calc-sum inside a branch (``branch_path`` non-empty) is crossed."""
    blk = Block("DS", TagType.INT, 1, 5)
    enable = Bool("Enable", external=True)
    total = Int("Total")
    flag = Bool("Flag")

    with Program() as prog:
        with Rung(total != 0):
            out(flag)
        with Rung():
            with branch(enable):
                calc(blk.select(1, 3).sum(), total)

    runner = PLC(prog, dt=0.010)
    runner.patch({"Enable": True})
    runner.step()
    runner.step()  # Enable held
    runner.patch({"DS2": 5})
    runner.step()

    chain = runner.cause("Total")

    assert chain is not None
    calc_step = next(s for s in chain.steps if s.transition.tag_name == "Total")
    assert "DS2" in [t.tag_name for t in calc_step.triggers]
    assert "DS2" in chain.tags()


def test_cause_crosses_multi_branch_writer_union() -> None:
    """When a rung writes one tag from two branches, the operand the *firing*
    branch read is found (no missed cause).  The static floor unions both
    branches' footprints; Tier 2 (active here via the interpreted replay) scopes
    to the branch that actually fired — either way DS11 is recovered.  See
    ``test_cause_gate_precise_multi_branch`` for the Tier-2 precision win."""
    blk = Block("DS", TagType.INT, 1, 20)
    sel_a = Bool("SelA", external=True)
    sel_b = Bool("SelB", external=True)
    total = Int("Total")
    flag = Bool("Flag")

    with Program() as prog:
        with Rung(total != 0):
            out(flag)
        with Rung():
            with branch(sel_a):
                calc(blk.select(1, 3).sum(), total)  # footprint DS1..DS3
            with branch(sel_b):
                calc(blk.select(10, 12).sum(), total)  # footprint DS10..DS12

    runner = PLC(prog, dt=0.010)
    runner.patch({"SelB": True})
    runner.step()
    runner.step()  # SelB held; branch B path
    runner.patch({"DS11": 7})
    runner.step()  # DS11 (branch B operand) flips Total -> 7

    chain = runner.cause("Total")

    assert chain is not None
    # The real operand is recovered via the unioned footprint (it was missed
    # before the union fix — branch A's footprint was picked).
    assert "DS11" in chain.tags()


# --------------------------------------------------------------------------
# Tier 2 — the interpreted read-tap: the captured reads replace the static
# footprint, fixing gate mis-attribution and unbounded indirect.
# --------------------------------------------------------------------------


def _two_branch_same_tag_program() -> Program:
    """One rung writes ``Total`` from two branches with disjoint footprints."""
    blk = Block("DS", TagType.INT, 1, 20)
    sel_a = Bool("SelA", external=True)
    sel_b = Bool("SelB", external=True)
    total = Int("Total")
    flag = Bool("Flag")
    with Program() as prog:
        with Rung(total != 0):
            out(flag)
        with Rung():
            with branch(sel_a):
                calc(blk.select(1, 3).sum(), total)  # footprint DS1..DS3
            with branch(sel_b):
                calc(blk.select(10, 12).sum(), total)  # footprint DS10..DS12
    return prog


def test_cause_gate_precise_multi_branch() -> None:
    """Tier-2 gate precision: only branch B fires, so even though branch A's
    operand DS1 is held non-zero, it is *not* attributed — the captured reads
    name only the firing branch.  Under the Tier-1 union DS1 would surface as a
    spurious enabler."""
    prog = _two_branch_same_tag_program()
    runner = PLC(prog, dt=0.010)
    runner.patch({"SelB": True, "DS1": 3})  # branch B fires; DS1 held non-zero
    runner.step()
    runner.step()  # SelB held
    runner.patch({"DS11": 7})
    runner.step()  # DS11 (branch B operand) flips Total -> 7

    chain = runner.cause("Total")

    assert chain is not None
    calc_step = next(s for s in chain.steps if s.transition.tag_name == "Total")
    assert "DS11" in [t.tag_name for t in calc_step.triggers]
    assert "DS11" in chain.tags()
    # Gate precision: branch A never fired, so its (non-zero) operand is absent.
    assert "DS1" not in chain.tags()
    assert "DS1" not in [e.tag_name for e in calc_step.enablers]


def test_cause_multi_writer_branches_no_cross_contamination() -> None:
    """Two branches of one rung write *different* tags and both fire.  Crossing
    ``X`` must not pull in ``Y``'s operand: the captured reads are scoped to the
    writer of the crossed tag (``& static_footprint``), never less precise than
    the static floor."""
    blk = Block("DS", TagType.INT, 1, 20)
    sel_a = Bool("SelA", external=True)
    sel_b = Bool("SelB", external=True)
    x = Int("X")
    y = Int("Y")
    flag = Bool("Flag")
    with Program() as prog:
        with Rung(x != 0):
            out(flag)
        with Rung():
            with branch(sel_a):
                calc(blk.select(1, 3).sum(), x)  # writes X, reads DS1..DS3
            with branch(sel_b):
                calc(blk.select(10, 12).sum(), y)  # writes Y, reads DS10..DS12

    runner = PLC(prog, dt=0.010)
    runner.patch({"SelA": True, "SelB": True})  # both branches fire
    runner.step()
    runner.step()
    runner.patch({"DS2": 5, "DS11": 7})
    runner.step()  # X -> 5 (via DS2), Y -> 7 (via DS11)

    chain = runner.cause("X")

    assert chain is not None
    calc_step = next(s for s in chain.steps if s.transition.tag_name == "X")
    assert "DS2" in [t.tag_name for t in calc_step.triggers]
    # No contamination from the sibling branch that wrote Y.
    assert "DS11" not in chain.tags()
    assert "DS11" not in [t.tag_name for t in calc_step.triggers]


def _indirect_copy_program() -> Program:
    """``Dest = DS[Ptr]`` — ``Ptr`` is unbounded, so the PDG cannot enumerate the
    resolved source address (Tier 1 sees only ``Ptr``)."""
    blk = Block("DS", TagType.INT, 1, 100)
    ptr = Int("Ptr", external=True)  # no choices / min / max -> unbounded
    dest = Int("Dest")
    flag = Bool("Flag")
    with Program() as prog:
        with Rung(dest != 0):
            out(flag)
        with Rung():
            copy(blk[ptr], dest)
    return prog


def test_unbounded_indirect_footprint_drops_resolved_address() -> None:
    """Premise guard: the static footprint of the indirect copy is just the
    pointer — the resolved address is *not* statically enumerable."""
    prog = _indirect_copy_program()
    node = next(n for n in build_program_graph(prog).rung_nodes if "Dest" in n.writes)
    assert node.data_reads == frozenset({"Ptr"})  # DS<n> absent — unbounded


def test_cause_crosses_unbounded_indirect_to_resolved_address() -> None:
    """Tier 2: the interpreted replay observes the *resolved* address the copy
    read (``DS50``) and attributes ``Dest``'s change to it — Tier 1's static
    footprint (only ``Ptr``) misses it entirely."""
    prog = _indirect_copy_program()
    runner = PLC(prog, dt=0.010)
    runner.patch({"Ptr": 50})  # point at DS50 (held from here)
    runner.step()
    runner.step()  # Ptr held; Dest still 0
    runner.patch({"DS50": 9})
    runner.step()  # DS50 0 -> 9, so Dest 0 -> 9 through the indirect copy

    chain = runner.cause("Dest")

    assert chain is not None
    copy_step = next(s for s in chain.steps if s.transition.tag_name == "Dest")
    assert "DS50" in [t.tag_name for t in copy_step.triggers]
    assert "DS50" in chain.tags()


def test_cross_falls_back_to_static_without_replay() -> None:
    """Without a ``node_reads_fn`` (no interpreted replay wired) the cross uses
    the static footprint — Tier 1 behaviour, unchanged."""
    prog = _sum_program()
    runner = PLC(prog, dt=0.010)
    runner.step()
    runner.patch({"DS2": 5})
    runner.step()
    scan = runner.history.scan_ids()[-1]
    pdg = build_program_graph(prog)
    writer_rung = next(i for i, _ in enumerate(prog.rungs) if "Total" in pdg.rung_nodes[i].writes)

    context = recorded_module._RecordedCauseContext(
        logic=prog.rungs,
        history=runner.history,
        rung_firings_fn=lambda _scan_id: {},
        pdg=pdg,
        node_reads_fn=None,  # no replay -> static fallback
    )
    crossed = recorded_module._cross_opaque_data_reads(
        context,
        recorded_module._RecordedWriter(
            tag_name="Total",
            rung_idx=writer_rung,
            sub_name=None,
            scan_id=scan,
            rung=prog.rungs[writer_rung],
        ),
    )

    assert crossed is not None
    triggers = crossed.triggers
    assert "DS2" in [t.tag_name for t in triggers]


class _HistoryStub:
    def scan_ids(self) -> list[int]:
        return [0]

    def at(self, _scan: int) -> object:
        class _State:
            tags: dict = {}

        return _State()


def _history_stub() -> _HistoryStub:
    return _HistoryStub()
