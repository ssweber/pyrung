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
from pathlib import Path

import pytest

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from tests.tumbler.skeleton import divergence_message, dump_skeleton, extract_skeleton

pytestmark = pytest.mark.tumbler

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
REGEN_ENV = "PYRUNG_REGEN_GOLDEN"

EXECUTE_MAX_SCANS = 20_000
COMPLETED_MAX_SCANS = 400_000


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
