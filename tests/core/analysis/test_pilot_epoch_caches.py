"""Lifecycle matrix for history-derived runner caches."""

from __future__ import annotations

import pytest

from pyrung import PLC, Int, Program, Rung, calc, copy
from pyrung.core.executor import ConditionViewCapture

_ALL_CACHES = {
    "recent_states",
    "replay_slabs",
    "replay_trace",
    "replay_captures",
}


def _warm_all(plc: PLC) -> None:
    plc._replay_slabs[-1] = {plc.state.scan_id: plc.state}
    plc._cached_replay_trace = (plc.state.scan_id, {})
    plc._cached_replay_captures[plc.state.scan_id] = ConditionViewCapture()


def _warm_cache_names(plc: PLC) -> set[str]:
    names: set[str] = set()
    if plc._recent_state_cache:
        names.add("recent_states")
    if plc._replay_slabs:
        names.add("replay_slabs")
    if plc._cached_replay_trace is not None:
        names.add("replay_trace")
    if plc._cached_replay_captures:
        names.add("replay_captures")
    return names


@pytest.mark.parametrize(
    ("event", "cleared"),
    [
        ("on_tip_advanced", {"replay_trace", "replay_captures"}),
        ("on_history_trimmed", {"replay_slabs"}),
        (
            "on_runtime_scope_reset",
            {
                "recent_states",
                "replay_trace",
                "replay_captures",
            },
        ),
        ("on_recording_reset", {"replay_slabs"}),
        ("on_replay_anchor_replaced", {"recent_states"}),
        ("on_replay_evidence_discarded", {"replay_trace", "replay_captures"}),
    ],
)
def test_epoch_cache_invalidation_matrix(event: str, cleared: set[str]) -> None:
    plc = PLC(logic=[])
    _warm_all(plc)

    getattr(plc._epoch_caches, event)()

    assert _warm_cache_names(plc) == _ALL_CACHES - cleared
    if "recent_states" in cleared:
        assert plc._recent_state_cache_bytes == 0


def test_tip_advance_invalidates_only_replay_evidence() -> None:
    plc = PLC(logic=[])
    _warm_all(plc)

    plc.step()

    assert _warm_cache_names(plc) == {
        "recent_states",
        "replay_slabs",
    }


def test_history_trim_clears_reconstruction_caches_and_selectively_prunes_recent() -> None:
    plc = PLC(logic=[])
    plc.run(3)
    _warm_all(plc)

    plc._trim_history_before(2)

    assert _warm_cache_names(plc) == {
        "recent_states",
        "replay_trace",
        "replay_captures",
    }
    assert tuple(plc._recent_state_cache) == (2, 3)


def test_stop_to_run_drops_the_replay_slabs_of_the_old_recording() -> None:
    # A restart starts a recording, as a reboot does: scan ids go back to 0, so a slab
    # built on the old log holds another run's states under the ids to come.
    plc = PLC(logic=[])
    plc.step()
    _warm_all(plc)

    plc.stop()
    plc.step()

    assert _warm_cache_names(plc) == {"recent_states"}


def test_a_seek_after_a_restart_answers_from_the_new_run() -> None:
    step, mode, seen = Int("Step"), Int("Mode"), Int("Seen")
    with Program() as program:
        with Rung():
            calc(step + 1, step)
        with Rung():
            copy(mode, seen)
    # A small recent-state cache and a far checkpoint, so a seek reaches for a slab.
    plc = PLC(program, cache=20, history_budget=1_048_576, checkpoint_interval=5000)

    plc.patch({"Mode": 7})
    plc.run(400)
    assert plc.history.at(100).tags["Seen"] == 7
    assert plc._replay_slabs

    plc.stop()
    plc.step()
    plc.patch({"Mode": 0})
    as_they_ran = {}
    for _ in range(400):
        plc.step()
        as_they_ran[plc.state.scan_id] = plc.state.tags

    assert plc.history.at(100).tags == as_they_ran[100]
    assert plc.history.at(100).tags["Seen"] == 0


def test_reboot_composes_runtime_and_recording_reset() -> None:
    plc = PLC(logic=[])
    plc.step()
    _warm_all(plc)

    plc.reboot()

    assert _warm_cache_names(plc) == {"recent_states"}


def test_fork_seals_epoch_without_invalidating_live_runner_caches() -> None:
    source = PLC(logic=[])
    source.step()
    _warm_all(source)

    child = source.fork()

    assert _warm_cache_names(source) == _ALL_CACHES
    assert len(child._causal_lineage.sealed_epochs) == 1
    assert not hasattr(child, "_causal_epoch_snapshots")
