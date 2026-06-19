"""Stage 3: why-regression fallback goal source fixtures.

Two shapes pinned here:

1. **Or-gate fixture (the #8 shape).** Target's writer is gated by
   ``Or(S_A, S_B)`` with disjoint branch tags — the static extractor drops
   the Or, so prerequisites yield nothing for it.  With recovery disabled
   (``_MAX_RECHECK_ITERS=0``) and the explore's BFS node cap lowered
   (``_MAX_NODES=1``), the why-regression source is the sole path:
   frontier-terminated ``why()`` on the work fork names the nearest
   actionable sub-goals (CmdA, CmdB) through the live Or branch.
   Both directions pinned: succeeds with the source; fails without it.

   The recovery oracle and the explore BFS can both carry this shape at
   their default budgets (the simple probe_orgate.py shapes solve via
   recovery today), so the pin constrains both to isolate the
   why-regression source.

2. **Fill-shape fixture.** Synthetic replica of the live fill station: goal
   gated on ``PV >= Lower`` where ``Lower = SetPoint - Band``, ``Band``
   rests at 0, ``SetPoint`` written only by a gated tare, and ``PV`` is an
   ND analog with a pipeline domain.  Asserts the walker solves the shape
   independent of which mechanism carries it.
"""

from __future__ import annotations

import pytest

from pyrung import Bool, Int, Or, Program, Rung, calc, copy, latch, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.analysis.walk import explore
from pyrung.core.analysis.walk import recovery as recovery_mod
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _walk(
    prog: Program,
    target_tag: str,
    target_value: object = True,
    *,
    nd_domains: dict[str, tuple[int, ...]] | None = None,
    budget: int = 64,
) -> bool:
    """Drive a single-goal walk and return whether it solved."""
    plc = PLC(prog, dt=0.010)
    work = plc.fork()
    walk._install_walk_harness(work)
    pdg = build_program_graph(work._program)
    known = work._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, work._program) & set(ext_inputs)
    steps = walk._walk_to_goal(
        work,
        target_tag,
        target_value,
        pdg,
        work._program,
        known,
        ext_inputs,
        edge_ext,
        budget,
        nd_domains=nd_domains,
        nogoods=walk.NoGoodStore(),
        holds=walk.HoldStore(),
    )
    return steps is not None


# ---------------------------------------------------------------------------
# 1. Or-gate fixture (the #8 shape)
# ---------------------------------------------------------------------------


def _orgate_program() -> tuple[Program, str]:
    """Or-gate with disjoint branch tags, invisible to the static extractor.

    Each Or branch (S_A, S_B) requires TWO sequential external steers —
    an arming input (CmdA / CmdB) to latch a guard, then a shared enable
    (CmdEnable) to latch the branch tag — so no single steer from the
    explore's BFS can reach Done=True after the serial prerequisites
    (Go) are walked.

    Rung 0: CmdA → latch(Guard_A)
    Rung 1: Guard_A, CmdEnable → latch(S_A)
    Rung 2: CmdB → latch(Guard_B)
    Rung 3: Guard_B, CmdEnable → latch(S_B)
    Rung 4: Go, Or(S_A, S_B) → latch(Done)
    Rung 5: Done → out(Target)

    Ground truth: CmdA, CmdEnable, Go → Done → Target.
    """
    CmdA = Bool("CmdA", external=True)
    CmdB = Bool("CmdB", external=True)
    CmdEnable = Bool("CmdEnable", external=True)
    Go = Bool("Go", external=True)
    Guard_A = Bool("Guard_A")
    Guard_B = Bool("Guard_B")
    S_A = Bool("S_A")
    S_B = Bool("S_B")
    Done = Bool("Done")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(CmdA):
            latch(Guard_A)
        with Rung(Guard_A, CmdEnable):
            latch(S_A)
        with Rung(CmdB):
            latch(Guard_B)
        with Rung(Guard_B, CmdEnable):
            latch(S_B)
        with Rung(Go, Or(S_A, S_B)):
            latch(Done)
        with Rung(Done):
            out(Target)

    return prog, Target.name


def test_orgate_premise() -> None:
    """Ground truth: the target IS reachable (manual input sequence)."""
    prog, _ = _orgate_program()
    plc = PLC(prog, dt=0.010)
    plc.patch({"CmdA": True})
    plc.step()
    assert plc.state.tags["Guard_A"] is True
    plc.patch({"CmdEnable": True, "Go": True})
    plc.step()
    assert plc.state.tags["S_A"] is True
    assert plc.state.tags["Done"] is True
    assert plc.state.tags["Target"] is True


def _ablate_orgate_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable recovery and constrain the explore BFS so the why-regression
    source is the sole path through the Or-gate."""
    monkeypatch.setattr(recovery_mod, "_MAX_RECHECK_ITERS", 0)
    monkeypatch.setattr(explore, "_MAX_NODES", 1)


def test_orgate_solves_via_why_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    """With recovery disabled and explore capped, the why-regression source
    carries the Or-gate shape: frontier-terminated why() names CmdA (or CmdB)
    as the nearest actionable sub-goal through the live Or branch."""
    _ablate_orgate_recovery(monkeypatch)
    prog, target = _orgate_program()
    assert _walk(prog, target) is True


def test_orgate_ablated_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direction pin: without recovery, explore depth, AND why-regression,
    the walk fails honestly — neither the static extractor nor the
    corridor can surface the Or-gate's branch prerequisites."""
    _ablate_orgate_recovery(monkeypatch)
    monkeypatch.setattr(recovery_mod, "_WHY_REGRESSION", False)
    prog, target = _orgate_program()
    assert _walk(prog, target) is False


# ---------------------------------------------------------------------------
# 2. Fill-shape fixture
# ---------------------------------------------------------------------------


def _fill_program() -> tuple[Program, str, dict[str, tuple[int, ...]]]:
    """Synthetic replica of the live fill station shape.

    Rung 0: (unconditional) calc(SetPoint - Band, Lower)
    Rung 1: TareBtn → copy(PV, SetPoint)
    Rung 2: PV >= Lower → out(Target)

    PV is an ND analog input with a pipeline domain.  SetPoint starts at 10
    (a prior tare), Band rests at 0, so Lower=10.  The walker must either
    steer PV to a satisfying value (≥10) or tare to drop Lower.
    """
    PV = Int("PV", external=True, default=0)
    SetPoint = Int("SetPoint", default=10)
    Band = Int("Band", default=0)
    Lower = Int("Lower")
    TareBtn = Bool("TareBtn", external=True)
    Target = Bool("Target")

    with Program() as prog:
        with Rung():
            calc(SetPoint - Band, Lower)
        with Rung(TareBtn):
            copy(PV, SetPoint)
        with Rung(PV >= Lower):
            out(Target)

    nd_domains: dict[str, tuple[int, ...]] = {"PV": (0, 5, 10, 15, 20)}
    return prog, Target.name, nd_domains


def test_fill_premise() -> None:
    """Ground truth: tare-then-check makes PV >= Lower."""
    prog, _, _ = _fill_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    assert plc.state.tags["Lower"] == 10
    assert plc.state.tags["Target"] is False

    # Direct path: steer PV to a satisfying value.
    plc2 = PLC(prog, dt=0.010)
    plc2.patch({"PV": 10})
    plc2.step()
    assert plc2.state.tags["Target"] is True

    # Tare path: set TareBtn → SetPoint=PV=0, next scan Lower=0, PV>=0.
    plc3 = PLC(prog, dt=0.010)
    plc3.patch({"TareBtn": True})
    plc3.step()  # SetPoint=0 (tared), but Lower still 10 (calc ran first)
    plc3.step()  # Lower=0, PV=0 >= 0 → Target=True
    assert plc3.state.tags["Target"] is True


def test_fill_shape_solves() -> None:
    """The walker solves the fill shape — pins the capability independent
    of which mechanism (inequality chase, why-regression, or recovery)
    carries it."""
    prog, target, nd_domains = _fill_program()
    assert _walk(prog, target, nd_domains=nd_domains) is True
