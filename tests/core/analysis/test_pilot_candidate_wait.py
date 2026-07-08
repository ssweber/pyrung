from __future__ import annotations

from types import SimpleNamespace

from pyrung.core.analysis.pilot.candidates import _build_candidates
from pyrung.core.analysis.pilot.charts import CompassGraph
from pyrung.core.analysis.pilot.compass import Compass, CompassObservation
from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionRoute
from pyrung.core.analysis.pilot.pilot import _SKIFF_KEY_BUDGET, _orient_escalate_skiff
from pyrung.core.analysis.pilot.trace import TraceNode


def _route(from_value: int, to_value: int) -> TransitionRoute:
    return TransitionRoute(
        destination_tag="State",
        destination_value=to_value,
        request_tag=None,
        request_value=None,
        source_constraints=(("State", from_value),),
        enablers=(),
        action_tags=frozenset(),
        writer_node=0,
        writer_subroutine=None,
        call_site_gates=(),
        from_values=(from_value,),
    )


def test_prescribed_wait_suppresses_stuck_reason():
    role = PipelineRoles("State")
    graph = CompassGraph(role, (_route(6, 16), _route(16, 17)))
    compass = Compass()
    compass.set_graphs((graph,))

    tree = TraceNode(
        "State",
        17,
        children=[TraceNode("UnreadableGuard", 1, satisfied=False, is_steerable=False)],
    )
    frame = SimpleNamespace(
        key=("state", 6),
        snap={"State": 6, "UnreadableGuard": 0},
        tree=tree,
        raw_trace_actions=(),
        raw_trace_action_details=(),
    )
    state = SimpleNamespace(nogoods={}, forced_holds={})
    ctx = SimpleNamespace(
        compass=compass,
        blocked_route_actions=frozenset(),
        edge_tags=set(),
        clear_only=frozenset(),
        steerable=frozenset(),
        pdg=SimpleNamespace(writers_of={}),
        program=object(),
        debug=False,
        route_allowed=lambda _pair: True,
        opaque_loop=frozenset(),
        target_tag="State",
        target_value=17,
    )

    candidates = _build_candidates(frame, state, ctx, lambda _msg: None)

    assert candidates.wait_prescribed is True
    assert candidates.wait_reason == "let-run State: 6->16"
    assert candidates.stuck_reason is None


def _drain(generator):
    events = []
    try:
        while True:
            events.append(next(generator))
    except StopIteration as stop:
        return events, stop.value


def test_apply_reports_changed_and_returns_self_when_nothing_new():
    """Compass.apply's no-new-knowledge contract: (compass, changed).

    Novel observations return ``changed=True`` and a new compass value; applying
    the *same* observations again returns ``changed=False`` and ``self`` — the
    identity guarantee the skiff relies on, established from the table ops rather
    than a whole-table equality scan.
    """
    obs = CompassObservation("edge", "State", ("Cmd", True), 6, 8)
    base = Compass()

    learned, changed = base.apply((obs,))
    assert changed is True
    assert learned is not base

    again, changed_again = learned.apply((obs,))
    assert changed_again is False
    assert again is learned

    # A probe mark is knowledge too: a fresh no-change tombstone counts as changed.
    probe = CompassObservation("no_change", "State", ("Other", True), 6, None)
    with_probe, probe_changed = learned.apply((probe,))
    assert probe_changed is True
    assert with_probe is not learned
    # …but re-applying it does not.
    _, probe_again = with_probe.apply((probe,))
    assert probe_again is False


def test_duplicate_skiff_observations_do_not_reorient(monkeypatch):
    obs = CompassObservation("edge", "State", ("Cmd", True), 6, 8)
    compass, _ = Compass().apply((obs,))
    ctx = SimpleNamespace(compass=compass)
    state = SimpleNamespace(
        work=SimpleNamespace(state=SimpleNamespace(scan_id=815)),
        stuck_keys={},
    )
    frame = SimpleNamespace(key=("state", 6))

    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.pilot.probe_live_guard_frontiers",
        lambda _frame, _state, _ctx: (obs,),
    )

    events, should_continue = _drain(_orient_escalate_skiff("all_rejected", frame, state, ctx))

    # The obs is already known: no knowledge change, so no re-orient lap, no event,
    # and the compass value is untouched.
    assert events == []
    assert should_continue is False
    assert ctx.compass is compass
    assert state.stuck_keys == {}


def test_exhausted_key_stops_after_skiff_budget(monkeypatch):
    """A stuck key that keeps learning fresh-but-useless probe marks stops.

    Each round returns a *new* probe mark (knowledge changes every time), so the
    part-2 changed signal alone would never terminate.  The per-key skiff budget
    caps the laps and then the loop stops honestly (returns ``False`` → the caller
    falls to the terminal stuck dump), instead of alternating forever.
    """
    counter = {"n": 0}

    def fresh_probe(_frame, _state, _ctx):
        counter["n"] += 1
        # A brand-new (tag, from, cause) each call — always changes knowledge.
        return (CompassObservation("no_change", "State", (f"Cmd{counter['n']}", True), 6, None),)

    monkeypatch.setattr("pyrung.core.analysis.pilot.pilot.probe_live_guard_frontiers", fresh_probe)

    ctx = SimpleNamespace(compass=Compass())
    state = SimpleNamespace(
        work=SimpleNamespace(state=SimpleNamespace(scan_id=1)),
        stuck_keys={},
    )
    frame = SimpleNamespace(key=("state", 6))

    laps = 0
    while True:
        _events, cont = _drain(_orient_escalate_skiff("all_rejected", frame, state, ctx))
        if not cont:
            break
        laps += 1
        assert laps <= 10, "skiff escalation must terminate at a stuck key"

    assert laps == _SKIFF_KEY_BUDGET
    assert state.stuck_keys[frame.key] == _SKIFF_KEY_BUDGET
