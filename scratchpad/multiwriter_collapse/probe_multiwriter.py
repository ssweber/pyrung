"""DESIGN PROBE: multi-writer route collapse (the boundary left open by the
OR-arm fix in commit 57a1b60).

The OR-arm fix collapses an OR *inside one writer's condition* when an arm is
fully steerable.  But ``enumerate_trace_choices`` only consults that collapse
when ``not multi_writer`` — a Bool tag with 2+ viable *writers* always surfaces
a choice, even when one writer is reachable by directly-steerable inputs alone.

This probe characterizes current behavior and validates the predicates a fix
would rely on, across three synthetic programs:

  P1  multi-LATCH, one steerable        -> should collapse onto manual latch
  P2  multi-OUT (duplicate coil)        -> last-wins clobber: the EARLY steerable
                                           writer does NOT work standalone
  P3  multi-writer, all internal        -> control: must STAY ambiguous (Burner)

It does NOT edit source.  It proves the design by: enumerate surfaces N choices;
an auto-resolver picks the cheapest *fully-steerable route*; and
``pilot_how(choice=<that id>)`` reaches where no-choice is ambiguous.
"""

from __future__ import annotations

import math

from pyrung import PLC, And, Bool, Int, Or, Program, copy, latch, out, rung
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.analysis.pdg import build_program_graph, resolve_rung
from pyrung.core.analysis.pilot.trace import (
    compute_steerable,
    compute_reference_constants,
    enumerate_trace_choices,
    trace_back,
    _arm_fully_steerable,
    _trace_score,
    _written_value_for_tag,
    _can_produce,
)
from pyrung.core.analysis.simplified import _sp_to_expr
from pyrung.core.analysis.pilot.compass import detect_opaque_loop
from pyrung.core.analysis.pilot.physical import install_harness


# ---------------------------------------------------------------------------
# Candidate predicates a real fix would add
# ---------------------------------------------------------------------------


def writer_condition_steerable(ri, tag, value, pdg, program, steerable):
    """Is writer *ri*'s rung condition reachable by directly-steerable inputs
    alone?  Reuses the OR-arm fix's recursive predicate on the whole condition."""
    ro = resolve_rung(program, pdg.rung_nodes[ri])
    if ro is None:
        return False
    sp = ro.sp_tree()
    if sp is None:
        return False
    return _arm_fully_steerable(_sp_to_expr(sp), tag, steerable)


def writer_is_retentive(ri, tag, pdg):
    """A writer that does NOT recompute the coil every scan (latch/SET, or
    copy/calc into a held register) — ``tag not in ote_writes``.  Establishing
    via such a writer persists; a non-retentive ``out`` is last-wins and an
    earlier writer is silently clobbered by a later one (the duplicate-coil
    smell P2 exposes), so it is NOT safe to auto-collapse onto."""
    return tag not in pdg.rung_nodes[ri].ote_writes


def _choice_writer(ch):
    """The single writer rung a multi-writer choice locks, or None."""
    return ch.writer_locks[0][2] if ch.writer_locks else None


def auto_resolve(choices, tag, value, snapshot, pdg, program, steerable, opaque_loop):
    """Cheapest collapsible route among surfaced multi-writer choices, or None.

    A choice is collapsible when its writer is (a) retentive — so establishing
    it is not clobbered — and (b) gated by *directly-steerable inputs* (the same
    ``_arm_fully_steerable`` test the OR-arm fix uses), NOT merely transitively
    reachable.  (b) is what preserves the Burner contract: ProdMode/MaintMode are
    reachable via ProdCmd/MaintCmd but are internal coils, so they stay surfaced.

    None => no writer is both retentive and input-gated => keep surfacing."""
    scored = []
    for ch in choices:
        ri = _choice_writer(ch)
        if ri is None:
            continue
        if not writer_is_retentive(ri, tag, pdg):
            continue  # non-retentive out: last-wins clobber, unsafe to pick
        if not writer_condition_steerable(ri, tag, value, pdg, program, steerable):
            continue  # internal-coil gate: a real engineer choice, keep surfaced
        tree = trace_back(
            tag, value, snapshot, pdg, program, steerable,
            opaque_loop=opaque_loop, choice=ch,
        )
        scored.append((_trace_score([tree], pdg), ch))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0])
    return scored[0][1]


# ---------------------------------------------------------------------------
# Synthetic programs
# ---------------------------------------------------------------------------


def prog_multi_latch():
    """Cmd latched by a steerable manual button OR by an internal state."""
    Manual = Bool("Manual", external=True)
    Detect = Bool("Detect", external=True)
    State = Int("State")
    Cmd = Bool("Cmd")
    with Program() as logic:
        with rung(Detect):
            copy(5, State)
        with rung(Manual):
            latch(Cmd)
        with rung(State == 5):
            latch(Cmd)
    return logic, Cmd, True


def prog_multi_out():
    """Cmd driven by two out() rungs (duplicate coil) — last-wins each scan."""
    Manual = Bool("Manual", external=True)
    Auto = Bool("Auto", external=True)
    Detect = Bool("Detect", external=True)
    State = Int("State")
    Cmd = Bool("Cmd")
    with Program() as logic:
        with rung(Detect):
            copy(5, State)
        with rung(Manual):
            out(Cmd)
        with rung(Auto, State == 5):
            out(Cmd)
    return logic, Cmd, True


def prog_multi_internal():
    """Both writers gated by internal coils — neither steerable (Burner shape)."""
    ProdCmd = Bool("ProdCmd", external=True)
    MaintCmd = Bool("MaintCmd", external=True)
    Mode = Int("Mode")
    ProdMode = Bool("ProdMode")
    MaintMode = Bool("MaintMode")
    Cmd = Bool("Cmd")
    with Program() as logic:
        with rung(ProdCmd):
            copy(1, Mode)
        with rung(MaintCmd):
            copy(2, Mode)
        with rung(Mode == 1):
            out(ProdMode)
        with rung(Mode == 2):
            out(MaintMode)
        with rung(ProdMode):
            latch(Cmd)
        with rung(MaintMode):
            latch(Cmd)
    return logic, Cmd, True


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def setup(plc):
    program = plc._program
    fork = plc.fork(history_budget=math.inf)
    pdg = build_program_graph(program)
    hb = install_harness(fork)
    rc = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - hb - rc
    opaque_loop = detect_opaque_loop(pdg, program)
    return program, fork, pdg, steerable, opaque_loop


def _replays_to(factory, path, tag, expected):
    plc = factory()
    for step in path.steps:
        plc.patch(step.action)
        for _ in range(step.scans):
            plc.step()
    return plc.state.tags[tag] == expected


def characterize(name, builder):
    logic, tag_obj, value = builder()
    tag = tag_obj.name
    plc = PLC(logic, dt=0.010)
    plc.step()
    program, fork, pdg, steerable, opaque_loop = setup(plc)
    snap = dict(fork.state.tags)

    print(f"\n{'='*72}\n{name}: how({tag}=={value})\n{'='*72}")

    for ri in sorted(pdg.writers_of.get(tag, frozenset())):
        ro = resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, tag)
        ok = _can_produce(wv, value)
        steer = writer_condition_steerable(ri, tag, value, pdg, program, steerable)
        print(f"  writer rung {pdg.rung_nodes[ri].rung_index}: "
              f"viable={ok} cond_steerable={steer}  wv={wv!r}")

    choices = enumerate_trace_choices(tag, value, snap, pdg, program, steerable=steerable)
    print(f"  enumerate -> {len(choices)} choice(s)")
    for ch in choices:
        print(f"     {ch}")

    # Today, no choice:
    path0 = pilot_how(PLC(logic, dt=0.010), tag_obj, max_scans=500)
    print(f"  pilot_how(no choice): reachable={path0.reachable} ambiguous={path0.ambiguous}")

    # Proposed: auto-resolve to the cheapest fully-steerable route
    pick = auto_resolve(choices, tag, value, snap, pdg, program, steerable, opaque_loop)
    if pick is None:
        print("  auto_resolve -> None  (keep surfacing: no input-only route)")
        return
    print(f"  auto_resolve -> choice id={pick.id} ({pick.label})")
    path1 = pilot_how(PLC(logic, dt=0.010), tag_obj, choice=int(pick.id), max_scans=500)
    replays = (
        _replays_to(lambda: PLC(logic, dt=0.010), path1, tag, value)
        if path1.reachable else False
    )
    print(f"  pilot_how(choice={pick.id}): reachable={path1.reachable} "
          f"replays={replays}  steps={[s.action for s in path1.steps]}")


def main():
    characterize("P1 multi-LATCH (one steerable)", prog_multi_latch)
    characterize("P2 multi-OUT (duplicate coil)", prog_multi_out)
    characterize("P3 multi-internal (Burner shape)", prog_multi_internal)


if __name__ == "__main__":
    main()
