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
from pyrung.core.runner import _compile_avoid
from tests.tumbler.skeleton import divergence_message, dump_skeleton, extract_skeleton

pytestmark = pytest.mark.tumbler

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
REGEN_ENV = "PYRUNG_REGEN_GOLDEN"

EXECUTE_MAX_SCANS = 20_000
COMPLETED_MAX_SCANS = 400_000
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
    requested bearing value or be explicitly classified as a departure
    (``ejected`` — an AMBIENT_DRIFT committed under the ejection guard).
    """
    for index, entry in enumerate(skeleton):
        if entry["kind"] != "zoom_accepted":
            continue
        requested = entry.get("zoom_target_value")
        if requested is None:
            continue  # target-terminal let-run: no channel bearing requested
        landed = entry.get("zoom_actual_value")
        assert landed == requested or entry.get("ejected") is True, (
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
# Internal-route gate: how(Sts_StateCurrent == 17) avoiding the Complete button
# ---------------------------------------------------------------------------


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


@pytest.mark.xfail(
    strict=True,
    reason="step-4 (CoastSession) acceptance gate: with the Complete button avoided "
    "the pilot must earn the internal route (Dry -> Cool -> program Hold -> door "
    "cycle -> Unhold -> Fluff -> Fluffing timer issues Complete); today it "
    "flounders in blind let-run coasts on this fixture",
)
def test_pilot_internal_route_gate_completed_avoiding_shortcut(tumbler_logic) -> None:
    """The internal-route challenge — born as a gate per the shipyard rule.

    ``test_pilot_golden_skeleton_completed`` covers the same target unavoided:
    the pilot reaches 17 by pressing ``Cmd_State_Complete`` from Execute (the
    legal operator shortcut).  Here ``avoid=Cmd_State_Complete`` forbids that
    press, so the only way to 17 is the internal route the hand-driven Bench
    proves (``test_constructive_route_to_completed``): ProductionExecuteSteps
    issues the Complete command itself via ``rise(S_Fluffing_tmr.Done)``.

    The pilot is NOT currently expected to manage this (a related single-Bool
    drive, ``how(y_BurnerLoop)``, is known to flounder in blind let-run coasts
    on this fixture — under separate diagnosis), so per the mechanism-gate rule
    in ``pilot/CLAUDE.md`` the test is born strict-xfail and flips when the
    CoastSession mechanism lands. No golden JSON yet — a floundering
    skeleton would churn; the golden gets recorded when this first
    legitimately passes.

    Fast-fail: the floundering mode repeats ~900-scan Unhold laps at ~90s
    wall each, so the drive loop carries a wall-clock deadline
    (``INTERNAL_ROUTE_WALL_BUDGET_S``) — under strict xfail, tripping it is
    today's expected failure (~4-5 min worst case, one lap of overshoot).
    """
    plc = PLC(tumbler_logic)
    plc.step()
    tags = plc._known_tags_by_name
    target = tags["Sts_StateCurrent"]
    avoid_pred = _compile_avoid(tags["Cmd_State_Complete"])
    deadline = time.monotonic() + INTERNAL_ROUTE_WALL_BUDGET_S
    events = []
    for event in pilot_events(
        plc, target == 17, max_scans=INTERNAL_ROUTE_MAX_SCANS, avoid_pred=avoid_pred
    ):
        events.append(event)
        if event.kind == "finished":
            break
        if time.monotonic() > deadline:
            pytest.fail(
                f"internal-route drive exceeded the {INTERNAL_ROUTE_WALL_BUDGET_S:.0f}s "
                f"wall budget at scan {event.scan} (kind={event.kind}) — the "
                f"floundering let-run mode; a healthy drive finishes far inside it"
            )
    skeleton = extract_skeleton(events)

    finished = _finished(skeleton)
    _assert_zoom_tripwire(skeleton)
    assert finished["reached"] is True, f"internal route not earned: {finished.get('reason')!r}"

    # Internal-route proof, mirroring the Bench: the Complete command was
    # never a pilot action — the program must have issued it itself.
    pressed = _all_action_tags(skeleton)
    assert "Cmd_State_Complete" not in pressed, (
        f"pilot pressed the avoided Complete button: {sorted(pressed)}"
    )

    # And the record shows the recipe era actually happened (Fluffing/step
    # progression or the HELD passage), not some other shortcut.
    assert _recipe_era_evidence(skeleton), (
        "reached 17 without any recipe-era beats (Fluffing timer, "
        "Internal__Step >= 103, or a HELD(11) passage) in the decision record"
    )
