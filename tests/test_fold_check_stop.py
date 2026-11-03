"""The fold check can fail: each comparison in tests/fold_check.py, against a broken run."""

from __future__ import annotations

import ast
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from pyrung import (
    PLC,
    Bool,
    Int,
    Program,
    Real,
    Rung,
    calc,
    copy,
    latch,
    on_delay,
    out,
    rise,
    run_function,
)
from pyrung.core import Timer, compiled_run, scan_trace
from pyrung.core.harness import Harness
from pyrung.core.physical import Approach, Physical
from pyrung.core.system_points import system
from tests import fold_check
from tests.fold_check import FoldMismatch


def _plc() -> PLC:
    n = Int("N")
    with Program(strict=False) as prog:
        with Rung():
            calc(n + 1, n)
    return PLC(prog)


def _stepper(plc: PLC, scans: int):
    def call() -> None:
        for _ in range(scans):
            plc.step()

    return call


def test_a_late_stop_is_caught() -> None:
    plc = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="stop predicate true at scan 5, folded run stopped at 9"),
    ):
        fold_check._checked("run_until", plc, _stepper(plc, 9), lambda s: s.tags["N"] >= 5)


def test_a_stop_on_the_first_true_scan_passes() -> None:
    plc = _plc()
    fold_check._checked("run_until", plc, _stepper(plc, 5), lambda s: s.tags["N"] >= 5)


def test_a_predicate_over_the_real_runner_is_not_asked() -> None:
    plc = _plc()
    before = fold_check.STATS["predicate skipped: reads the real runner"]
    fold_check._checked("run_until", plc, _stepper(plc, 9), lambda _s: plc.state.tags["N"] >= 5)
    assert fold_check.STATS["predicate skipped: reads the real runner"] == before + 1


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch):
    """The real wrappers, installed for this test only (everything is restored)."""
    import pyrung.core.analysis.pilot.coast as pilot_coast
    import pyrung.core.analysis.pilot.cyclefold as cyclefold
    import pyrung.core.coast_run as coast_run
    import pyrung.core.fold as fold

    for mod, name in (
        (fold, "fold_run_until"),
        (fold, "fold_run_for"),
        (cyclefold, "cycle_fold_until"),
        (coast_run, "coast_until"),
        (pilot_coast, "coast_until"),
        (compiled_run, "run_scans"),
        (compiled_run, "INTERPRETED_PREFIX"),
        (scan_trace, "MIN_RUN_SCANS"),
        (scan_trace, "BURST_SIZES"),
        (scan_trace, "STRICT_SELF_CHECK"),
    ):
        monkeypatch.setattr(mod, name, getattr(mod, name))
    monkeypatch.setattr(compiled_run, "_fold_check_installed", False, raising=False)
    return fold, coast_run


def _overshooting(extra: int):
    def drive(plc, predicate, *a, **k):
        while not predicate(plc.state):
            plc.step()
        for _ in range(extra):
            plc.step()
        return plc.state

    return drive


def _overshooting_scans(extra: int):
    drive = _overshooting(extra)

    def run_scans(plc, _scans, predicate, **_kwargs):
        return drive(plc, predicate)

    return run_scans


def test_run_until_through_install_catches_a_late_stop(installed, monkeypatch) -> None:
    monkeypatch.setattr(compiled_run, "run_scans", _overshooting_scans(3))
    fold_check.install()
    plc = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="stop predicate true at scan 5, folded run stopped at 8"),
    ):
        plc.run_until(lambda s: s.tags["N"] >= 5, max_cycles=100)


def test_run_until_through_install_passes_an_exact_stop(installed, monkeypatch) -> None:
    monkeypatch.setattr(compiled_run, "run_scans", _overshooting_scans(0))
    fold_check.install()
    plc = _plc()
    plc.run_until(lambda s: s.tags["N"] >= 5, max_cycles=100)
    assert plc.state.scan_id == 5


def test_a_coast_through_install_catches_a_late_stop(installed, monkeypatch) -> None:
    _, coast_run = installed

    def overshoot(plc, predicate, **_kwargs):
        return _overshooting(2)(plc, predicate)

    monkeypatch.setattr(coast_run, "coast_until", overshoot)
    fold_check.install()
    plc = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="stop predicate true at scan 5, folded run stopped at 7"),
    ):
        coast_run.coast_until(plc, lambda s: s.tags["N"] >= 5, budget=100)


def test_a_coast_through_install_catches_a_run_past_its_budget(installed, monkeypatch) -> None:
    _, coast_run = installed

    def past(plc, _predicate, *, budget, **_kwargs):
        for _ in range(budget + 1):
            plc.step()
        return False

    monkeypatch.setattr(coast_run, "coast_until", past)
    fold_check.install()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="asked for 4 scans from scan 0, landed at 5"),
    ):
        coast_run.coast_until(_plc(), lambda _s: False, budget=4)


def test_a_stateful_predicate_is_not_advanced_by_the_check() -> None:
    plc = _plc()
    calls = []

    def predicate(s) -> bool:
        calls.append(s.scan_id)
        return s.tags["N"] >= 5

    fold_check._checked("run_until", plc, _stepper(plc, 5), predicate)
    assert calls == []


def test_a_predicate_that_swallows_exceptions_is_still_detected() -> None:
    plc = _plc()
    before = fold_check.STATS["predicate skipped: reads the real runner"]

    def predicate(_s) -> bool:
        try:
            return plc.state.tags["N"] >= 5
        except Exception:
            return False

    fold_check._checked("run_until", plc, _stepper(plc, 9), predicate)
    assert fold_check.STATS["predicate skipped: reads the real runner"] == before + 1


def test_a_counting_predicate_is_judged_from_its_own_copy() -> None:
    plc = _plc()
    k = [0]

    def predicate(_s) -> bool:
        k[0] += 1
        return k[0] >= 5

    # The folded run's own calls are not made here; the copy is taken first and
    # the real closure is untouched afterwards.
    fold_check._checked("run_until", plc, _stepper(plc, 5), predicate)
    assert k == [0]


# --- A mismatch nobody declared fails, whoever catches it --------------------------------


SWALLOWS = """
from tests import fold_check


def test_swallows_a_mismatch():
    try:
        raise fold_check._mismatch("fold-check: provoked")
    except Exception:
        pass


def test_after_it():
    pass
"""

DECLARES = """
from tests import fold_check


def test_declares_a_mismatch():
    with fold_check.deliberate():
        try:
            raise fold_check._mismatch("fold-check: provoked")
        except Exception:
            pass
"""


def _session(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    """A session of its own: what fails it is the point, and this one must not fail."""
    test = tmp_path / "test_swallow.py"
    test.write_text(source, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "--fold-check",
            "--rootdir",
            str(tmp_path),
            "-q",
            "-p",
            "no:cacheprovider",
            str(test),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=Path(__file__).parent.parent,
        timeout=120,
    )


def test_a_swallowed_mismatch_fails_its_test_and_the_session(tmp_path: Path) -> None:
    result = _session(tmp_path, SWALLOWS)
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout
    assert "test_swallows_a_mismatch" in result.stdout
    assert "a fold mismatch was caught and hidden: fold-check: provoked" in result.stdout
    assert "2 passed, 1 error" in result.stdout, "its teardown fails, and no other test"
    assert "MISMATCH in " in result.stdout


def test_a_declared_mismatch_fails_nothing(tmp_path: Path) -> None:
    result = _session(tmp_path, DECLARES)
    assert result.returncode == pytest.ExitCode.OK, result.stdout
    assert "deliberate mismatch in " in result.stdout


def test_a_mismatch_seen_only_at_the_end_fails_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside any test (a fixture of a wider scope, say): the session still answers."""
    from types import SimpleNamespace

    from tests import conftest

    session: Any = SimpleNamespace(
        config=SimpleNamespace(getoption=lambda *_a, **_k: False), exitstatus=0
    )
    monkeypatch.setattr(fold_check, "MISMATCHES", [])
    monkeypatch.setattr(fold_check, "SELF_CHECK_MISMATCHES", [])
    conftest.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 0
    monkeypatch.setattr(fold_check, "SELF_CHECK_MISMATCHES", [("?", "message")])
    conftest.pytest_sessionfinish(session, 0)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


# --- A run does what was asked of it -----------------------------------------------------


NEVER = Bool("ProbeNever")
Go, Y = Bool("Go"), Bool("Y")
Cnt, Hr = Int("Cnt"), Int("Hr")
Tm = Timer.clone("Tm")


def _timed() -> PLC:
    """On a timer, so the guarded loop runs long stretches of it."""
    with Program(strict=False) as program:
        with Rung(Go):
            on_delay(Tm, 5000)
        with Rung(Tm.Done):
            latch(Y)
        with Rung(Y):
            calc(Cnt + 1, Cnt)
    plc = PLC(program, dt=0.01)
    plc.patch({"Go": True})
    return plc


def _short_of(scans: int):
    """_run_compiled that stops *scans* early and says it is done."""
    real = compiled_run._run_compiled

    def run(plc, kernel, budget, predicate):
        real(plc, kernel, budget - scans, predicate)
        return 0

    return run


def test_a_run_landing_short_of_the_scans_asked_is_caught(installed, monkeypatch) -> None:
    monkeypatch.setattr(compiled_run, "_run_compiled", _short_of(100))
    fold_check.install()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="asked for 2000 scans from scan 0, landed at 1900"),
    ):
        _timed().run_for(20.0)


def test_a_run_until_returning_with_the_predicate_false_is_caught(installed, monkeypatch) -> None:
    monkeypatch.setattr(compiled_run, "_run_compiled", _short_of(100))
    fold_check.install()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="100 of 2000 scans left and the stop predicate false"),
    ):
        _timed().run_until(NEVER, max_cycles=2000)


def test_a_run_until_stopping_where_the_predicate_holds_passes(installed) -> None:
    fold_check.install()
    plc = _timed()
    plc.run_until(Y, max_cycles=3000)
    assert plc.state.scan_id == 500
    assert plc.state.tags["Y"] is True


def test_a_run_until_stopping_on_its_first_scan_passes(installed) -> None:
    fold_check.install()
    plc = _timed()
    plc.run_until(lambda s: s.scan_id >= 1, max_cycles=100)
    assert plc.state.scan_id == 1


def test_a_run_past_the_scans_asked_is_caught() -> None:
    plc = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="asked for 5 scans from scan 0, landed at 9"),
    ):
        fold_check._checked("compiled", plc, _stepper(plc, 9), budget=5)


def test_a_run_of_no_scans_when_some_were_asked_is_caught() -> None:
    plc = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="asked for 5 scans from scan 0, landed at 0"),
    ):
        fold_check._checked("compiled", plc, lambda: None, budget=5)


def test_a_short_landing_a_predicate_cannot_judge_is_counted() -> None:
    plc = _plc()
    before = fold_check.STATS["short landing not judged: no answer from the predicate"]
    fold_check._checked(
        "compiled", plc, _stepper(plc, 9), lambda _s: plc.state.tags["N"] >= 9, budget=20
    )
    assert fold_check.STATS["short landing not judged: no answer from the predicate"] == before + 1


def test_a_long_short_landing_with_the_predicate_false_is_caught(monkeypatch) -> None:
    monkeypatch.setattr(fold_check, "MAX_SCANS", 100)
    plc = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="50 of 200 scans left and the stop predicate false"),
    ):
        fold_check._checked("compiled", plc, _stepper(plc, 150), lambda _s: False, budget=200)


def test_a_long_short_landing_where_the_predicate_holds_passes(monkeypatch) -> None:
    monkeypatch.setattr(fold_check, "MAX_SCANS", 100)
    plc = _plc()
    fold_check._checked(
        "compiled", plc, _stepper(plc, 150), lambda s: s.tags["N"] >= 150, budget=200
    )
    assert plc.state.scan_id == 150


def test_a_long_short_landing_with_no_answer_is_listed(monkeypatch) -> None:
    monkeypatch.setattr(fold_check, "MAX_SCANS", 100)
    plc = _plc()
    reason = "short landing not judged: too long to step, no answer from the predicate"
    before = fold_check.STATS[reason]
    fold_check._checked(
        "compiled", plc, _stepper(plc, 150), lambda _s: plc.state.tags["N"] >= 150, budget=200
    )
    assert fold_check.STATS[reason] == before + 1
    assert reason + ", tests:" in "\n".join(fold_check.summary())


# --- A run that raises is held to stepping's raise ----------------------------------------


def _raises_at_1500(v):
    if v >= 1500:
        raise ValueError("boom")
    return {"out": v}


def _raising(function: Any = _raises_at_1500) -> PLC:
    """On a timer and a count: lands in the loop, and raises once the count gets there."""
    with Program(strict=False) as program:
        with Rung(Go):
            on_delay(Tm, 2000)
        with Rung():
            calc(Cnt + 1, Cnt)
        with Rung():
            run_function(function, ins={"v": Cnt}, outs={"out": Hr})
    plc = PLC(program, dt=0.01)
    plc.patch({"Go": True})
    return plc


def test_a_raised_run_is_compared_and_raises_on(installed) -> None:
    fold_check.install()
    before = fold_check.STATS["raised runs compared"]
    plc = _raising()
    with pytest.raises(ValueError, match="boom"):
        plc.run_for(30.0)
    assert plc.state.scan_id == 1499
    assert fold_check.STATS["raised runs compared"] == before + 1


def test_a_raised_run_landing_on_the_wrong_state_is_caught(installed, monkeypatch) -> None:
    real = compiled_run._install

    def scanned_past(plc, replay, state):
        return real(plc, replay, state.with_tags({"Cnt": state.tags["Cnt"] + 300}))

    monkeypatch.setattr(compiled_run, "_install", scanned_past)
    fold_check.install()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="differs from stepping.*Cnt: folded"),
    ):
        _raising().run_for(30.0)


def test_a_raise_stepping_does_not_make_is_caught(installed, monkeypatch) -> None:
    real = compiled_run._run_compiled

    def spurious(plc, kernel, budget, predicate):
        real(plc, kernel, budget // 2, predicate)
        raise RuntimeError("spurious kernel raise")

    monkeypatch.setattr(compiled_run, "_run_compiled", spurious)
    fold_check.install()
    with fold_check.deliberate(), pytest.raises(FoldMismatch, match="stepping ran on"):
        _timed().run_for(20.0)


def _no_twin_can_follow(monkeypatch) -> None:
    monkeypatch.setattr(fold_check, "MAX_SCANS", 100)
    monkeypatch.setattr(fold_check, "MAX_PLAIN_COMPILED_SCANS", 100)


def test_a_raise_too_long_to_compare_is_judged_on_its_last_scan(installed, monkeypatch) -> None:
    _no_twin_can_follow(monkeypatch)
    fold_check.install()
    reason = "raised runs too long to compare, their last scan judged"
    before = fold_check.STATS[reason]
    plc = _raising()
    with pytest.raises(ValueError, match="boom"):
        plc.run_for(30.0)
    assert plc.state.scan_id == 1499
    assert fold_check.STATS[reason] == before + 1


def test_a_spurious_raise_too_long_to_compare_is_caught(installed, monkeypatch) -> None:
    _no_twin_can_follow(monkeypatch)
    real = compiled_run._run_compiled

    def spurious(plc, kernel, budget, predicate):
        real(plc, kernel, budget // 2, predicate)
        raise RuntimeError("spurious kernel raise")

    monkeypatch.setattr(compiled_run, "_run_compiled", spurious)
    fold_check.install()
    with fold_check.deliberate(), pytest.raises(FoldMismatch, match="stepping ran on"):
        _timed().run_for(20.0)


def test_another_raise_too_long_to_compare_is_caught(monkeypatch) -> None:
    _no_twin_can_follow(monkeypatch)
    plc = _raising()

    def call() -> None:
        for _ in range(1499):
            plc.step()
        raise KeyError("not what stepping raises")

    with fold_check.deliberate(), pytest.raises(FoldMismatch, match="stepping raised ValueError"):
        fold_check._checked("compiled", plc, call, budget=2000)


def test_another_exception_type_is_caught() -> None:
    def raises_at_5(v):
        if v >= 5:
            raise ValueError("boom")
        return {"out": v}

    plc = _raising(raises_at_5)

    def call() -> None:
        for _ in range(4):
            plc.step()
        raise KeyError("not what stepping raises")

    with fold_check.deliberate(), pytest.raises(FoldMismatch, match="stepping raised ValueError"):
        fold_check._checked("compiled", plc, call, budget=10)


def test_a_raise_from_the_predicate_on_the_landing_is_judged_there() -> None:
    plc = _plc()

    def predicate(state) -> bool:
        if state.tags["N"] >= 5:
            raise LookupError("no answer")
        return False

    def call() -> None:
        for _ in range(5):
            plc.step()
        predicate(plc.state)

    before = fold_check.STATS["raised runs compared"]
    with pytest.raises(LookupError):
        fold_check._checked("compiled", plc, call, predicate, budget=10)
    assert fold_check.STATS["raised runs compared"] == before + 1


def test_a_raise_before_any_scan_is_listed() -> None:
    plc = _plc()

    def call() -> None:
        raise TypeError("bad argument")

    reason = "raised run not judged: raised before any scan"
    before = fold_check.STATS[reason]
    with pytest.raises(TypeError):
        fold_check._checked("compiled", plc, call, budget=10)
    assert fold_check.STATS[reason] == before + 1
    assert reason + ", tests:" in "\n".join(fold_check.summary())


def test_stepping_raising_where_the_run_went_on_is_caught() -> None:
    raising = [False]

    def raises_when_told(v):
        if raising[0]:
            raise ValueError("boom")
        return {"out": v}

    plc = _raising(raises_when_told)

    def call() -> None:
        for _ in range(9):
            plc.step()
        raising[0] = True

    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="stepping raised ValueError after scan 0"),
    ):
        fold_check._checked("run_until", plc, call)


# --- own_twin() lets off one runner's twin, and nothing else ------------------------------


def test_own_twin_still_holds_a_run_to_the_scans_asked(installed, monkeypatch) -> None:
    monkeypatch.setattr(compiled_run, "_run_compiled", _short_of(100))
    fold_check.install()
    plc = _timed()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="asked for 2000 scans from scan 0, landed at 1900"),
        fold_check.own_twin(plc),
    ):
        plc.run_for(20.0)


def test_own_twin_leaves_other_runners_checked(installed, monkeypatch) -> None:
    def stale(plc: PLC) -> None:
        plc._state_tags_read_cache = {**plc._state.tags, "Cnt": -1}
        plc._state_tags_read_cache_state = plc._state

    fold_check.install()
    plc = _timed()
    other = _timed()
    with fold_check.own_twin(plc) as compare:
        plc.run_for(20.0)
        twin = _timed()
        for _ in range(2000):
            twin.step()
        compare(twin)
        _after_install(monkeypatch, stale)
        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="read mirror at scan"):
            other.run_for(20.0)


def test_own_twin_must_compare() -> None:
    plc = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="never compared its run"),
        fold_check.own_twin(plc),
    ):
        pass


def test_own_twin_compare_sees_a_difference() -> None:
    plc = _plc()
    plc.run(3)
    twin = _plc()
    twin.run(3)
    twin.patch({"N": 40})
    twin.step()
    plc.step()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="differs from stepping: N"),
        fold_check.own_twin(plc) as compare,
    ):
        compare(twin)


def test_own_twin_compare_sees_another_scan() -> None:
    plc = _plc()
    twin = _plc()
    twin.step()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="run at scan 0, stepped at 1"),
        fold_check.own_twin(plc) as compare,
    ):
        compare(twin)


def test_own_twin_is_listed_in_the_summary() -> None:
    plc = _plc()
    with fold_check.own_twin(plc) as compare:
        compare(_plc())
    assert "skipped: the test steps its own twin, tests:" in "\n".join(fold_check.summary())


def test_own_twin_refuses_the_run_itself() -> None:
    plc = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="compared with itself"),
        fold_check.own_twin(plc) as compare,
    ):
        compare(plc)


def _stepped(scans: int) -> PLC:
    twin = _timed()
    for _ in range(scans):
        twin.step()
    return twin


def test_own_twin_refuses_a_compare_from_before_the_run(installed) -> None:
    fold_check.install()
    plc = _timed()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="last compare is of scan 0, its run got to scan 2000"),
        fold_check.own_twin(plc) as compare,
    ):
        compare(_timed())
        plc.run_for(20.0)


def test_own_twin_refuses_a_second_run_after_the_compare(installed) -> None:
    fold_check.install()
    plc = _timed()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="last compare is of scan 2000, its run got to scan 4000"),
        fold_check.own_twin(plc) as compare,
    ):
        plc.run_for(20.0)
        compare(_stepped(2000))
        plc.run_for(20.0)


def test_own_twin_refuses_a_second_run_with_nothing_installed() -> None:
    plc = _plc()
    plc.run(3)
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="last compare is of scan 3, its run got to scan 5"),
        fold_check.own_twin(plc) as compare,
    ):
        twin = _plc()
        twin.run(3)
        compare(twin)
        plc.run(2)


def test_own_twin_passes_a_compare_of_where_the_run_got_to(installed) -> None:
    fold_check.install()
    plc = _timed()
    with fold_check.own_twin(plc) as compare:
        plc.run_for(20.0)
        compare(_stepped(2000))
        plc.run_for(20.0)
        compare(_stepped(4000))


def test_own_twin_compare_sees_the_runner_around_the_state() -> None:
    plc = _plc()
    plc.run(3)
    twin = _plc()
    twin.run(3)
    twin.force("Unrelated", True)
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="the runner differs at scan 3: forces"),
        fold_check.own_twin(plc) as compare,
    ):
        compare(twin)


def test_own_twin_holds_a_block_that_raises_to_its_compare() -> None:
    plc = _plc()
    before = len(fold_check.MISMATCHES)
    with pytest.raises(KeyError):
        with fold_check.own_twin(plc):
            raise KeyError("the test's own failure")
    assert [m for _, m in fold_check.MISMATCHES[before:]] == [
        "fold-check (own twin): the block never compared its run"
    ]
    del fold_check.MISMATCHES[before:]


def test_own_twin_holds_a_run_that_raises_to_its_landing(installed) -> None:
    fold_check.install()
    plc = _raising()
    before = len(fold_check.MISMATCHES)
    with pytest.raises(ValueError, match="boom"):
        with fold_check.own_twin(plc) as compare:
            compare(_raising())
            plc.run_for(30.0)
    assert [m for _, m in fold_check.MISMATCHES[before:]] == [
        "fold-check (own twin): the block's last compare is of scan 0, its run got to scan 1499"
    ]
    del fold_check.MISMATCHES[before:]


def test_own_twin_passes_a_block_that_raises_after_its_compare() -> None:
    plc = _plc()
    before = len(fold_check.MISMATCHES)
    with pytest.raises(KeyError):
        with fold_check.own_twin(plc) as compare:
            compare(_plc())
            raise KeyError("the test's own failure")
    assert fold_check.MISMATCHES[before:] == []


def _uses_of_own_twin(path: Path) -> set[str]:
    """Each function of *path* that names own_twin, whether or not it calls it there."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    used: set[str] = set()

    def names_it(node: ast.AST) -> bool:
        return (isinstance(node, ast.Attribute) and node.attr == "own_twin") or (
            isinstance(node, ast.Name) and node.id == "own_twin"
        )

    for top in tree.body:
        if any(names_it(node) for node in ast.walk(top)):
            used.add(getattr(top, "name", f"line {top.lineno}"))
    return used


def test_the_tests_using_own_twin_are_these() -> None:
    root = Path(__file__).parent
    users = {
        f"{path.relative_to(root).as_posix()}::{use}"
        for path in root.rglob("*.py")
        for use in _uses_of_own_twin(path)
    }
    assert users == {
        "core/test_history_against_stepping.py::test_a_mutable_default_is_the_function_s_own_list",
        "core/test_run_function_arguments.py::_a_run_lands_where_its_own_twin_does",
        *(
            f"test_fold_check_stop.py::{name}"
            for name in (
                "test_inside_an_own_twin_block_it_may_land_short",
                "test_own_twin_still_holds_a_run_to_the_scans_asked",
                "test_own_twin_leaves_other_runners_checked",
                "test_own_twin_must_compare",
                "test_own_twin_compare_sees_a_difference",
                "test_own_twin_compare_sees_another_scan",
                "test_own_twin_is_listed_in_the_summary",
                "test_own_twin_refuses_the_run_itself",
                "test_own_twin_refuses_a_compare_from_before_the_run",
                "test_own_twin_refuses_a_second_run_after_the_compare",
                "test_own_twin_refuses_a_second_run_with_nothing_installed",
                "test_own_twin_passes_a_compare_of_where_the_run_got_to",
                "test_own_twin_compare_sees_the_runner_around_the_state",
                "test_own_twin_holds_a_block_that_raises_to_its_compare",
                "test_own_twin_holds_a_run_that_raises_to_its_landing",
                "test_own_twin_passes_a_block_that_raises_after_its_compare",
            )
        ),
    }


# --- deliberate() means one mismatch ------------------------------------------------------


def test_deliberate_with_no_mismatch_fails() -> None:
    before = len(fold_check.MISMATCHES)
    with pytest.raises(FoldMismatch, match="expects one mismatch, this block had 0"):
        with fold_check.deliberate():
            pass
    del fold_check.MISMATCHES[before:]


def test_deliberate_declares_only_the_first_mismatch() -> None:
    before, declared = len(fold_check.MISMATCHES), len(fold_check.DECLARED)
    with pytest.raises(FoldMismatch, match="expects one mismatch, this block had 2"):
        with fold_check.deliberate():
            fold_check._mismatch("the one the test provoked")
            fold_check._mismatch("a real one")
    assert [m for _, m in fold_check.DECLARED[declared:]] == ["the one the test provoked"]
    assert "a real one" in [m for _, m in fold_check.MISMATCHES[before:]]
    del fold_check.MISMATCHES[before:]


def test_deliberate_does_not_cover_another_thread() -> None:
    before = len(fold_check.MISMATCHES)

    def elsewhere() -> None:
        fold_check._mismatch("from another thread")

    with fold_check.deliberate():
        thread = threading.Thread(target=elsewhere)
        thread.start()
        thread.join()
        fold_check._mismatch("the one the test provoked")
    assert [m for _, m in fold_check.MISMATCHES[before:]] == ["from another thread"]
    del fold_check.MISMATCHES[before:]


def test_deliberate_does_not_mask_what_escapes_it() -> None:
    before = len(fold_check.MISMATCHES)
    with pytest.raises(KeyError):
        with fold_check.deliberate():
            raise KeyError("the test's own failure")
    del fold_check.MISMATCHES[before:]


def test_deliberate_holds_a_block_that_raises_to_its_count() -> None:
    before = len(fold_check.MISMATCHES)
    with pytest.raises(KeyError):
        with fold_check.deliberate():
            raise KeyError("the test's own failure")
    assert [m for _, m in fold_check.MISMATCHES[before:]] == [
        "fold-check: deliberate() expects one mismatch, this block had 0"
    ]
    del fold_check.MISMATCHES[before:]

    with pytest.raises(KeyError):
        with fold_check.deliberate():
            fold_check._mismatch("the one the test provoked")
            fold_check._mismatch("a real one")
            raise KeyError("the test's own failure")
    assert [m for _, m in fold_check.MISMATCHES[before:]] == [
        "a real one",
        "fold-check: deliberate() expects one mismatch, this block had 2",
    ]
    del fold_check.MISMATCHES[before:]


def test_deliberate_is_content_with_one_mismatch_and_a_raise() -> None:
    before = len(fold_check.MISMATCHES)
    with pytest.raises(FoldMismatch):
        with fold_check.deliberate():
            raise fold_check._mismatch("the one the test provoked")
    assert fold_check.MISMATCHES[before:] == []


# --- A coast keeps less than stepping, and what it keeps is stepping's -------------------


def _counting() -> PLC:
    n = Int("N")
    with Program(strict=False) as prog:
        with Rung():
            calc(n + 1, n)
    return PLC(prog, checkpoint_interval=10)


def _coast_that(change: Any, kind: str = "coast", *, plc: PLC | None = None, scans: int = 100):
    """Check a run of *scans*, with *change* done to what it left."""
    plc = _counting() if plc is None else plc

    def call() -> None:
        for _ in range(scans):
            plc.step()
        change(plc)

    fold_check._checked(kind, plc, call, budget=scans)
    return plc


def _drop_checkpoints(plc: PLC) -> None:
    plc._thin_checkpoints(3)


def test_a_coast_may_keep_fewer_checkpoints() -> None:
    plc = _coast_that(_drop_checkpoints)
    assert len(plc._checkpoints) < 10


def test_only_a_coast_may() -> None:
    with fold_check.deliberate(), pytest.raises(FoldMismatch, match="the runner differs"):
        _coast_that(_drop_checkpoints, "compiled")


def test_a_checkpoint_stepping_does_not_have_is_caught() -> None:
    def one_more(plc: PLC) -> None:
        plc._checkpoints[33] = plc._state

    with fold_check.deliberate(), pytest.raises(FoldMismatch, match="checkpoints stepping does"):
        _coast_that(one_more)


def test_a_kept_checkpoint_of_another_state_is_caught() -> None:
    def wrong(plc: PLC) -> None:
        plc._checkpoints[50] = plc._checkpoints[40]

    with fold_check.deliberate(), pytest.raises(FoldMismatch, match="checkpoint"):
        _coast_that(wrong)


def test_a_kept_checkpoint_with_no_force_record_is_caught() -> None:
    def forget(plc: PLC) -> None:
        del plc._scan_log._force_changes_by_scan[50]

    with fold_check.deliberate(), pytest.raises(FoldMismatch, match="no force record"):
        _coast_that(forget)


def test_a_force_record_stepping_does_not_have_is_caught() -> None:
    def invent(plc: PLC) -> None:
        plc._scan_log._force_changes_by_scan[50] = {"N": 99}

    with fold_check.deliberate(), pytest.raises(FoldMismatch, match="forces recorded at scan 50"):
        _coast_that(invent)


def test_install_leaves_what_a_coast_keeps_alone(installed) -> None:
    _, coast_run = installed
    names = ("ENTRY_WAIT", "STRETCH_CHECKPOINT_INTERVALS", "MAX_CHECKPOINTS", "MAX_RECORD_RUNS")
    before = [getattr(coast_run, name) for name in names]
    fold_check.install()
    assert [getattr(coast_run, name) for name in names] == before


def test_a_real_thinned_coast_passes(installed, monkeypatch) -> None:
    _, coast_run = installed
    monkeypatch.setattr(coast_run, "MAX_CHECKPOINTS", 3)
    fold_check.install()
    plc = _timed()
    before = fold_check.STATS["checked: coast"]
    coast_run.coast_until(plc, lambda _s: False, budget=5000)
    assert len(plc._checkpoints) <= 3
    assert fold_check.STATS["checked: coast"] == before + 1


class TestScansOutOfTheRecords:
    SETTLED = "scans out of the records, settled to compare"

    def test_they_are_compared_and_left_as_they_were(self, installed, monkeypatch) -> None:
        _, coast_run = installed
        monkeypatch.setattr(coast_run, "MAX_RECORD_RUNS", 5)
        fold_check.install()
        plc = _timed()
        before = fold_check.STATS[self.SETTLED]
        coast_run.coast_until(plc, lambda _s: False, budget=5000)
        assert plc._fold_windows, "a later question still has to find them left out"
        left_out = sum(high - low + 1 for low, high in plc._fold_windows)
        assert fold_check.STATS[self.SETTLED] - before == left_out

    def test_a_wrong_answer_for_them_is_caught(self, installed, monkeypatch) -> None:
        _, coast_run = installed
        monkeypatch.setattr(coast_run, "MAX_RECORD_RUNS", 5)
        real = PLC._stepped_again

        def forgets(self: PLC, first: int, last: int, check_limits: Any) -> PLC:
            replay = real(self, first, last, check_limits)
            replay._rung_firing_timelines = type(replay._rung_firing_timelines)()
            return replay

        monkeypatch.setattr(PLC, "_stepped_again", forgets)
        fold_check.install()
        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="fired on"):
            coast_run.coast_until(_timed(), lambda _s: False, budget=5000)


class TestACoastTooLongToStep:
    PLAIN = "scans compared with plain compiled"
    SKIPPED = "skipped: longer than FOLD_CHECK_MAX_SCANS"

    def test_it_is_compared_with_plain_compiled(self, monkeypatch) -> None:
        monkeypatch.setattr(fold_check, "MAX_SCANS", 50)
        before, skipped = fold_check.STATS[self.PLAIN], fold_check.STATS[self.SKIPPED]
        _coast_that(lambda _plc: None)
        assert fold_check.STATS[self.PLAIN] == before + 100
        assert fold_check.STATS[self.SKIPPED] == skipped

    def test_a_wrong_landing_is_caught(self, monkeypatch) -> None:
        monkeypatch.setattr(fold_check, "MAX_SCANS", 50)

        def wrong(plc: PLC) -> None:
            plc._state = plc._state.set(tags=plc._state.tags.set("N", -1))

        with fold_check.deliberate(), pytest.raises(FoldMismatch):
            _coast_that(wrong)

    def test_a_kept_checkpoint_stepping_does_not_have_is_caught(self, monkeypatch) -> None:
        monkeypatch.setattr(fold_check, "MAX_SCANS", 50)

        def one_more(plc: PLC) -> None:
            plc._checkpoints[33] = plc._state

        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="stepping does not"):
            _coast_that(one_more)

    def test_it_may_keep_fewer_checkpoints(self, monkeypatch) -> None:
        monkeypatch.setattr(fold_check, "MAX_SCANS", 50)
        _coast_that(_drop_checkpoints)


class TestACoastStoppedByTheLedger:
    def _stops_early(self, out_of_work: Any, change: Any = None) -> PLC:
        plc = _counting()

        def call() -> None:
            for _ in range(9):
                plc.step()
            if change is not None:
                change(plc)

        fold_check._checked(
            "coast", plc, call, lambda _s: False, budget=100, out_of_work=out_of_work
        )
        return plc

    def test_it_may_land_short_with_the_predicate_false(self) -> None:
        assert self._stops_early(lambda: True).state.scan_id == 9

    def test_nothing_else_may(self) -> None:
        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="predicate false"):
            self._stops_early(lambda: False)

    def test_what_it_left_is_still_compared(self) -> None:
        def wrong(plc: PLC) -> None:
            plc._state = plc._state.set(tags=plc._state.tags.set("N", -1))

        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="N"):
            self._stops_early(lambda: True, wrong)

    def test_install_reads_it_from_the_coast(self, installed, monkeypatch) -> None:
        _, coast_run = installed
        fold_check.install()
        plc = _timed()
        memo = coast_run.memo_of(plc)
        memo.work_left = lambda: 5
        monkeypatch.setattr(coast_run, "_kernel_for", lambda *_a: None)  # no loop, so it counts
        stats: dict[str, int] = {}
        coast_run.coast_until(plc, lambda _s: False, budget=100, stats=stats)
        assert stats["out_of_work"] == 1

    def test_its_predicate_has_to_be_false_where_it_landed(self) -> None:
        plc = _counting()
        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="predicate true"):
            fold_check._checked(
                "coast",
                plc,
                lambda: [plc.step() for _ in range(9)],
                lambda state: state.scan_id == 9,
                budget=100,
                out_of_work=lambda: True,
            )

    def _stops_too_long_to_step(self, monkeypatch, predicate: Any) -> PLC:
        monkeypatch.setattr(fold_check, "MAX_SCANS", 5)
        plc = _counting()
        fold_check._checked(
            "coast",
            plc,
            lambda: [plc.step() for _ in range(9)],
            predicate,
            budget=100,
            out_of_work=lambda: True,
        )
        return plc

    def test_too_long_to_step_it_is_still_asked_where_it_landed(self, monkeypatch) -> None:
        assert self._stops_too_long_to_step(monkeypatch, lambda _s: False).state.scan_id == 9
        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="predicate true"):
            self._stops_too_long_to_step(monkeypatch, lambda state: state.scan_id == 9)

    def test_a_stop_is_held_to_the_ledger(self) -> None:
        fold_check._hold_to_the_ledger("coast", 5, {"kernel_scans": 5, "out_of_work": 1})
        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="stopped for its ledger"):
            fold_check._hold_to_the_ledger("coast", 5, {"kernel_scans": 4, "out_of_work": 1})
        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="stopped for its ledger"):
            fold_check._hold_to_the_ledger("coast", None, {"kernel_scans": 4, "out_of_work": 1})

    def test_a_coast_past_its_ledger_is_caught(self) -> None:
        fold_check._hold_to_the_ledger("coast", 5, {"kernel_scans": 6})  # a stretch's landing
        fold_check._hold_to_the_ledger("coast", None, {"kernel_scans": 700})  # no search
        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="with 5 left"):
            fold_check._hold_to_the_ledger("coast", 5, {"kernel_scans": 7})

    def test_a_raised_coast_says_nothing_about_it(self) -> None:
        fold_check._hold_to_the_ledger("coast", 5, {})


def test_inside_an_own_twin_block_it_may_land_short() -> None:
    plc = _counting()
    with fold_check.own_twin(plc) as compare:
        fold_check._checked(
            "coast",
            plc,
            lambda: [plc.step() for _ in range(9)],
            lambda _s: False,
            budget=100,
            out_of_work=lambda: True,
        )
        stepped = _counting()
        stepped.run(9)
        compare(stepped)


# --- The runner around the state ---------------------------------------------------------


def _after_install(monkeypatch: pytest.MonkeyPatch, change: Any) -> None:
    real = compiled_run._install

    def install(plc, replay, state):
        real(plc, replay, state)
        change(plc)

    monkeypatch.setattr(compiled_run, "_install", install)


FORK_SCANS = "scans compared on a fork from inside a range"


def test_a_fork_is_compared_under_a_force(installed) -> None:
    fold_check.install()
    plc = _timed()
    plc.force(Cnt, 7)
    before = fold_check.STATS[FORK_SCANS]
    plc.run_for(20.0)
    assert fold_check.STATS[FORK_SCANS] > before


def test_a_fork_is_compared_under_a_plant(installed) -> None:
    fold_check.install()
    temp = Real("Temp", physical=Physical("TempSensor", profile=Approach(100.0, 5.0)), link="Go")
    with Program(strict=False) as program:
        with Rung():
            copy(temp, Real("Shown"))
        with Rung(Go):
            on_delay(Tm, 5000)
    plc = PLC(program, dt=0.01)
    Harness(plc).install()
    plc.patch({"Go": True})
    before = fold_check.STATS[FORK_SCANS]
    plc.run_for(20.0)
    assert fold_check.STATS[FORK_SCANS] > before


def test_a_stale_read_mirror_on_the_real_runner_is_caught(installed, monkeypatch) -> None:
    def stale(plc: PLC) -> None:
        plc._state_tags_read_cache = {**plc._state.tags, "Cnt": -1}
        plc._state_tags_read_cache_state = plc._state

    _after_install(monkeypatch, stale)
    fold_check.install()
    with (
        fold_check.deliberate(),
        pytest.raises(
            FoldMismatch, match=r"read mirror at scan \d+ is not of its state: \['Cnt'\]"
        ),
    ):
        _timed().run_for(20.0)


def _parked() -> PLC:
    plc = _timed()
    plc.run(5)
    plc.seek(3)
    return plc


def test_a_parked_playhead_stays_put(installed) -> None:
    fold_check.install()
    plc = _parked()
    plc.run_for(20.0)
    assert plc.playhead == 3


def test_a_run_moving_a_parked_playhead_is_caught(installed, monkeypatch) -> None:
    def follow(plc: PLC) -> None:
        plc._playhead = plc._state.scan_id

    plc = _parked()
    _after_install(monkeypatch, follow)
    fold_check.install()
    with fold_check.deliberate(), pytest.raises(FoldMismatch, match="playhead: folded="):
        plc.run_for(20.0)


def _rtc_apply_early() -> PLC:
    with Program(strict=False) as program:
        with Rung(Go):
            on_delay(Tm, 100)
        with Rung(rise(Tm.Done)):
            copy(10, system.rtc.new_second)
            copy(5, system.rtc.new_minute)
            copy(1, system.rtc.new_hour)
            out(system.rtc.apply_time)
        with Rung():
            copy(system.rtc.hour, Hr)
    plc = PLC(program, dt=0.01)
    plc.patch({"Go": True})
    return plc


def _origin_from_the_apply(monkeypatch: pytest.MonkeyPatch, only: PLC | None) -> None:
    real = PLC._set_rtc_and_record

    def record(self, value, sim_time, *, scan_id=None):
        real(self, value, sim_time, scan_id=scan_id)
        if only is None or self is only:
            self._scan_log._rtc_origin = (value, float(sim_time))

    monkeypatch.setattr(PLC, "_set_rtc_and_record", record)


def test_a_wrong_rtc_origin_is_caught_in_the_log(installed, monkeypatch) -> None:
    plc = _rtc_apply_early()
    _origin_from_the_apply(monkeypatch, plc)
    fold_check.install()
    with fold_check.deliberate(), pytest.raises(FoldMismatch, match="rtc base at the start"):
        plc.run_for(30.0)


def test_a_wrong_rtc_origin_is_caught_at_the_scan_before_an_early_apply(
    installed, monkeypatch
) -> None:
    """In both runners' logs, so only the seek against the states stepped shows it."""
    _origin_from_the_apply(monkeypatch, None)
    fold_check.install()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="scan 1 differs from stepping: Hr"),
    ):
        _rtc_apply_early().run_for(30.0)


def test_an_rtc_apply_early_in_the_range_passes(installed) -> None:
    fold_check.install()
    plc = _rtc_apply_early()
    plc.run_for(30.0)
    assert plc.state.tags["Hr"] == 1
