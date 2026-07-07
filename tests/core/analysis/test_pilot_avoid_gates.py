"""``avoid=`` excludes routes, operator actions, and observed scan states.

The user's contract:

    avoid X = do not take a path that depends on X.  avoid excludes routes,
    operator actions, and observed scan states that satisfy the predicate.
    Momentary commands are treated as actions, not just settled states.

Three internal gates enforce it:

* **Route gate** (``_prepare_route`` / ``_route_forces``) — a route whose trace
  forces the avoided predicate is pruned before the drive begins.
* **Action gate** (``_try_action_batch`` — the convergence seam for command
  pulses, prescribed batches and widening; ``candidates`` prerequisite holds;
  ``_hold_allowed`` for investigation-installed corrective holds) — a candidate
  whose overlaid action makes the predicate true is rejected *before* the pulse.
* **Scan gate** (``verify.verify_gates``) — extended from settled-state-only to
  transient coverage: the pulse scan and every coast snapshot, so there is no
  "two-scan wink" where the avoided condition blips true mid-trial and settles
  false again.

Arity: ``avoid=`` accepts one condition or a tuple/list = **union** of
exclusions (each avoided independently); ``avoid=And(A, B)`` avoids only the
combined state.  ``via=`` is unchanged (conjunction).

Every gate test here is hand-driveable and honestly-failing — the ground-truth
``*_reachable`` checks pin that the target really is reachable so a decline is a
genuine avoid-exclusion, not a missing feature.
"""

from __future__ import annotations

from pyrung import (
    PLC,
    And,
    Bool,
    Int,
    Or,
    Program,
    Rung,
    Timer,
    calc,
    copy,
    on_delay,
    out,
)

# ---------------------------------------------------------------------------
# 1. Momentary-command action case
# ---------------------------------------------------------------------------


def _momentary_program(with_alternate: bool) -> tuple[Program, object, dict[str, object]]:
    """``Filling`` reachable by pressing the momentary command ``Cmd`` (fast) or,
    when *with_alternate*, by holding ``Slow`` through a dwell (slow)."""
    Cmd = Bool("Cmd", external=True)
    Slow = Bool("Slow", external=True)
    Step = Int("Step", default=1)
    Dwell = Timer.clone("Dwell")
    Filling = Bool("Filling")

    with Program(strict=False) as prog:
        if with_alternate:
            # Two ways to Step==2: the momentary command Cmd, or the slow dwell
            # (hold Slow, let the timer complete) — OR arms of one writer.
            with Rung(Or(Cmd, Dwell.Done)):
                copy(2, Step)
            with Rung(Slow, Step == 1):
                on_delay(Dwell, 30, "ms")
        else:
            with Rung(Cmd):
                copy(2, Step)
        with Rung(Step == 2):
            out(Filling)

    return prog, Filling, {"Cmd": Cmd, "Slow": Slow}


def test_momentary_command_reachable_by_hand() -> None:
    """Ground truth: pressing ``Cmd`` reaches ``Filling`` — so a decline that
    names ``Cmd`` is a genuine avoid-exclusion, not a miss."""
    prog, _target, _tags = _momentary_program(with_alternate=True)
    plc = PLC(prog, dt=0.010)
    plc.patch({"Cmd": True})
    plc.step()
    assert plc.state.tags["Filling"] is True


def test_avoid_momentary_command_takes_alternate() -> None:
    """``avoid=Cmd`` must not press the momentary command and must reach via the
    slow dwell route instead."""
    prog, target, tags = _momentary_program(with_alternate=True)
    plc = PLC(prog, dt=0.010)
    path = plc.how(target, avoid=tags["Cmd"], max_scans=2000)
    assert path.reachable, path.reason

    inputs = [(t, v) for step in path.journal for t, v in step.inputs]
    assert all(t != "Cmd" for t, _v in inputs), f"Cmd was pressed: {inputs}"

    replay = path.replay()
    assert replay.state.tags["Filling"] is True


def test_avoid_momentary_command_no_alternate_declines_naming_it() -> None:
    """With no alternate, ``avoid=Cmd`` yields an honest unreachable Path whose
    reason names the avoided command."""
    prog, target, tags = _momentary_program(with_alternate=False)
    plc = PLC(prog, dt=0.010)
    path = plc.how(target, avoid=tags["Cmd"], max_scans=1000)
    assert not path.reachable
    assert "Cmd" in (path.reason or ""), path.reason


# ---------------------------------------------------------------------------
# 2. Route case (route gate already exists — pinning coverage)
# ---------------------------------------------------------------------------


def _route_program() -> tuple[Program, object, dict[str, object]]:
    Auto = Bool("Auto", external=True)
    Manual = Bool("Manual", external=True)
    Running = Bool("Running")
    with Program() as prog:
        with Rung(Or(Auto, Manual)):
            out(Running)
    return prog, Running, {"Auto": Auto, "Manual": Manual}


def test_avoid_route_picks_the_other_route() -> None:
    """Two routes to ``Running`` (Auto | Manual); ``avoid=Manual`` picks Auto."""
    prog, target, tags = _route_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(target, avoid=tags["Manual"], max_scans=1000)
    assert path.reachable, path.reason
    inputs = [(t, v) for step in path.journal for t, v in step.inputs]
    assert all(t != "Manual" for t, _v in inputs), f"Manual was used: {inputs}"


# ---------------------------------------------------------------------------
# 3. Transient-wink case (scan gate transient coverage)
# ---------------------------------------------------------------------------


def _wink_program() -> tuple[Program, object, object, dict[str, object]]:
    """A single ``Go`` pulse latches ``Run``; ``Step`` then advances 0→1→2→3 over
    the coast, passing ``Step == 2`` where ``Mid`` blips true for one scan before
    settling at ``Step == 3`` (``Target`` true, ``Mid`` false)."""
    Go = Bool("Go", external=True)
    Run = Bool("Run")
    Step = Int("Step", default=0)
    Mid = Bool("Mid")
    Target = Bool("Target")
    with Program() as prog:
        with Rung(Or(Go, Run)):
            out(Run)
        with Rung(Run, Step < 3):
            calc(Step + 1, Step)
        with Rung(Step == 2):
            out(Mid)
        with Rung(Step == 3):
            out(Target)
    return prog, Target, Mid, {"Go": Go}


def test_wink_reachable_by_hand_and_mid_blips() -> None:
    """Ground truth: the target is reachable, and ``Mid`` really does blip true
    mid-run — so rejecting it is the scan gate doing its job."""
    prog, _target, _mid, _tags = _wink_program()
    plc = PLC(prog, dt=0.010)
    plc.patch({"Go": True})
    saw_mid = False
    reached = False
    for _ in range(8):
        plc.step()
        if plc.state.tags["Mid"] is True:
            saw_mid = True
        if plc.state.tags["Target"] is True:
            reached = True
    assert saw_mid, "Mid never blipped — program does not exercise the wink"
    assert reached


def test_avoid_transient_wink_is_rejected() -> None:
    """``avoid=Mid`` rejects the only path (it winks ``Mid`` mid-coast) and
    honestly declines, naming the avoided condition."""
    prog, target, _mid, tags = _wink_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(target, avoid=Bool("Mid"), max_scans=1500)
    assert not path.reachable
    assert "Mid" in (path.reason or ""), path.reason


# ---------------------------------------------------------------------------
# 4. Multi-avoid union case
# ---------------------------------------------------------------------------


def _multi_avoid_program(with_clean: bool) -> tuple[Program, object, dict[str, object]]:
    A = Bool("A", external=True)
    B = Bool("B", external=True)
    C = Bool("C", external=True)
    Step = Int("Step", default=1)
    Filling = Bool("Filling")
    with Program(strict=False) as prog:
        if with_clean:
            with Rung(Or(A, B, C)):
                copy(2, Step)
        else:
            with Rung(Or(A, B)):
                copy(2, Step)
        with Rung(Step == 2):
            out(Filling)
    return prog, Filling, {"A": A, "B": B, "C": C}


def test_avoid_tuple_union_reaches_via_clean_lever() -> None:
    """``avoid=(A, B)`` excludes both single-tag paths; the clean ``C`` path is
    used."""
    prog, target, tags = _multi_avoid_program(with_clean=True)
    plc = PLC(prog, dt=0.010)
    path = plc.how(target, avoid=(tags["A"], tags["B"]), max_scans=1000)
    assert path.reachable, path.reason
    inputs = [(t, v) for step in path.journal for t, v in step.inputs]
    assert all(t not in ("A", "B") for t, _v in inputs), f"A or B used: {inputs}"


def test_avoid_list_union_matches_tuple() -> None:
    """``avoid=[A, B]`` is the same union as ``avoid=(A, B)``."""
    prog, target, tags = _multi_avoid_program(with_clean=True)
    plc = PLC(prog, dt=0.010)
    path = plc.how(target, avoid=[tags["A"], tags["B"]], max_scans=1000)
    assert path.reachable, path.reason
    inputs = [(t, v) for step in path.journal for t, v in step.inputs]
    assert all(t not in ("A", "B") for t, _v in inputs), f"A or B used: {inputs}"


def test_avoid_union_no_clean_lever_declines_naming_it() -> None:
    """With no clean lever, ``avoid=(A, B)`` declines honestly, naming an avoided
    condition (the trace collapses the Or arms to one, so it names the arm it
    surfaced — either ``A`` or ``B`` — not necessarily both)."""
    prog, target, tags = _multi_avoid_program(with_clean=False)
    plc = PLC(prog, dt=0.010)
    path = plc.how(target, avoid=(tags["A"], tags["B"]), max_scans=800)
    assert not path.reachable
    reason = path.reason or ""
    assert "avoid excludes" in reason, reason
    assert ("A" in reason) or ("B" in reason), reason


def test_avoid_composite_and_excludes_only_the_joint_state() -> None:
    """``avoid=And(A, B)`` excludes only the *combined* state, so pressing a
    single lever is allowed and the target is reached."""
    prog, target, tags = _multi_avoid_program(with_clean=False)
    plc = PLC(prog, dt=0.010)
    path = plc.how(target, avoid=And(tags["A"], tags["B"]), max_scans=1000)
    assert path.reachable, path.reason
    inputs = [(t, v) for step in path.journal for t, v in step.inputs]
    assert any(t in ("A", "B") for t, _v in inputs), f"no single lever used: {inputs}"


# ---------------------------------------------------------------------------
# 5. Investigation-hold admissibility seam
# ---------------------------------------------------------------------------


def test_avoid_hold_is_inadmissible() -> None:
    """A corrective hold that drives an avoided tag is inadmissible — the seam
    every corrective-hold install site (investigate/corrections) routes through.
    """
    from pyrung.core.analysis.pilot._ops import _avoid_forces, _hold_allowed
    from pyrung.core.analysis.pilot.types import _AvoidMember, _AvoidPredicate

    avoid = _AvoidPredicate(
        (_AvoidMember(name="X", pred=lambda s: bool(s.get("X")), tags=frozenset({"X"})),)
    )

    class _Ctx:
        avoid_pred = avoid
        resting = {"X": False, "Y": False}
        compass = None
        blocked_route_actions: frozenset = frozenset()

        def route_allowed(self, pair: tuple[str, object]) -> bool:
            return True

    ctx = _Ctx()
    assert _avoid_forces(ctx, [("X", True)])
    assert not _avoid_forces(ctx, [("Y", True)])
    # A hold that drives the avoided tag is a path that depends on it → rejected.
    assert not _hold_allowed(ctx, ("X", True))
    # A hold on an unrelated tag is fine.
    assert _hold_allowed(ctx, ("Y", True))
