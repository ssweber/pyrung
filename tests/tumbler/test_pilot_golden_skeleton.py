"""Golden decision-skeleton locks for PILOT drives on the tumbler fixture.

Locks the planner's DECISION RECORD, not just its outcome: a change that
still reaches the goal but corrupts the reasoning (wrong rejection ground,
lost route beat, a coast overshoot accepted as 'ambient') fails here.

The skeleton (see ``tests/tumbler/skeleton.py``) keeps decision fields only —
event kinds, tags, accept/reject grounds, slugs, route beats, provisional
lifecycle, zoom requested-vs-landed — and drops everything run-variable
(scan ids, dwell counts, fork/perf numbers).

Regenerating a golden::

    PYRUNG_REGEN_GOLDEN=1 uv run pytest tests/tumbler/test_pilot_golden_skeleton.py

The regen run rewrites the file and FAILS loudly, so a regeneration can
never silently pass in CI.  Review the diff before committing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot._ops import PilotRung
from pyrung.core.condition import AllCondition, AnyCondition, CompareEq
from pyrung.core.runner import _compile_avoid
from tests.fixtures.tumbler import enter_production
from tests.tumbler.bench import Bench
from tests.tumbler.skeleton import divergence_message, dump_skeleton, extract_skeleton

pytestmark = pytest.mark.tumbler

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
REGEN_ENV = "PYRUNG_REGEN_GOLDEN"

EXECUTE_MAX_SCANS = 20_000
COMPLETED_MAX_SCANS = 400_000
BURNER_MAX_SCANS = 20_000
BURNER_WALL_BUDGET_S = 240.0
# The internal-route gate's scan budget must clear a HEALTHY drive (the
# hand-driven Bench route completes around scan ~2,817, and ``max_scans``
# charges committed scans minus accepted-coast dwell credit) — 40k is
# generous, so the cap is never what makes today's xfail fast.  The fast-fail
# comes from an explicit wall-clock deadline in the drive loop instead:
# today's floundering mode grinds ~900-scan Unhold laps at ~90s wall each
# (measured 2026-07-14), so a scan cap alone would burn ~50 minutes.  A
# healthy drive finishes far inside the deadline (the unavoided COMPLETED
# drive takes ~9s; raise the deadline if slow hardware ever needs it).
INTERNAL_ROUTE_MAX_SCANS = 40_000
INTERNAL_ROUTE_WALL_BUDGET_S = 240.0


# ---------------------------------------------------------------------------
# Drive + compare plumbing
# ---------------------------------------------------------------------------


def _drive_state_target(logic, value: int, max_scans: int):
    """Cold-boot pilot drive toward ``Sts_StateCurrent == value``.

    Cold boot only (one settling scan — the pilot loop itself requires a
    non-zero scan id).  No Production forcing, no pre-forced permissives:
    mode changes and door corrections are exactly what the pilot is expected
    to DISCOVER, and that discovery is part of the locked record.
    """
    plc = PLC(logic)
    plc.step()
    target = plc._known_tags_by_name["Sts_StateCurrent"]
    events = []
    for event in pilot_events(plc, target == value, max_scans=max_scans):
        events.append(event)
        if event.kind == "finished":
            break
    return events


def _drive_bool_target(logic, tag_name: str, max_scans: int, wall_budget_s: float):
    """Cold-boot Pilot drive toward one Boolean output."""
    plc = PLC(logic, dt=0.010)
    plc.step()
    target = plc._known_tags_by_name[tag_name]
    deadline = time.monotonic() + wall_budget_s
    events = []
    for event in pilot_events(plc, target, max_scans=max_scans):
        events.append(event)
        if event.kind == "finished":
            break
        if time.monotonic() > deadline:
            pytest.fail(
                f"how({tag_name}) exceeded the {wall_budget_s:.0f}s wall budget "
                f"at scan {event.scan} (kind={event.kind})"
            )
    return events


def _assert_matches_golden(skeleton: list[dict], golden_path: Path) -> None:
    rendered = dump_skeleton(skeleton)
    if os.environ.get(REGEN_ENV) == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(rendered, encoding="utf-8")
        pytest.fail(
            f"{golden_path.name} regenerated because {REGEN_ENV}=1 — "
            f"review the diff, commit it, and rerun without the flag"
        )
    assert golden_path.exists(), (
        f"golden file missing: {golden_path} — generate it with {REGEN_ENV}=1"
    )
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    if skeleton != golden:
        pytest.fail(divergence_message(golden, skeleton, "golden", "actual"))


def _assert_zoom_tripwire(skeleton: list[dict]) -> None:
    """The state-6 tripwire, independent of the golden (survives regeneration).

    Historical bug: a coast requested 3->6 landed at 8 and was accepted as
    'ambient'.  Every ``zoom_accepted`` must either land exactly on the
    requested bearing value, carry a receipt proving its relational boundary
    was reached, or be explicitly classified as a departure (``ejected`` — an
    AMBIENT_DRIFT committed under the ejection guard).
    """
    for index, entry in enumerate(skeleton):
        if entry["kind"] != "zoom_accepted":
            continue
        requested = entry.get("zoom_target_value")
        if requested is None:
            continue  # target-terminal let-run: no channel bearing requested
        landed = entry.get("zoom_actual_value")
        assert (
            landed == requested
            or entry.get("bearing_stop_reason") == "reached"
            or entry.get("ejected") is True
        ), (
            f"zoom_accepted[{index}] landed at {landed!r} but requested "
            f"{requested!r} without being classified as a departure "
            f"(ejected={entry.get('ejected')!r}): {entry}"
        )


def _finished(skeleton: list[dict]) -> dict:
    assert skeleton, "empty skeleton"
    entry = skeleton[-1]
    assert entry["kind"] == "finished", f"stream did not end in finished: {entry['kind']}"
    return entry


def _accepted_tags(skeleton: list[dict]) -> list[str]:
    return [
        entry["candidate_detail"]["tag"]
        for entry in skeleton
        if entry["kind"] == "candidate_accepted"
    ]


def _assert_no_factory_reset(skeleton: list[dict]) -> None:
    """Deep drives may not erase earned work to manufacture a fresh route."""

    pressed = _all_action_tags(skeleton)
    assert "Cmd_Reset2FactoryDefault" not in pressed, (
        f"pilot destroyed the current program while work was still available: {sorted(pressed)}"
    )


# ---------------------------------------------------------------------------
# Workhorse: how(Sts_StateCurrent == 6) — the Execute drive
# ---------------------------------------------------------------------------


def test_pilot_golden_skeleton_execute(tumbler_logic) -> None:
    events = _drive_state_target(tumbler_logic, 6, EXECUTE_MAX_SCANS)
    skeleton = extract_skeleton(events)

    # Outcome assertions independent of the golden (survive regeneration).
    finished = _finished(skeleton)
    assert finished["reached"] is True, f"Execute drive did not reach: {finished}"
    _assert_zoom_tripwire(skeleton)

    # Machine-correct beat order: sm_CopyOrJumpState R3 hard-enables states
    # 2/4/6/9 regardless of mode, so the Execute drive needs NO mode-change
    # press — the bare PackML sequence is the honest record.
    assert _accepted_tags(skeleton) == [
        "Cmd_State_Clear",
        "Cmd_State_Reset",
        "Cmd_State_Start",
    ]

    _assert_matches_golden(skeleton, GOLDEN_DIR / "how_execute_skeleton.json")


# ---------------------------------------------------------------------------
# Deep gate: how(Sts_StateCurrent == 17) — the COMPLETED drive
# ---------------------------------------------------------------------------


def test_pilot_golden_skeleton_completed(tumbler_logic) -> None:
    events = _drive_state_target(tumbler_logic, 17, COMPLETED_MAX_SCANS)
    skeleton = extract_skeleton(events)

    finished = _finished(skeleton)
    _assert_zoom_tripwire(skeleton)
    _assert_no_factory_reset(skeleton)

    # This drive DOES require Production mode (the recipe/completion path
    # runs through the production SFCs) — the mode-change discovery is part
    # of the locked record.
    accepted = _accepted_tags(skeleton)
    assert "Cmd_Mode_Production" in accepted or any(
        "Cmd_Mode_Production" in json.dumps(entry.get("co_actions", ()))
        for entry in skeleton
        if entry["kind"] == "candidate_accepted"
    ), f"COMPLETED drive never discovered the Production mode change: accepted={accepted}"

    if not finished["reached"]:
        # An honest stall is still a lockable skeleton — but the stall must
        # be pointable, not silent: the terminal report names its frontier.
        reason = finished.get("reason") or ""
        assert "still waiting on" in reason or "frontier" in reason, (
            f"stall is not pointable — terminal reason names no frontier: {reason!r}"
        )

    _assert_matches_golden(skeleton, GOLDEN_DIR / "how_completed_skeleton.json")


# ---------------------------------------------------------------------------
# Causal-frontier gate: how(y_BurnerLoop)
# ---------------------------------------------------------------------------


def _confirmed_holds(skeleton: list[dict], kind: str) -> list[list]:
    return [
        hypothesis.get("holds", [])
        for entry in skeleton
        if entry["kind"] == "trend_regression"
        for hypothesis in (entry.get("investigation") or {}).get("confirmed_detail", ())
        if hypothesis.get("kind") == kind
    ]


def _hold_dest(hold) -> str | None:
    if isinstance(hold, list) and hold:
        return hold[0]
    if isinstance(hold, dict):
        return hold.get("dest")
    return None


def _guard_equalities(condition) -> set[tuple[str, object]]:
    """Literal equality facts nested anywhere in an And/Or guard."""

    if isinstance(condition, CompareEq):
        name = getattr(condition.tag, "name", None)
        return {(name, condition.value)} if name is not None else set()
    if isinstance(condition, (AllCondition, AnyCondition)):
        return {fact for child in condition.conditions for fact in _guard_equalities(child)}
    return set()


def test_pilot_golden_skeleton_y_burnerloop(tumbler_logic) -> None:
    """The exact deep-chain route to the burner output."""
    events = _drive_bool_target(
        tumbler_logic,
        "y_BurnerLoop",
        BURNER_MAX_SCANS,
        BURNER_WALL_BUDGET_S,
    )
    skeleton = extract_skeleton(events)

    finished = _finished(skeleton)
    assert finished["reached"] is True, f"BurnerLoop drive did not reach: {finished}"
    _assert_zoom_tripwire(skeleton)
    _assert_no_factory_reset(skeleton)
    assert any(
        tag == "x_RotateFB"
        for entry in skeleton
        if entry["kind"] == "candidates_built"
        for tag, _value in entry.get("completion_frontier") or ()
    ), "the Starting->Execute completion frontier never named x_RotateFB"

    # The Execute-era departure has one exact coordinated corrective frontier:
    # the physical door contacts. Defaults and first-scan plumbing must never
    # reappear as a broad batch.
    precise_dest_sets = [
        {_hold_dest(hold) for hold in holds}
        for holds in _confirmed_holds(skeleton, "precise-cause")
    ]
    assert {"x_DoorClosed", "x_LintDoorClosed"} in precise_dest_sets
    assert not any(
        dest in {"Test_Simulate_1st_Scan", "Cmd_ForceClear", "Cmd_CmdChgRequest"}
        for destinations in precise_dest_sets
        for dest in destinations
    )

    # The first door correction owns the actual Starting/Step-101 occurrence
    # from the recorded deep chain. The exact corridor may be a nested state
    # mapping condition; the raw-object tripwire requires both coordinates so
    # regeneration cannot bless a global door hold.
    starting_door_rungs = [
        hold
        for event in events
        if event.kind == "trend_regression"
        for hypothesis in (event.data.get("investigation") or {}).get("confirmed_detail", ())
        if hypothesis.get("kind") == "latch-exposure"
        for hold in hypothesis.get("holds", ())
        if isinstance(hold, PilotRung)
        and hold.dest in {"x_DoorClosed", "x_LintDoorClosed"}
        and ("Sts_StateCurrent", 3) in _guard_equalities(hold.guard)
        and ("Internal__Step", 101) in _guard_equalities(hold.guard)
    ]
    assert {rung.dest for rung in starting_door_rungs} == {
        "x_DoorClosed",
        "x_LintDoorClosed",
    }

    # The later complement-reset watchdog is a separate causal era.
    liveness_dest_sets = [
        {_hold_dest(hold) for hold in holds} for holds in _confirmed_holds(skeleton, "liveness")
    ]
    assert {"x_RotateSensor"} in liveness_dest_sets

    _assert_matches_golden(skeleton, GOLDEN_DIR / "how_y_burnerloop_skeleton.json")


# ---------------------------------------------------------------------------
# Internal-route gate: how(Sts_State_Completed) avoiding the Complete button
# ---------------------------------------------------------------------------


def test_held_dry_route_chooses_unhold_not_start(tumbler_logic) -> None:
    """A possible future Complete producer cannot invent an inapplicable Start."""
    bench = Bench(tumbler_logic)
    bench.force_physical()
    bench.step()
    enter_production(bench.plc)
    bench.scan = bench.plc.state.scan_id
    for command in ("Cmd_State_Clear", "Cmd_State_Reset", "Cmd_State_Start"):
        bench.pulse(command)
    assert bench.step_until(
        lambda: bench.get("Sts_StateCurrent") == 6,
        4_000,
    )
    assert bench.step_until(lambda: bench.get("Internal__Step") == 101, 4_000)

    bench.pulse("Cmd_State_Hold")
    assert bench.step_until(
        lambda: bench.get("Sts_StateCurrent") == 11,
        800,
    )
    assert bench.get("Internal__Step") == 101

    tags = bench.plc._known_tags_by_name
    avoid_pred = _compile_avoid(tags["Cmd_State_Complete"])
    last_snapshot = None
    for event in pilot_events(
        bench.plc,
        tags["Sts_StateCurrent"] == 17,
        max_scans=bench.plc.state.scan_id + 100,
        avoid_pred=avoid_pred,
    ):
        if event.kind == "iteration":
            last_snapshot = event.data["snapshot"]
            continue
        if event.kind != "candidates_built":
            continue
        assert last_snapshot["Sts_StateCurrent"] == 11
        assert last_snapshot["Internal__Step"] == 101
        pairs = tuple(candidate["pair"] for candidate in event.data["candidates"])
        assert pairs[0] == ("Cmd_State_Unhold", True)
        assert ("Cmd_State_Start", True) not in pairs
        route = event.data["route_plan"]
        assert route["path"][0]["from"] == 11
        assert route["path"][0]["to"] == 12
        return
    pytest.fail("PILOT did not produce a HELD/Step101 candidate reading")


def test_pilot_internal_route_progress_skeleton(tumbler_logic) -> None:
    """Lock the cold avoided-Complete drive to the real completion signal.

    The former endpoint was an Unhold read at HELD/Step101. That landing was
    premature safety motion, not recipe progress; the exact-producer bearing
    now preserves Execute long enough for investigation to identify and install
        the two door guards. Later watchdog regressions must accept the recorded
        rotate-sensor reset operation and then the sail-relay absence root as
        distinct owners of their Execute->Abort incidents. The owner-verified dry dwell must then advance the
    recipe through the dry, cool, hold-for-fluff, and fluff operations. Pipeline
    motion observed while reading a later producer must keep its current owner;
    it must not introduce a duplicate Hold command. Each later door incident on
    the reused state-transition executor is corrected in its own causal era,
    after which the program-owned Complete transition reaches State 17 and
    asserts ``Sts_State_Completed``. The Boolean target is deliberate: reaching
    state 17 one scan before its status output is not completion.
    """
    plc = PLC(tumbler_logic)
    plc.step()
    tags = plc._known_tags_by_name
    avoid_pred = _compile_avoid(tags["Cmd_State_Complete"])
    events = []
    door_correction = None
    liveness_correction = None
    sail_correction = None
    post_sail_door_corrections: set[str] = set()
    finished = None
    deadline = time.monotonic() + INTERNAL_ROUTE_WALL_BUDGET_S
    for event in pilot_events(
        plc,
        tags["Sts_State_Completed"],
        max_scans=INTERNAL_ROUTE_MAX_SCANS,
        avoid_pred=avoid_pred,
    ):
        events.append(event)
        if event.kind == "trend_regression":
            confirmed = (event.data.get("investigation") or {}).get(
                "confirmed_detail",
                (),
            )
            precise = {
                rung.dest
                for hypothesis in confirmed
                if hypothesis.get("kind") == "precise-cause"
                for rung in hypothesis.get("holds", ())
                if hasattr(rung, "dest")
            }
            if {"x_DoorClosed", "x_LintDoorClosed"} <= precise:
                door_correction = event
            liveness = {
                rung.dest
                for hypothesis in confirmed
                if hypothesis.get("kind") == "liveness"
                for rung in hypothesis.get("holds", ())
                if hasattr(rung, "dest")
            }
            if "x_RotateSensor" in liveness:
                liveness_correction = event
            absence_roots = {
                rung.dest
                for hypothesis in confirmed
                if hypothesis.get("kind") == "absence-root"
                for rung in hypothesis.get("holds", ())
                if hasattr(rung, "dest")
            }
            if "x_SailRelay" in absence_roots:
                sail_correction = event
            latch_exposure = {
                rung.dest
                for hypothesis in confirmed
                if hypothesis.get("kind") == "latch-exposure"
                for rung in hypothesis.get("holds", ())
                if hasattr(rung, "dest")
            }
            if sail_correction is not None:
                post_sail_door_corrections.update(
                    latch_exposure & {"x_DoorClosed", "x_LintDoorClosed"}
                )
        if event.kind == "finished":
            finished = event
            break
        if time.monotonic() > deadline:
            pytest.fail(
                "cold avoided-Complete drive did not correct its Execute watchdog departure"
            )

    assert door_correction is not None
    assert liveness_correction is not None
    assert sail_correction is not None
    # Bounded correction proof owns one recorded fault. The post-sail door
    # contacts may therefore be learned in successive incidents instead of a
    # single eager reconstruction, but both must be present before completion.
    assert post_sail_door_corrections == {"x_DoorClosed", "x_LintDoorClosed"}
    assert finished is not None
    assert finished.data["reached"] is True
    assert liveness_correction.data["investigation"]["confirmed"] > 0
    assert any(
        hypothesis.get("kind") == "liveness"
        for hypothesis in liveness_correction.data["investigation"]["confirmed_detail"]
    )
    assert any(
        hypothesis.get("kind") == "absence-root"
        and any(getattr(rung, "dest", None) == "x_SailRelay" for rung in hypothesis["holds"])
        for hypothesis in sail_correction.data["investigation"]["confirmed_detail"]
    )
    assert any(
        event.kind == "candidate_accepted"
        and tuple(event.data.get("applied") or ()) == (("Cmd_State_Unhold", True),)
        for event in events
    )
    assert any(
        pair[0] == "Internal__Step" and pair[1] >= 109
        for event in events
        if event.kind == "provisional_promoted"
        for pair in event.data.get("landing_mark") or ()
    )
    assert not any(
        event.kind == "candidate_accepted"
        and ("Cmd_State_Hold", True) in tuple(event.data.get("applied") or ())
        for event in events
    )
    assert not any(
        any(
            getattr(rung, "dest", None) == "Test_Simulate_1st_Scan"
            for rung in hypothesis.get("holds", ())
        )
        for event in events
        if event.kind == "trend_regression"
        for hypothesis in (event.data.get("investigation") or {}).get(
            "confirmed_detail",
            (),
        )
    )
    skeleton = extract_skeleton(events)
    _assert_zoom_tripwire(skeleton)
    _assert_no_factory_reset(skeleton)

    # The program-owned route must earn completion without Pilot pressing the
    # avoided operator shortcut, and its record must show that it traversed the
    # production recipe rather than finding an unrelated path to the output.
    pressed = _all_action_tags(skeleton)
    assert "Cmd_State_Complete" not in pressed, (
        f"pilot pressed the avoided Complete button: {sorted(pressed)}"
    )
    assert _recipe_era_evidence(skeleton), (
        "reached completion without any recipe-era beats (Fluffing timer, "
        "Internal__Step >= 103, or a HELD(11) passage) in the decision record"
    )

    _assert_matches_golden(
        skeleton,
        GOLDEN_DIR / "how_completed_avoid_complete_progress_skeleton.json",
    )


def _all_action_tags(skeleton: list[dict]) -> set[str]:
    """Every tag the pilot actually pressed/held on a committed action event."""
    tags: set[str] = set()
    for entry in skeleton:
        if entry["kind"] not in (
            "candidate_accepted",
            "trial_committed",
            "batch_accepted",
            "widening_accepted",
        ):
            continue
        for pair in entry.get("applied") or ():
            tags.add(pair[0])
        candidate = entry.get("candidate")
        if isinstance(candidate, dict):
            tags.update(candidate.keys())
    return tags


def _recipe_era_evidence(skeleton: list[dict]) -> bool:
    """Did the drive show the recipe-era beats the internal route requires?

    The Bench ground truth (test_constructive_route_to_completed) passes
    Internal__Step 103/105/107/109, a HELD(11) passage, and the Fluffing
    timer.  Any of those in the decision record is evidence the pilot
    actually worked the production SFC rather than shortcutting.
    """
    dumped = json.dumps(skeleton)
    if "S_Fluffing_tmr" in dumped or "S_CurrStep_Fluff" in dumped:
        return True
    for entry in skeleton:
        for field in ("to_value", "settled_value", "zoom_actual_value", "channel_value"):
            if entry.get(field) == 11 and "Sts_StateCurrent" in json.dumps(entry):
                return True
        for field in ("gauge_at_source", "landing_mark"):
            for pair in entry.get(field) or ():
                if pair[0] == "Internal__Step" and isinstance(pair[1], int) and pair[1] >= 103:
                    return True
    return False
