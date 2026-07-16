"""The empirical steerable veto — cause-driven demotion of masquerading levers.

Static steerability is a *hypothesis*; the recorded run is *testimony*.  A tag the
static classifier calls steerable but that the recorded run shows the PROGRAM
wrote (at a scan the pilot neither held nor pulsed it) is NOT a free lever in the
live context.  This is "Verify is the sole source of CONFIRMED" applied to
classification.  Fail-safe: positive evidence only — never promotes, and no
recorded change leaves the static verdict untouched.

Covers the primitive and each wired consumer: ``chase_cause_roots`` (recurse
through, never nogood-stop), the skiff free-word decline / probe selection.
"""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import Bool, Int, Program, Rung, copy, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.causal import (
    chase_cause_roots,
    empirical_program_writes,
    pilot_touched_tags,
)
from pyrung.core.analysis.pilot.skiff import _frontier_free_words, _frontier_probes
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
    fired = empirical_program_writes(
        plc, frozenset({"Status"}), start_scan=0, end_scan=end, pilot_touched=frozenset()
    )
    assert "Status" in fired


def test_veto_is_fail_safe_without_evidence() -> None:
    logic = _mover_program()
    plc = PLC(logic)
    plc.step()
    plc.step()  # Trigger never pressed → Status never moves

    end = plc.state.scan_id
    assert (
        empirical_program_writes(
            plc, frozenset({"Status"}), start_scan=0, end_scan=end, pilot_touched=frozenset()
        )
        == frozenset()
    )


def test_veto_excludes_pilot_touched_tags() -> None:
    logic = _mover_program()
    plc = PLC(logic)
    plc.step()
    plc.patch({"Trigger": True})
    plc.step()
    plc.step()
    end = plc.state.scan_id
    # If the pilot itself could have moved Status, positive evidence is withheld
    # (fail-safe): a tag in pilot_touched is never demoted.
    assert (
        empirical_program_writes(
            plc,
            frozenset({"Status"}),
            start_scan=0,
            end_scan=end,
            pilot_touched=frozenset({"Status"}),
        )
        == frozenset()
    )


def test_pilot_touched_tags_unions_holds_and_journey() -> None:
    hold_log = [SimpleNamespace(tags=(("HeldA", 1), ("HeldB", 0)))]
    journey = [SimpleNamespace(inputs={"Pulsed": True})]
    touched = pilot_touched_tags(hold_log, journey, {"ForcedC": 1})
    assert touched == frozenset({"HeldA", "HeldB", "Pulsed", "ForcedC"})


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


# ---------------------------------------------------------------------------
# Consumer: skiff free-word decline / probe selection
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


def test_skiff_free_word_decline_without_evidence_names_the_word() -> None:
    ctx, frontier = _free_word_ctx()
    assert "PV" in ctx.steerable
    # No evidence → the free word still headlines (prior behavior).
    assert "PV" in _frontier_free_words(frontier, ctx)


def test_skiff_free_word_decline_drops_empirically_written_word() -> None:
    ctx, frontier = _free_word_ctx()
    # Recorded run shows PV program-written → not a free lever, dropped from decline.
    assert "PV" not in _frontier_free_words(frontier, ctx, frozenset({"PV"}))


def test_skiff_probe_selection_skips_empirically_written_word() -> None:
    ctx, frontier = _free_word_ctx()
    snap = {"PV": 0, "Step": 0}
    # Give PV a declared-ish probe path via nd_domains so it would be a probe.
    ctx.nd_domains = {"PV": (0, 1, 2)}
    with_evidence = _frontier_probes(frontier, snap, {}, ctx, frozenset({"PV"}))
    assert not any(t == "PV" for t, _ in with_evidence)
