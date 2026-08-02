from types import SimpleNamespace

import pyrung.core.analysis.pilot.investigate as investigate
import pyrung.core.analysis.pilot.investigation_replay as replay


def test_replay_records_are_reexported_without_changing_identity() -> None:
    assert investigate.ReplayStep is replay.ReplayStep
    assert investigate.ReplayOutcome is replay.ReplayOutcome
    assert investigate.RegressionWitness is replay.RegressionWitness
    assert investigate.ExcursionResult is replay.ExcursionResult


def test_regression_ownership_uses_the_investigate_facade_binding(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []

    def cause_replayed(_plc, _witness, *, start_scan: int, end_scan: int) -> bool:
        calls.append((start_scan, end_scan))
        return False

    monkeypatch.setattr(investigate, "_regression_cause_replayed", cause_replayed)
    plc = SimpleNamespace(state=SimpleNamespace(tags={"Channel": False}))
    witness = investigate.RegressionWitness(
        channel_tag="Channel",
        source=False,
        departed=True,
        landing=True,
        departure_scan=1,
        cause=(),
        causal_spine=frozenset(),
    )

    ownership = investigate._regression_ownership(
        plc,
        witness,
        (),
        set(),
        start_scan=3,
        end_scan=7,
    )

    assert calls == [(3, 7)]
    assert ownership.cause_silenced


def test_build_replay_facade_passes_current_local_hooks(monkeypatch) -> None:
    captured: dict[str, replay.ReplayHooks] = {}
    sentinel = object()

    def build_replay(*_args, hooks: replay.ReplayHooks, **_kwargs):
        captured["hooks"] = hooks
        return sentinel

    witness = lambda *_args, **_kwargs: None
    monkeypatch.setattr(replay, "build_replay_fn", build_replay)
    monkeypatch.setattr(investigate, "incident_regression_witness", witness)

    result = investigate.build_replay_fn(None, 0, (), (), ctx=None)

    assert result is sentinel
    assert captured["hooks"].incident_regression_witness is witness
    assert captured["hooks"].regression_cause_replayed is investigate._regression_cause_replayed
    assert captured["hooks"].implicated_writers is investigate._implicated_writers
