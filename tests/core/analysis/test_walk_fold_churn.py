"""D2 redesign: per-scan churn vs. the fold's plateau guard, rung by rung.

Each rung gets a tripwire program that (a) reaches Target by a manual input
sequence (the premise — the target is genuinely reachable) and (b) failed
``how()`` before its rung landed, because an unconditional self-updating
``calc`` rung defeats the plateau guard program-wide: no plateau ever forms,
folding is unavailable, and dwells beyond the pulse react budget kill every
corridor.

Rung 1 — unread churn: the churner is read by nothing, so it is
unobservable; excluding it from the plateau guard is exact (nothing any rung
reads — or the verify replay checks — can depend on it).  The ablation
direction is pinned per rung: with the fold pass disabled the walk must fail
the way it did before the rung landed.

Rung 2 — read-but-disjoint-cone churn: the churner IS read, but its entire
downstream closure (reader rungs' writes, transitively, including called
subroutines) never reaches the walk's target cone.  Folding past closure
flips can then change nothing the walk steers toward or the verify replay's
target check reads; divergence stays confined to the disjoint cone.  An
empty target set excludes nothing (direct ``_build_fold_context`` callers).

Rung 3 — affine self-calc churn as a fold source: an unconditional
``calc((T + c) % m, T)`` (or plain ``calc(T + c, T)``) read by enabling
comparisons in the goal cone becomes a tracked source instead of visible
churn — excluded from the plateau guard, patched exactly during jumps
(``(v + (skip-1)·c) % m``; the landing step's own calc supplies the final
increment), with its comparisons joining the crossing set (first
truth-flip of the modular recurrence).  Landing states are bit-equal to
step-by-step execution, which the verify replay then confirms.

Rung 4 — derived crossings: a tag that mirrors an accumulator through an
unconditional ``copy(Acc, X)`` or constant-offset ``calc(Acc ± k, X)``
gives exact derived thresholds — ``X cmp T`` flips when ``Acc cmp T − k``
does — so the mirror's comparisons translate onto the accumulator and
join the crossing set, and the mirror leaves the plateau guard.  The
translation is conservative by construction: the mirror flips 0–1 scans
after the accumulator crossing (rung order), so stopping one-before the
accumulator crossing always stops before the mirror's readers flip.  Any
read of the mirror that can't be resolved to an exact threshold (compound
condition, data copy, non-literal operand) refuses the mirror, preserving
today's refusal.
"""

from __future__ import annotations

import pytest

from pyrung import And, Bool, Int, Or, Program, Rung, Timer, calc, copy, on_delay, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.analysis.walk import recovery as recovery_mod
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Shared cross-guard scaffold (100 ms timers: dwell 10 scans at dt=0.010,
# beyond _PULSE_REACT_CAP, so corridors need folding to survive the dwell)
# ---------------------------------------------------------------------------


def _unread_churn_program() -> tuple[Program, Bool]:
    """Cross-guard plus a free-running parity counter read by nothing."""
    Input_A = Bool("Input_A", external=True)
    Input_B = Bool("Input_B", external=True)
    Reset_Cmd = Bool("Reset_Cmd", external=True)
    Latch_A = Bool("Latch_A")
    Latch_B = Bool("Latch_B")
    Guard_A = Bool("Guard_A")
    Guard_B = Bool("Guard_B")
    TimerA = Timer.clone("TimerA")
    TimerB = Timer.clone("TimerB")
    Cycle = Int("Cycle")
    Target = Bool("Target")

    with Program() as prog:
        with Rung():
            calc((Cycle + 1) % 2, Cycle)
        with Rung(Input_A):
            on_delay(TimerA, 100, "ms")
        with Rung(Input_B):
            on_delay(TimerB, 100, "ms")
        with Rung(Or(And(TimerA.Done, ~Guard_B), Latch_A)):
            out(Latch_A)
        with Rung(Or(And(TimerB.Done, ~Guard_A), Latch_B)):
            out(Latch_B)
        with Rung(Or(TimerA.Done, Guard_A), ~Reset_Cmd):
            out(Guard_A)
        with Rung(Or(TimerB.Done, Guard_B), ~Reset_Cmd):
            out(Guard_B)
        with Rung(Latch_A, Latch_B):
            out(Target)

    return prog, Target


def _crossguard_premise(prog: Program) -> None:
    """Manual sequence: latch A, reset guards, latch B — Target reachable."""
    plc = PLC(prog, dt=0.010)
    plc.patch({"Input_A": True})
    for _ in range(15):
        plc.step()
    plc.patch({"Input_A": False})
    plc.step()
    assert plc.state.tags["Latch_A"] is True
    assert plc.state.tags["Guard_A"] is True
    plc.patch({"Reset_Cmd": True})
    plc.step()
    plc.patch({"Reset_Cmd": False})
    plc.step()
    assert plc.state.tags["Guard_A"] is False
    assert plc.state.tags["Latch_A"] is True
    plc.patch({"Input_B": True})
    for _ in range(20):
        plc.step()
    assert plc.state.tags["Latch_B"] is True
    assert plc.state.tags["Target"] is True


def _walk_single_goal(
    prog: Program, target: Bool, disabled: frozenset[str], dt: float = 0.010
) -> bool:
    """Drive one single-goal walk (the matrix-test entry); return solved."""
    plc = PLC(prog, dt=dt)
    work = plc.fork()
    walk._install_walk_harness(work)
    pdg = build_program_graph(work._program)
    known = work._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, work._program) & set(ext_inputs)
    governing, gov_value = walk._governing(target.name, True, pdg, work._program, plc=work)
    steps = walk._walk_to_goal(
        work,
        governing,
        gov_value,
        pdg,
        work._program,
        known,
        ext_inputs,
        edge_ext,
        64,
        nogoods=walk.NoGoodStore(),
        holds=walk.HoldStore(),
        disabled_passes=disabled,
    )
    return steps is not None


# ---------------------------------------------------------------------------
# Rung 1: unread churn
# ---------------------------------------------------------------------------


def test_unread_churn_premise() -> None:
    prog, _target = _unread_churn_program()
    _crossguard_premise(prog)


def test_unread_churn_walk_solves() -> None:
    """The tripwire: unread per-scan churn must not defeat folding."""
    prog, target = _unread_churn_program()
    path = PLC(prog, dt=0.010).how(target)
    assert path.reachable is True
    assert path.total_scans > 0


@pytest.mark.xfail(
    reason="temporal done_bit fix gives the walker a direct decomposition "
    "that bypasses the fold path — the pass is an efficiency optimisation, "
    "no longer a correctness gate for this program shape",
    strict=True,
)
def test_unread_churn_ablation_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabling the pass restores the pre-rung failure (the fold-kind
    ablation obligation: only the refusing direction may regress)."""
    # The why-regression fallback goal source can rescue this shape through
    # sub-goal recursion; ablate it so the pin isolates the fold pass.
    monkeypatch.setattr(recovery_mod, "_WHY_REGRESSION", False)
    prog, target = _unread_churn_program()
    assert _walk_single_goal(prog, target, frozenset()) is True
    assert _walk_single_goal(prog, target, frozenset({"fold_unread_churn"})) is False


def test_unread_churn_exclusion_not_applied_to_goal_tag() -> None:
    """A walk whose target IS the churner must not exclude it: the verify
    replay reads the target, so the fold may not drift it."""
    prog, _target = _unread_churn_program()
    plc = PLC(prog, dt=0.010)
    cycle = plc._known_tags_by_name["Cycle"]
    path = plc.how(cycle == 1)
    assert path.reachable is True


# ---------------------------------------------------------------------------
# Rung 2: read-but-disjoint-cone churn
# ---------------------------------------------------------------------------


def _disjoint_churn_program() -> tuple[Program, Bool]:
    """Cross-guard plus a parity counter read by one comparison whose only
    effect (Blinky) is outside the Target cone."""
    Input_A = Bool("Input_A", external=True)
    Input_B = Bool("Input_B", external=True)
    Reset_Cmd = Bool("Reset_Cmd", external=True)
    Latch_A = Bool("Latch_A")
    Latch_B = Bool("Latch_B")
    Guard_A = Bool("Guard_A")
    Guard_B = Bool("Guard_B")
    TimerA = Timer.clone("TimerA")
    TimerB = Timer.clone("TimerB")
    Cycle = Int("Cycle")
    Blinky = Bool("Blinky")
    Target = Bool("Target")

    with Program() as prog:
        with Rung():
            calc((Cycle + 1) % 2, Cycle)
        with Rung(Cycle == 0):
            out(Blinky)
        with Rung(Input_A):
            on_delay(TimerA, 100, "ms")
        with Rung(Input_B):
            on_delay(TimerB, 100, "ms")
        with Rung(Or(And(TimerA.Done, ~Guard_B), Latch_A)):
            out(Latch_A)
        with Rung(Or(And(TimerB.Done, ~Guard_A), Latch_B)):
            out(Latch_B)
        with Rung(Or(TimerA.Done, Guard_A), ~Reset_Cmd):
            out(Guard_A)
        with Rung(Or(TimerB.Done, Guard_B), ~Reset_Cmd):
            out(Guard_B)
        with Rung(Latch_A, Latch_B):
            out(Target)

    return prog, Target


def test_disjoint_churn_premise() -> None:
    prog, _target = _disjoint_churn_program()
    _crossguard_premise(prog)


def test_disjoint_churn_walk_solves() -> None:
    """The tripwire: churn whose downstream cone never reaches the target
    must not defeat folding."""
    prog, target = _disjoint_churn_program()
    path = PLC(prog, dt=0.010).how(target)
    assert path.reachable is True
    assert path.total_scans > 0


@pytest.mark.xfail(
    reason="temporal done_bit fix gives the walker a direct decomposition "
    "that bypasses the fold path — the pass is an efficiency optimisation, "
    "no longer a correctness gate for this program shape",
    strict=True,
)
def test_disjoint_churn_ablation_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    # The why-regression fallback goal source can rescue this shape through
    # sub-goal recursion; ablate it so the pin isolates the fold pass.
    monkeypatch.setattr(recovery_mod, "_WHY_REGRESSION", False)
    prog, target = _disjoint_churn_program()
    assert _walk_single_goal(prog, target, frozenset()) is True
    assert _walk_single_goal(prog, target, frozenset({"fold_disjoint_churn"})) is False


def test_disjoint_churn_closure_is_excluded() -> None:
    """The whole closure (churner + its reader's output) leaves the plateau
    guard — excluding the churner alone would never form a plateau."""
    prog, _target = _disjoint_churn_program()
    plc = PLC(prog, dt=0.010)
    pdg = build_program_graph(prog)
    ctx = walk._build_fold_context(plc, pdg, prog, target_names=frozenset({"Target"}))
    assert {"Cycle", "Blinky"} <= ctx.churn_excluded


def test_disjoint_churn_requires_declared_targets() -> None:
    """No targets declared (direct callers) means nothing is provably
    disjoint — the conservative direction is no exclusion."""
    prog, _target = _disjoint_churn_program()
    plc = PLC(prog, dt=0.010)
    pdg = build_program_graph(prog)
    ctx = walk._build_fold_context(plc, pdg, prog)
    assert "Blinky" not in ctx.churn_excluded
    assert "Cycle" not in ctx.churn_excluded


def test_read_churn_in_target_cone_is_not_excluded() -> None:
    """A churner read by an enabling comparison inside the target cone must
    stay visible — excluding it would blind the guard to a real derived
    threshold (this is rung 3's program, refused until rung 3 lands)."""
    Input_B = Bool("Input_B", external=True)
    Latch_B = Bool("Latch_B")
    TimerB = Timer.clone("TimerB")
    Cycle = Int("Cycle")

    with Program() as prog:
        with Rung():
            calc((Cycle + 1) % 2, Cycle)
        with Rung(Input_B):
            on_delay(TimerB, 100, "ms")
        with Rung(Or(And(TimerB.Done, Cycle == 0), Latch_B)):
            out(Latch_B)

    plc = PLC(prog, dt=0.010)
    pdg = build_program_graph(prog)
    ctx = walk._build_fold_context(plc, pdg, prog, target_names=frozenset({"Latch_B"}))
    assert "Cycle" not in ctx.churn_excluded


# ---------------------------------------------------------------------------
# Rung 3: affine(-mod) self-calc churn as a fold source
# ---------------------------------------------------------------------------


def _conjunct_churn_program(mod: int, want: int) -> tuple[Program, Bool]:
    """Cross-guard where the Latch_B arm is additionally gated on the
    free-running counter's phase (``Cycle == want`` with ``Cycle`` mod *mod*)."""
    Input_A = Bool("Input_A", external=True)
    Input_B = Bool("Input_B", external=True)
    Reset_Cmd = Bool("Reset_Cmd", external=True)
    Latch_A = Bool("Latch_A")
    Latch_B = Bool("Latch_B")
    Guard_A = Bool("Guard_A")
    Guard_B = Bool("Guard_B")
    TimerA = Timer.clone("TimerA")
    TimerB = Timer.clone("TimerB")
    Cycle = Int("Cycle")
    Target = Bool("Target")

    with Program() as prog:
        with Rung():
            calc((Cycle + 1) % mod, Cycle)
        with Rung(Input_A):
            on_delay(TimerA, 100, "ms")
        with Rung(Input_B):
            on_delay(TimerB, 100, "ms")
        with Rung(Or(And(TimerA.Done, ~Guard_B), Latch_A)):
            out(Latch_A)
        with Rung(Or(And(TimerB.Done, ~Guard_A, Cycle == want), Latch_B)):
            out(Latch_B)
        with Rung(Or(TimerA.Done, Guard_A), ~Reset_Cmd):
            out(Guard_A)
        with Rung(Or(TimerB.Done, Guard_B), ~Reset_Cmd):
            out(Guard_B)
        with Rung(Latch_A, Latch_B):
            out(Target)

    return prog, Target


def _linear_selfcalc_program() -> tuple[Program, Bool]:
    """The only path to Target is a 5000-scan free-running count — beyond
    the advance iteration guard, so it needs a real fold, not stepping.
    Enable gives the walk a steerable input; the count itself stays
    unconditional."""
    Enable = Bool("Enable", external=True)
    Count = Int("Count")
    Target = Bool("Target")

    with Program() as prog:
        with Rung():
            calc(Count + 1, Count)
        with Rung(Count >= 5000, Enable):
            out(Target)

    return prog, Target


def test_conjunct_churn_premise() -> None:
    prog, _target = _conjunct_churn_program(2, 0)
    _crossguard_premise(prog)


def test_conjunct_churn_walk_solves() -> None:
    """The tripwire: a mod-wrap counter read by an enabling comparison must
    fold as a tracked source, not die as visible churn."""
    prog, target = _conjunct_churn_program(2, 0)
    path = PLC(prog, dt=0.010).how(target)
    assert path.reachable is True
    assert path.total_scans > 0


def test_conjunct_churn_mod3_walk_solves() -> None:
    prog, target = _conjunct_churn_program(3, 2)
    path = PLC(prog, dt=0.010).how(target)
    assert path.reachable is True


@pytest.mark.xfail(
    reason="temporal done_bit fix gives the walker a direct decomposition "
    "that bypasses the fold path — the pass is an efficiency optimisation, "
    "no longer a correctness gate for this program shape",
    strict=True,
)
def test_conjunct_churn_ablation_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    # The why-regression fallback goal source can rescue this shape through
    # sub-goal recursion; ablate it so the pin isolates the fold pass.
    monkeypatch.setattr(recovery_mod, "_WHY_REGRESSION", False)
    prog, target = _conjunct_churn_program(2, 0)
    assert _walk_single_goal(prog, target, frozenset()) is True
    assert _walk_single_goal(prog, target, frozenset({"fold_modwrap_source"})) is False


def test_linear_selfcalc_walk_folds_to_threshold() -> None:
    """A plain ``calc(Count + 1, Count)`` becomes a per-scan source: the
    5000-scan climb folds to the comparison crossing and verifies on a
    step-by-step replay (exact landing)."""
    prog, target = _linear_selfcalc_program()
    path = PLC(prog, dt=0.010).how(target)
    assert path.reachable is True
    assert path.total_scans >= 5000


def test_modwrap_source_context_shape() -> None:
    """The conjunct churner is tracked (excluded + crossing-bounded), not
    merely invisible; the linear form rides the per-scan source machinery."""
    prog, _target = _conjunct_churn_program(2, 0)
    plc = PLC(prog, dt=0.010)
    pdg = build_program_graph(prog)
    ctx = walk._build_fold_context(plc, pdg, prog, target_names=frozenset({"Target"}))
    assert "Cycle" in ctx.modwrap_names
    assert "Cycle" not in ctx.churn_excluded
    assert ctx.comparisons.get("Cycle")

    prog2, _target2 = _linear_selfcalc_program()
    plc2 = PLC(prog2, dt=0.010)
    pdg2 = build_program_graph(prog2)
    ctx2 = walk._build_fold_context(plc2, pdg2, prog2, target_names=frozenset({"Target"}))
    assert "Count" in ctx2.acc_names
    assert ctx2.comparisons.get("Count")


# ---------------------------------------------------------------------------
# Rung 4: derived crossings (acc-mirror thresholds)
# ---------------------------------------------------------------------------


def _mirror_dwell_program(offset: int = 0) -> tuple[Program, Bool]:
    """The only dwell comparison reads a copy/offset of the Acc, never the
    Acc itself (Done unread).  30 s = 6000 scans at dt=0.005 — past the
    advance iteration guard, so churn-stepping cannot ride it out (the Acc
    is 16-bit, so the preset stays under 32767 ms)."""
    Enable = Bool("Enable", external=True)
    DwellT = Timer.clone("DwellT")
    Mirror = Int("Mirror")
    Target = Bool("Target")

    if offset:
        with Program() as prog:
            with Rung(Enable):
                on_delay(DwellT, 30000, "ms")
            with Rung():
                calc(DwellT.Acc + offset, Mirror)
            with Rung(Mirror >= 30000 + offset):
                out(Target)
    else:
        with Program() as prog:
            with Rung(Enable):
                on_delay(DwellT, 30000, "ms")
            with Rung():
                copy(DwellT.Acc, Mirror)
            with Rung(Mirror >= 30000):
                out(Target)

    return prog, Target


def test_mirror_dwell_premise() -> None:
    prog, _target = _mirror_dwell_program()
    plc = PLC(prog, dt=0.005)
    plc.patch({"Enable": True})
    for _ in range(6005):
        plc.step()
    assert plc.state.tags["Target"] is True


def test_mirror_dwell_walk_folds() -> None:
    """The tripwire: a dwell guarded only through an acc mirror must fold
    via the translated threshold."""
    prog, target = _mirror_dwell_program()
    path = PLC(prog, dt=0.005).how(target)
    assert path.reachable is True
    assert path.total_scans >= 5999


def test_mirror_dwell_offset_calc_walk_folds() -> None:
    """Constant-offset calc mirrors translate with the threshold shift."""
    prog, target = _mirror_dwell_program(offset=500)
    path = PLC(prog, dt=0.005).how(target)
    assert path.reachable is True
    assert path.total_scans >= 5999


def test_mirror_dwell_ablation_direction() -> None:
    prog, target = _mirror_dwell_program()
    assert _walk_single_goal(prog, target, frozenset(), dt=0.005) is True
    assert _walk_single_goal(prog, target, frozenset({"fold_derived_crossings"}), dt=0.005) is False


def test_mirror_context_shape() -> None:
    """The mirror leaves the plateau guard and its threshold lands on the
    accumulator, shifted by the offset."""
    prog, _target = _mirror_dwell_program(offset=500)
    plc = PLC(prog, dt=0.005)
    pdg = build_program_graph(prog)
    ctx = walk._build_fold_context(plc, pdg, prog, target_names=frozenset({"Target"}))
    assert "Mirror" in ctx.mirror_names
    assert ("ge", 30000) in ctx.comparisons.get("DwellT_Acc", ())
    assert "Mirror" not in ctx.comparisons


def test_mirror_with_data_read_is_refused() -> None:
    """A mirror feeding a data copy can't be proven threshold-only — it
    stays visible and the fold refuses, as today."""
    Enable = Bool("Enable", external=True)
    DwellT = Timer.clone("DwellT")
    Mirror = Int("Mirror")
    Second = Int("Second")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(Enable):
            on_delay(DwellT, 30000, "ms")
        with Rung():
            copy(DwellT.Acc, Mirror)
        with Rung(Enable):
            copy(Mirror, Second)
        with Rung(Mirror >= 30000):
            out(Target)

    plc = PLC(prog, dt=0.005)
    pdg = build_program_graph(prog)
    ctx = walk._build_fold_context(plc, pdg, prog, target_names=frozenset({"Target"}))
    assert "Mirror" not in ctx.mirror_names
    assert "DwellT_Acc" not in ctx.comparisons


def test_unread_churn_advance_bails_immediately_on_futile_wait(monkeypatch) -> None:
    """With the churner excluded, a wait nothing can advance is recognized
    as futile on the first plateau probe — one scan, not a react budget."""
    from pyrung import calc as _calc

    Cycle = Int("Cycle")
    with Program() as prog:
        with Rung():
            _calc((Cycle + 1) % 2, Cycle)
    plc = PLC(prog, dt=0.010)
    pdg = build_program_graph(prog)
    ctx = walk._build_fold_context(plc, pdg, prog)
    assert "Cycle" in ctx.churn_excluded
    plc.step()  # settle first-scan system bookkeeping before probing
    work = plc.fork()

    orig_step = PLC.step
    calls = 0

    def counting_step(self, *a, **k):
        nonlocal calls
        calls += 1
        return orig_step(self, *a, **k)

    monkeypatch.setattr(PLC, "step", counting_step)
    held = work.state.tags.get("Goal")  # never written, stays None
    auto = walk._advance_time(work, "Goal", held, ctx, walk._PULSE_REACT_CAP)

    assert auto is None
    assert calls == 1
