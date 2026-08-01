"""The empirical steerable veto — cause-driven demotion of masquerading levers.

Static steerability is a *hypothesis*; the recorded run is *testimony*.  A tag the
static classifier calls steerable but that the recorded run shows the PROGRAM
wrote (at a scan the pilot neither held nor pulsed it) is NOT a free lever in the
live context.  This is "Verify is the sole source of CONFIRMED" applied to
classification.  Fail-safe: positive evidence only — never promotes, and no
recorded change leaves the static verdict untouched.

Covers the primitive and each wired consumer: ``chase_cause_roots`` (recurse
through, never nogood-stop) and skiff probe selection.
"""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import Bool, Int, Program, Rung, copy, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.causal import (
    _shared_cause,
    chase_cause_roots,
    empirical_program_writes,
)
from pyrung.core.analysis.pilot.skiff import _frontier_probes
from pyrung.core.analysis.pilot.trace import compute_resting_values
from pyrung.core.analysis.steerable import compute_steerable
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Primitive: empirical_program_writes
# ---------------------------------------------------------------------------


def _mover_program() -> Program:
    """``Status`` (statically an input-looking word) is program-written to 1 when
    the steerable input ``Trigger`` rises."""
    trigger = Bool("Trigger", external=True)
    status = Int("Status")
    with Program(strict=False) as logic:
        with Rung(trigger):
            copy(1, status)
    return logic


def test_empirical_program_writes_fires_on_recorded_program_write() -> None:
    logic = _mover_program()
    plc = PLC(logic)
    plc.step()  # scan 0: Status stays 0
    plc.patch({"Trigger": True})
    plc.step()  # scan 1: program writes Status -> 1
    plc.step()

    end = plc.state.scan_id
    # The pilot never touched Status → its recorded 0->1 is the program's.
    fired = empirical_program_writes(plc, frozenset({"Status"}), start_scan=0, end_scan=end)
    assert "Status" in fired


def test_veto_is_fail_safe_without_evidence() -> None:
    logic = _mover_program()
    plc = PLC(logic)
    plc.step()
    plc.step()  # Trigger never pressed → Status never moves

    end = plc.state.scan_id
    assert (
        empirical_program_writes(plc, frozenset({"Status"}), start_scan=0, end_scan=end)
        == frozenset()
    )


def test_veto_excludes_exact_recorded_pilot_write() -> None:
    status = Int("PilotWrittenStatus")
    seen = Bool("PilotWrittenSeen")
    with Program(strict=False) as logic:
        with Rung(status == 1):
            out(seen)
    plc = PLC(logic)
    from pyrung.core.synthesis import Synthesis, copy_hold_rung

    plc._synthesis = Synthesis(
        holds=[copy_hold_rung(value=1, dest=status)],
    )
    plc.step()
    end = plc.state.scan_id

    assert (
        empirical_program_writes(
            plc,
            frozenset({status.name}),
            start_scan=0,
            end_scan=end,
        )
        == frozenset()
    )


def test_veto_accepts_later_plant_restoration_after_hold_expiry() -> None:
    guard = Bool("EmpiricalReleaseGuard", external=True)
    status = Int("EmpiricalReleasedStatus")
    seen = Bool("EmpiricalReleasedSeen")
    with Program(strict=False) as logic:
        with Rung(status == 1):
            out(seen)
    plc = PLC(logic)
    from pyrung.core.synthesis import Synthesis, copy_hold_rung

    plc._synthesis = Synthesis(
        plant=[copy_hold_rung(value=0, dest=status)],
        holds=[copy_hold_rung(value=1, dest=status, guard=guard)],
    )
    plc.patch({guard.name: True})
    plc.step()
    plc.step()
    plc.patch({guard.name: False})
    plc.step()
    plc.step()

    assert empirical_program_writes(
        plc,
        frozenset({status.name}),
        start_scan=0,
        end_scan=plc.state.scan_id,
    ) == frozenset({status.name})


# ---------------------------------------------------------------------------
# Consumer: chase_cause_roots recurses through an empirically-written root
# ---------------------------------------------------------------------------


def _chain_program() -> Program:
    """A steerable input ``Lever`` drives ``Mid`` (a coil that LOOKS steerable to
    the walk only if we pretend), and ``Mid`` drives ``Out``.  cause(Out) roots
    at ``Lever`` through ``Mid``."""
    lever = Bool("Lever", external=True)
    mid = Bool("Mid")
    out_ = Bool("Out")
    with Program(strict=False) as logic:
        with Rung(lever):
            out(mid)
        with Rung(mid):
            out(out_)
    return logic


def test_chase_recurses_through_empirical_write() -> None:
    logic = _chain_program()
    plc = PLC(logic)
    plc.step()
    plc.patch({"Lever": True})
    plc.step()
    plc.step()

    steerable = frozenset({"Lever", "Mid"})  # pretend Mid also looks steerable

    # Baseline: Mid is a terminal nogood — the walk stops there.
    base_ng, _ = chase_cause_roots(plc, "Out", steerable)
    assert "Mid" in base_ng

    # With the veto marking Mid empirically program-written, the walk must NOT
    # stop at Mid — it recurses through to the real steerable root, Lever.
    veto_ng, _ = chase_cause_roots(plc, "Out", steerable, empirical_writes=frozenset({"Mid"}))
    assert "Mid" not in veto_ng
    assert "Lever" in veto_ng


def test_chase_veto_none_is_byte_identical() -> None:
    logic = _chain_program()
    plc = PLC(logic)
    plc.step()
    plc.patch({"Lever": True})
    plc.step()
    plc.step()
    steerable = frozenset({"Lever", "Mid"})
    a = chase_cause_roots(plc, "Out", steerable)
    b = chase_cause_roots(plc, "Out", steerable, empirical_writes=frozenset())
    assert a == b


def test_chase_steps_behind_steerable_effect_with_recorded_writer() -> None:
    """The departure being explained is not its own corrective lever."""
    logic = _chain_program()
    plc = PLC(logic)
    plc.step()
    plc.patch({"Lever": True})
    plc.step()

    nogoods, holds = chase_cause_roots(
        plc,
        "Out",
        frozenset({"Out", "Lever"}),
    )

    assert nogoods == {"Lever"}
    assert holds == [("Lever", False)]


def test_explicit_scan_cause_is_shared_across_investigation_passes(
    monkeypatch,
) -> None:
    logic = _chain_program()
    plc = PLC(logic)
    plc.step()
    plc.patch({"Lever": True})
    plc.step()
    scan = plc.state.scan_id
    original = plc.cause
    calls = 0

    def counted_cause(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(plc, "cause", counted_cause)

    first = _shared_cause(plc, "Out", scan)
    second = _shared_cause(plc, "Out", scan)

    assert first is not None
    assert second is first
    assert calls == 1

    from pyrung.core.analysis.pilot.overlay import _set_synth_holds

    _set_synth_holds(plc, [])
    assert _shared_cause(plc, "Out", scan) is not None
    assert calls == 2


def test_explicit_scan_cause_is_shared_by_sibling_counterfactual_worlds(
    monkeypatch,
) -> None:
    """Immutable prefix evidence belongs to its epoch, not either child."""
    logic = _chain_program()
    source = PLC(logic)
    source.step()
    source.patch({"Lever": True})
    source.step()
    scan = source.state.scan_id

    left = source.fork(scan_id=scan, inherit_log=True)
    right = source.fork(scan_id=scan, inherit_log=True)
    owner = left._causal_owner_at(scan)
    assert owner is not None
    assert right._causal_owner_at(scan) is owner

    original = owner.cause
    calls = 0

    def counted_cause(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, "cause", counted_cause)

    first = _shared_cause(left, "Out", scan)
    second = _shared_cause(right, "Out", scan)

    assert first is not None
    assert second is first
    assert calls == 1


# ---------------------------------------------------------------------------
# Consumer: skiff probe selection
# ---------------------------------------------------------------------------


def _free_word_ctx() -> tuple[SimpleNamespace, str]:
    """A frontier ``Step`` gated by a free word ``PV`` (steerable Int, no declared
    domain).  Returns a stub ctx and the frontier tag."""
    pv = Int("PV")
    step = Int("Step")
    with Program(strict=False) as logic:
        with Rung(pv < 5):
            copy(1, step)
    graph = build_program_graph(logic)
    plc = PLC(logic)
    plc.step()
    known = plc._known_tags_by_name
    steerable = compute_steerable(graph, known, logic)
    resting = compute_resting_values(steerable, known, graph, logic)
    ctx = SimpleNamespace(pdg=graph, steerable=steerable, resting=resting, nd_domains={})
    return ctx, "Step"


def test_skiff_probe_selection_skips_empirically_written_word() -> None:
    ctx, frontier = _free_word_ctx()
    snap = {"PV": 0, "Step": 0}
    # Give PV a declared-ish probe path via nd_domains so it would be a probe.
    ctx.nd_domains = {"PV": (0, 1, 2)}
    with_evidence = _frontier_probes(frontier, snap, {}, ctx, frozenset({"PV"}))
    assert not any(t == "PV" for t, _ in with_evidence)
