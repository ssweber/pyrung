"""Consumed-same-scan handshakes (findings §2a, the PackML mode_change shape).

A handshake tag produced and cleared within one scan (producer rung sets
it, the consumer acts on it, a later rung clears it) is never true at a
scan boundary.  Pre-fix, ``_unsatisfied_conditions`` spawned boundary goals
for such tags; the goals were structurally unreachable, recovery burned its
rounds on them, and the walk reported a FALSE ``unsolvable`` certificate
for a reachable target.

Two mechanisms are pinned here:

- ``_is_scan_transient`` — the static proof that a tag is back at its
  default at every boundary (conservative: anything unprovable is not
  transient).
- ``transient_handshake`` (widening pass) — bundles the transient gate's
  producer inputs with the consumer writer's own externals and its
  copy-source value binding into one simultaneous multi-input steer, so
  the whole chain fires mid-scan.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, copy, latch, out, reset, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.analysis.walk.priors import _is_scan_transient
from pyrung.core.runner import PLC


def _handshake_program():
    """R1 latches ReqBool on rise(Req) when ModeSel is valid; R2 consumes it
    (copies ModeSel into Mode); R3 clears it — all in one scan.  Ground
    truth: ModeSel=2 + Req pulse reaches Target in one scan."""
    Req = Bool("Req", external=True)
    ModeSel = Int("ModeSel", external=True)
    ReqBool = Bool("ReqBool")
    Mode = Int("Mode")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(rise(Req), ModeSel >= 1, ModeSel <= 3):
            latch(ReqBool)
        with Rung(ReqBool, Mode == 0):
            copy(ModeSel, Mode)
        with Rung(ReqBool):
            reset(ReqBool)
        with Rung(Mode == 2):
            out(Target)

    return prog, Target


def test_handshake_ground_truth() -> None:
    prog, _target = _handshake_program()
    plc = PLC(prog, dt=0.010)
    plc.patch({"ModeSel": 2})
    plc.step()
    plc.patch({"Req": True})
    plc.step()
    assert plc.state.tags["Mode"] == 2
    assert plc.state.tags["Target"] is True


def test_handshake_walk_solves_with_simultaneous_bundle() -> None:
    """The walk reaches the handshake-gated target — pre-fix this returned a
    false ``unsolvable`` certificate (the ReqBool boundary goal is
    structurally unreachable, but the target is not)."""
    prog, target = _handshake_program()
    plc = PLC(prog, dt=0.010)
    plc.step()

    path = plc.how(target)
    assert path.reachable
    # The chain only fires when the request edge and the valid mode value
    # land in the same scan — and the copy-source binding must pick the
    # goal value (2), not merely an inequality-satisfying one.
    patches: dict = {}
    for step in path.steps:
        if step.action:
            patches.update(step.action)
    assert patches.get("Req") is True
    assert patches.get("ModeSel") == 2


def test_handshake_ablated_refuses() -> None:
    """Disabling ``transient_handshake`` removes the bundles; the walk
    regresses in the refusing direction (no false plan, no solve)."""
    prog, target = _handshake_program()

    plc = PLC(prog, dt=0.010)
    work = plc.fork()
    walk._install_walk_harness(work)
    pdg = build_program_graph(work._program)
    known = work._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, work._program) & set(ext_inputs)

    def run(disabled: frozenset[str]):
        w = plc.fork()
        walk._install_walk_harness(w)
        return walk._walk_to_goal(
            w,
            target.name,
            True,
            pdg,
            w._program,
            known,
            ext_inputs,
            edge_ext,
            64,
            nogoods=walk.NoGoodStore(),
            holds=walk.HoldStore(),
            disabled_passes=disabled,
        )

    assert run(frozenset()) is not None
    assert run(frozenset({"transient_handshake"})) is None


# ---------------------------------------------------------------------------
# _is_scan_transient units: the static proof and its conservative refusals
# ---------------------------------------------------------------------------


def _transient_detect(prog, name: str) -> bool:
    pdg = build_program_graph(prog)
    plc = PLC(prog, dt=0.010)
    return _is_scan_transient(name, pdg, prog, plc._known_tags_by_name)


def test_transient_detected_for_handshake() -> None:
    prog, _target = _handshake_program()
    assert _transient_detect(prog, "ReqBool")


def test_transient_refuses_external_input() -> None:
    prog, _target = _handshake_program()
    assert not _transient_detect(prog, "Req")


def test_transient_refuses_ote_written_tag() -> None:
    """An OTE tag reflects its condition at the boundary — not transient."""
    A = Bool("A", external=True)
    B = Bool("B")

    with Program() as prog:
        with Rung(A):
            out(B)

    assert not _transient_detect(prog, "B")


def test_transient_refuses_clearer_before_producer() -> None:
    """A clearer ABOVE the producer leaves the tag set across the boundary."""
    Go = Bool("Go", external=True)
    T = Bool("T")

    with Program() as prog:
        with Rung(T):
            reset(T)
        with Rung(rise(Go)):
            latch(T)

    assert not _transient_detect(prog, "T")


def test_transient_refuses_conditionally_gated_clearer() -> None:
    """A clearer gated on another tag (And) may not fire when T is set —
    ordinary latch/reset pairs must keep their boundary goals."""
    Go = Bool("Go", external=True)
    Ack = Bool("Ack", external=True)
    T = Bool("T")

    with Program() as prog:
        with Rung(rise(Go)):
            latch(T)
        with Rung(T, Ack):
            reset(T)

    assert not _transient_detect(prog, "T")


# ---------------------------------------------------------------------------
# The full PackML shape: ack-cleared HMI bits + cross-scope clearer via the
# subroutine call gate + transient copy-source (findings §2a on the template)
# ---------------------------------------------------------------------------


def _packml_chain_program():
    """Distilled from the Tumbler/Dryer template's UnitMode protocol.

    - ``ChgReq``/``ProdMode`` are HMI bits the PROGRAM resets (acknowledge
      pattern) — they have writers, so their TagRole is not INPUT.
    - ``ReqBool`` is transient cross-scope: producer in main, unconditional
      clearer inside the sub whose CALL GATE (``ReqBool == 1``) fires when
      set.
    - ``UnitMode`` is transient same-scope and is the copy-SOURCE of the
      goal value (``ModeCur = 1``).  Its declared default is 5 — like the
      template's ``C_UnitMode`` — while the protocol RESTS at 0 (what the
      clearer writes): the rest value must be inferred from the clearers,
      not the declared default.

    Ground truth: one simultaneous patch ``{ProdMode: True, ChgReq: True}``.
    """
    from pyrung import Int, call, copy, subroutine

    ProdMode = Bool("ProdMode", external=True)
    ChgReq = Bool("ChgReq", external=True)
    UnitMode = Int("UnitMode", default=5)
    ReqBool = Int("ReqBool")
    ModeCur = Int("ModeCur")
    Target = Bool("Target")

    @subroutine("mode_sub")
    def mode_sub():
        with Rung(ProdMode):
            copy(1, UnitMode, oneshot=True)
        with Rung(UnitMode >= 1, UnitMode <= 3):
            copy(UnitMode, ModeCur)
        with Rung():
            copy(0, ReqBool)
        with Rung():
            copy(0, UnitMode)
        with Rung():
            reset(ChgReq)

    with Program() as prog:
        with Rung(ChgReq):
            copy(1, ReqBool, oneshot=True)
        with Rung(ReqBool == 1):
            call(mode_sub)
        with Rung(ModeCur == 1):
            out(Target)

    return prog, Target


def test_packml_chain_ground_truth() -> None:
    prog, _target = _packml_chain_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    plc.patch({"ProdMode": True, "ChgReq": True})
    plc.step()
    assert plc.state.tags["ModeCur"] == 1
    assert plc.state.tags["Target"] is True
    # The handshake registers are back at default at the boundary.
    assert plc.state.tags["ReqBool"] == 0
    assert plc.state.tags["ChgReq"] is False


def test_packml_chain_walk_solves() -> None:
    """Pre-fix this returned a false ``unsolvable`` (the request bit was not
    even steerable: the program's acknowledge reset gives it a writer)."""
    prog, target = _packml_chain_program()
    plc = PLC(prog, dt=0.010)
    plc.step()

    path = plc.how(target)
    assert path.reachable
    patches: dict = {}
    for step in path.steps:
        if step.action:
            patches.update(step.action)
    assert patches.get("ChgReq") is True
    assert patches.get("ProdMode") is True


def test_packml_chain_ablations_refuse() -> None:
    """Both widening passes are load-bearing for the chain; disabling either
    regresses in the refusing direction."""
    prog, target = _packml_chain_program()
    plc = PLC(prog, dt=0.010)
    pdg = build_program_graph(plc._program)
    known = plc._known_tags_by_name

    def run(disabled: frozenset[str]):
        from pyrung.core.analysis.walk.passes import run_walk_passes
        from pyrung.core.analysis.walk.priors import _external_bool_inputs

        w = plc.fork()
        walk._install_walk_harness(w)
        advice, _journal = run_walk_passes(prog, pdg, disabled=disabled)
        ext_inputs = _external_bool_inputs(pdg, known, prog, advice=advice)
        edge_ext = walk._edge_tags(pdg, prog) & set(ext_inputs)
        return walk._walk_to_goal(
            w,
            target.name,
            True,
            pdg,
            w._program,
            known,
            ext_inputs,
            edge_ext,
            64,
            nogoods=walk.NoGoodStore(),
            holds=walk.HoldStore(),
            disabled_passes=disabled,
        )

    assert run(frozenset()) is not None
    assert run(frozenset({"ack_cleared_inputs"})) is None
    assert run(frozenset({"transient_handshake"})) is None


def test_transient_detected_cross_scope_via_call_gate() -> None:
    """ReqBool: producer in main, unconditional clearer inside the sub whose
    call gate (``ReqBool == 1``) fires whenever the tag is set."""
    prog, _target = _packml_chain_program()
    assert _transient_detect(prog, "ReqBool")
    assert _transient_detect(prog, "UnitMode")


def test_ack_cleared_inputs_detected() -> None:
    from pyrung.core.analysis.walk.priors import _ack_cleared_bool_inputs

    prog, _target = _packml_chain_program()
    pdg = build_program_graph(prog)
    plc = PLC(prog, dt=0.010)
    acks = _ack_cleared_bool_inputs(pdg, plc._known_tags_by_name, prog)
    assert "ChgReq" in acks
    # ProdMode is never written by this program — it stays a plain INPUT.
    assert "ProdMode" not in acks
    # Internal state never qualifies.
    assert "Target" not in acks
