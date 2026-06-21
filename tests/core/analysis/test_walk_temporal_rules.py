"""Temporal rule learning for the corridor walker."""

from __future__ import annotations

from pyrung import Bool, Or, Program, Rung, Timer, latch, on_delay, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.analysis.walk.base import _NO_MONITORS
from pyrung.core.analysis.walk.rules import recursive_cause_evidence, temporal_cycle_recovery
from pyrung.core.runner import PLC


def _ctx_for(prog: Program, plc: PLC):
    pdg = build_program_graph(prog)
    known = plc._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, prog) & set(ext_inputs)
    return walk._WalkContext(
        pdg=pdg,
        program=prog,
        known=known,
        ext_inputs=ext_inputs,
        edge_ext=edge_ext,
        fold_ctx=walk._build_fold_context(plc, pdg, prog),
        nogoods=walk.NoGoodStore(),
        holds=walk.HoldStore(),
    )


def test_rule_store_promotes_conflicting_level_rules() -> None:
    store = walk.RuleStore()
    off = walk.AvoidEvent("OffWD_Done", "Sensor", False, max_scans=9)
    on = walk.AvoidEvent("OnWD_Done", "Sensor", True, max_scans=4)

    store.add_level(walk.LevelRule("Sensor", False, "cannot_hold", avoid_event=off))
    store.add_level(walk.LevelRule("Sensor", True, "cannot_hold", avoid_event=on))

    temporal = store.active_temporal()
    assert len(temporal) == 1
    rule = temporal[0].payload
    assert isinstance(rule, walk.TemporalRule)
    assert rule.kind == "cycle"
    assert rule.tag == "Sensor"
    assert {c.event_tag for c in rule.constraints} == {"OffWD_Done", "OnWD_Done"}
    assert all(r.status == "superseded" for r in store.entries() if r.id in rule.replaces)


def test_recursive_evidence_records_nonretentive_on_delay_boundary() -> None:
    sensor = Bool("Sensor", external=True)
    internal = Bool("Internal")
    tmr = Timer.clone("SensorOnWD")

    with Program() as prog:
        with Rung(sensor):
            out(internal)
        with Rung(internal):
            on_delay(tmr, 50, "ms")

    plc = PLC(prog, dt=0.010)
    ctx = _ctx_for(prog, plc)
    plc.patch({"Sensor": True})
    for _ in range(5):
        plc.step()

    chain = plc.cause("SensorOnWD_Done")
    assert chain is not None
    evidence = recursive_cause_evidence(
        ctx,
        plc,
        chain,
        target_tag="SensorOnWD_Done",
        monitors=_NO_MONITORS,
    )

    assert [(r.tag_name, r.to_value) for r in evidence.roots] == [("Sensor", True)]
    levels = [r.payload for r in ctx.rules.entries() if isinstance(r.payload, walk.LevelRule)]
    assert len(levels) == 1
    assert levels[0].tag == "Sensor"
    assert levels[0].value is True
    assert levels[0].kind == "cannot_hold"
    assert levels[0].avoid_event is not None
    assert levels[0].avoid_event.event_tag == "SensorOnWD_Done"
    assert levels[0].avoid_event.max_scans == 4


def test_temporal_cycle_recovery_uses_promoted_cycle_rule() -> None:
    sensor = Bool("Sensor", external=True)
    seen_low = Bool("SeenLow")
    seen_high = Bool("SeenHigh")
    target = Bool("Target")
    off_wd = Timer.clone("OffWD")
    on_wd = Timer.clone("OnWD")
    alarm = Bool("Alarm")

    with Program() as prog:
        with Rung(~sensor):
            latch(seen_low)
        with Rung(sensor):
            latch(seen_high)
        with Rung(seen_low, seen_high):
            out(target)
        with Rung(~sensor):
            on_delay(off_wd, 30, "ms")
        with Rung(sensor):
            on_delay(on_wd, 30, "ms")
        with Rung(Or(off_wd.Done, on_wd.Done)):
            out(alarm)

    plc = PLC(prog, dt=0.010)
    plc.step()
    assert plc.state.tags["SeenLow"] is True
    assert plc.state.tags["Target"] is not True

    ctx = _ctx_for(prog, plc)
    ctx.rules.add_level(
        walk.LevelRule(
            "Sensor",
            False,
            "cannot_hold",
            avoid_event=walk.AvoidEvent("OffWD_Done", "Sensor", False, max_scans=2),
        )
    )
    ctx.rules.add_level(
        walk.LevelRule(
            "Sensor",
            True,
            "cannot_hold",
            avoid_event=walk.AvoidEvent("OnWD_Done", "Sensor", True, max_scans=2),
        )
    )

    steps = temporal_cycle_recovery(ctx, plc, "Target", True, 8, _NO_MONITORS)

    assert steps is not None
    replay = plc.fork()
    for action, scans in steps:
        if action:
            replay.patch(action)
        for _ in range(scans):
            replay.step()
            assert replay.state.tags["Alarm"] is not True
    assert replay.state.tags["Target"] is True
