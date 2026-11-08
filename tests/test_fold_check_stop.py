"""The long-run check can fail: each comparison in tests/fold_check.py, against a broken run."""

from __future__ import annotations

import ast
import collections
import functools
import subprocess
import sys
import threading
import types
import weakref
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pyrsistent import PMap

from pyrung import (
    PLC,
    Bool,
    Int,
    Program,
    Real,
    Rung,
    copy,
    latch,
    math,
    on_delay,
    out,
    rise,
    run_function,
)
from pyrung.core import SystemState, Timer, coast_run, compiled_run, read_trace, scan_trace
from pyrung.core.harness import Harness
from pyrung.core.physical import Physical
from pyrung.core.system_points import system
from tests import fold_check
from tests.fold_check import FoldMismatch


def _plc() -> PLC:
    n = Int("N")
    with Program(strict=False) as prog:
        with Rung():
            math(n + 1, n)
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


def test_the_twin_is_asked_on_the_plain_state() -> None:
    # A predicate that reads a traced state differently is judged as a user's would read it.
    plc = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="stop predicate true at scan 5, folded run stopped at 9"),
    ):
        fold_check._checked(
            "run_until",
            plc,
            _stepper(plc, 9),
            lambda s: type(s) is SystemState and type(s.tags) is PMap and s.tags["N"] >= 5,
        )


def test_a_predicate_naming_a_module_is_not_asked() -> None:
    # A module is shared with the run, so a twin's ask could move what the run reads.
    plc = _plc()
    before = fold_check.STATS["predicate skipped: not copyable"]
    fold_check._checked(
        "run_until",
        plc,
        _stepper(plc, 9),
        lambda s: threading.active_count() > 0 and s.tags["N"] >= 5,
    )
    assert fold_check.STATS["predicate skipped: not copyable"] == before + 1


def test_a_predicate_naming_a_timer_is_asked() -> None:
    timer = Timer.clone("AskedT")
    plc = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="stop predicate true at scan 5, folded run stopped at 9"),
    ):
        fold_check._checked(
            "run_until",
            plc,
            _stepper(plc, 9),
            lambda s: timer.Acc.name not in s.tags and s.tags["N"] >= 5,
        )


def test_a_trace_that_misreads_a_predicate_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reads no tag and answers False: the coast runs past a stop that holds for a while, then
    # goes on to its budget, so only a twin asked past the trace can say so.
    plc, acc = _timing_plc("MisreadT")
    monkeypatch.setattr(read_trace._TracedTags, "__getitem__", lambda _self, _key: False)

    def predicate(s: Any) -> bool:
        return s.tags[acc] == 20

    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="stop predicate true at scan 2000, folded run stopped"),
    ):
        fold_check._checked(
            "coast",
            plc,
            lambda: coast_run.coast_until(plc, predicate, budget=5_000),
            predicate,
            budget=5_000,
        )


def _timing_plc(name: str) -> tuple[PLC, str]:
    run = Bool("Run")
    timer = Timer.clone(name)
    with Program() as prog:
        with Rung(run):
            on_delay(timer, 30, "s")
    plc = PLC(prog, dt=0.01)
    plc.patch({run: True})
    plc.step()
    return plc, timer.Acc.name


def _opaque(pred: Any) -> SimpleNamespace:
    return SimpleNamespace(name="opaque", pred=pred, tags=(), condition=None)


def _seek(plc: PLC, *members: Any, budget: int = 5_000) -> None:
    """A real seek for a target that never comes, with these ``avoid=`` members armed."""
    import pyrung.core.analysis.pilot.coast as pilot_coast

    session = pilot_coast.CoastSession(plc)
    session.arm_avoid(SimpleNamespace(members=members))
    session.seek(
        [pilot_coast.predicate_trigger("target", pilot_coast.TARGET, lambda _s: False)],
        budget=budget,
    )


def test_an_opaque_avoid_is_let_off_between_the_scans_it_is_judged_on(installed) -> None:
    fold_check.install()
    plc, acc = _timing_plc("LetOffT")
    before = fold_check.STATS["predicate let off: true between the scans it is judged on"]
    _seek(plc, _opaque(lambda snap: snap.get(acc) == 20))
    assert plc.state.scan_id == 5_001
    assert (
        fold_check.STATS["predicate let off: true between the scans it is judged on"] == before + 1
    )


@pytest.mark.parametrize(
    "opaque_says",
    [
        pytest.param(lambda _acc: False, id="false"),
        pytest.param(lambda acc: acc is not None and acc >= 10, id="true first"),
    ],
)
def test_an_opaque_avoid_lets_no_other_member_off(
    installed, monkeypatch: pytest.MonkeyPatch, opaque_says: Any
) -> None:
    # The trace misreads, so the compiled member's stop is run past in a stretch.  The opaque
    # member beside it answers for itself only, whatever it says on that scan.
    fold_check.install()
    timer = Timer.clone("BesideT")
    run = Bool("Run")
    with Program() as prog:
        with Rung(run):
            on_delay(timer, 30, "s")
    plc = PLC(prog, dt=0.01)
    plc.patch({run: True})
    plc.step()
    acc = timer.Acc.name
    compiled = SimpleNamespace(
        name="compiled",
        pred=lambda snap: snap.get(acc) == 20,
        tags=(acc,),
        condition=timer.Acc == 20,
    )
    monkeypatch.setattr(
        read_trace._TracedTags,
        "__getitem__",
        lambda self, key: False if read_trace._READ_TRACES else self._untraced[key],
    )
    monkeypatch.setattr(
        read_trace._TracedTags, "__contains__", lambda self, key: key in self._untraced
    )
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="stop predicate true at scan 2000, folded run stopped"),
    ):
        _seek(plc, _opaque(lambda snap: opaque_says(snap.get(acc))), compiled)


def test_an_opaque_avoid_is_held_to_the_scans_the_coast_executes(installed, monkeypatch) -> None:
    def ignores_it(plc, _predicate, *, budget, on_kernel_scan, **_kwargs):
        for _ in range(budget):
            plc.step()
            on_kernel_scan(plc.state.scan_id)
        return False

    monkeypatch.setattr(installed, "coast_until", ignores_it)
    fold_check.install()
    plc = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="stop predicate true at scan 5, folded run stopped at 9"),
    ):
        _seek(plc, _opaque(lambda snap: snap.get("N", 0) >= 5), budget=9)


def test_a_coast_may_stop_short_on_an_opaque_avoid(installed) -> None:
    fold_check.install()
    plc = _plc()
    _seek(plc, _opaque(lambda snap: snap.get("N", 0) >= 5))
    assert plc.state.tags["N"] >= 5
    assert plc.state.scan_id < 5_000


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
    import pyrung.core.coast_run as coast_run

    for mod, name in (
        (coast_run, "coast_until"),
        (pilot_coast, "coast_until"),
        (pilot_coast.CoastSession, "arm_avoid"),
        (compiled_run, "run_scans"),
        (compiled_run, "INTERPRETED_PREFIX"),
        (scan_trace, "MIN_RUN_SCANS"),
        (scan_trace, "BURST_SIZES"),
        (scan_trace, "STRICT_SELF_CHECK"),
    ):
        monkeypatch.setattr(mod, name, getattr(mod, name))
    monkeypatch.setattr(compiled_run, "_fold_check_installed", False, raising=False)
    return coast_run


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
    coast_run = installed

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
    coast_run = installed

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


def test_a_function_held_in_an_object_is_copied_too() -> None:
    plc = _plc()
    k = [0]

    def count(_s) -> bool:
        k[0] += 1
        return k[0] >= 5

    holder = SimpleNamespace(steps=[count])
    fold_check._checked("run_until", plc, _stepper(plc, 5), lambda s: holder.steps[0](s))
    assert k == [0]


def _counting_step(k: list[int]) -> Any:
    def count(_self: Any, s: Any) -> bool:
        k[0] += 1
        return s.tags["N"] >= 5

    return count


def test_a_bound_method_of_a_closure_is_copied_too() -> None:
    plc = _plc()
    k = [0]
    method = types.MethodType(_counting_step(k), SimpleNamespace())
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="stop predicate true at scan 5, folded run stopped at 9"),
    ):
        fold_check._checked("run_until", plc, _stepper(plc, 9), method)
    assert k == [0]


def test_a_bound_builtin_is_bound_to_the_copy() -> None:
    plc = _plc()
    seen: list[Any] = []
    add = seen.append

    def predicate(s: Any) -> bool:
        add(s.scan_id)
        return s.tags["N"] >= 5

    fold_check._checked("run_until", plc, _stepper(plc, 5), predicate)
    assert seen == []


class _Slotted:
    __slots__ = ("step",)


def test_a_function_in_a_slot_is_copied_too() -> None:
    plc = _plc()
    k = [0]
    holder = _Slotted()
    holder.step = _counting_step(k)
    fold_check._checked("run_until", plc, _stepper(plc, 5), lambda s: holder.step(None, s))
    assert k == [0]


def test_a_cycle_through_a_tuple_and_a_list_is_kept() -> None:
    plc = _plc()
    ring: list[Any] = []
    link = (ring,)
    ring.append(link)

    def predicate(s: Any) -> bool:
        return link[0][0] is link and s.tags["N"] >= 5

    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="stop predicate true at scan 5, folded run stopped at 9"),
    ):
        fold_check._checked("run_until", plc, _stepper(plc, 9), predicate)


_BUMPS = 0


def _bump() -> None:
    global _BUMPS
    _BUMPS += 1


def _bumped(_s: Any) -> bool:
    _bump()
    return _BUMPS >= 1


def test_two_copied_functions_of_a_module_share_its_names() -> None:
    copied = fold_check._isolated(_bumped, _plc())
    assert copied is not None
    assert copied(None) is True
    assert _BUMPS == 0


class _CopiesItsOwnWay:
    """Copies itself through copy.deepcopy(), which hands back the function it holds."""

    def __init__(self, **parts: Any) -> None:
        self.__dict__.update(parts)

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        new = type(self).__new__(type(self))
        new.__dict__ = deepcopy(self.__dict__, memo)
        return new


class _CopiesWithoutTheMemo(_CopiesItsOwnWay):
    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        new = type(self).__new__(type(self))
        new.__dict__ = deepcopy(self.__dict__)
        return new


class _CopiesShallowly(_CopiesItsOwnWay):
    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        new = type(self).__new__(type(self))
        new.__dict__ = dict(self.__dict__)
        return new


class _SlotBesideADict:
    __slots__ = ("step", "__dict__")


def _in_a_slot_beside_a_dict(step: Any) -> Any:
    holder = _SlotBesideADict()
    holder.step = step
    return holder


# Boxes a weak reference points to, kept alive for the test that reads them.
_ALIVE: list[Any] = []


def _kept(obj: Any) -> Any:
    _ALIVE.append(obj)
    return obj


class _Box:
    def __init__(self, step: Any) -> None:
        self.step = step


def _behind_a_weak_reference(step: Any) -> Any:
    return weakref.ref(_kept(_Box(step)))


class _Counter:
    def __init__(self) -> None:
        self.n = 0

    def bump(self) -> int:
        self.n += 1
        return self.n


@pytest.mark.parametrize(
    "hide",
    [
        pytest.param(lambda step: collections.deque([step]), id="deque"),
        pytest.param(functools.partial, id="partial"),
        pytest.param(_in_a_slot_beside_a_dict, id="slot beside a dict"),
        pytest.param(_behind_a_weak_reference, id="behind a weak reference"),
        pytest.param(lambda step: [step, [object() for _ in range(25_000)]], id="past 20000"),
    ],
)
def test_an_own_deepcopy_that_keeps_a_function_anywhere_is_not_asked(hide: Any) -> None:
    holder = _CopiesItsOwnWay(part=hide(_counting_step([0])))
    assert fold_check._isolated(lambda _s: holder.part, _plc()) is None


@pytest.mark.parametrize(
    "part",
    [
        pytest.param(lambda: _Counter().bump, id="a method"),
        pytest.param(lambda: [0].append, id="a builtin"),
        pytest.param(lambda: weakref.ref(_kept(_Counter())), id="a weak reference"),
        pytest.param(lambda: weakref.proxy(_kept(_Counter())), id="a weak proxy"),
        pytest.param(lambda: types, id="a module"),
    ],
)
def test_an_own_copy_that_keeps_what_the_original_holds_is_not_asked(part: Any) -> None:
    holder = _CopiesShallowly(part=part())
    assert fold_check._isolated(lambda _s: holder.part, _plc()) is None


def test_an_own_deepcopy_of_a_method_binds_it_to_the_copy() -> None:
    counter = _Counter()
    holder = _CopiesItsOwnWay(bump=counter.bump)
    copied = fold_check._isolated(lambda _s: holder.bump() > 0, _plc())
    assert copied is not None
    assert copied(None) is True
    assert counter.n == 0


class _HoldsAWeakMethod:
    def __init__(self) -> None:
        self.counter = _Counter()
        self.bump = weakref.WeakMethod(self.counter.bump)

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        new = type(self).__new__(type(self))
        new.counter = deepcopy(self.counter, memo)
        new.bump = weakref.WeakMethod(new.counter.bump)
        return new


def test_an_own_deepcopy_may_keep_a_weak_method_of_its_copy() -> None:
    holder = _HoldsAWeakMethod()

    def predicate(_s: Any) -> bool:
        bump = holder.bump()
        return bump is not None and bump() > 0

    copied = fold_check._isolated(predicate, _plc())
    assert copied is not None
    assert copied(None) is True
    assert holder.counter.n == 0


def test_an_own_deepcopy_that_copies_the_runner_is_not_asked() -> None:
    plc = _plc()
    holder = _CopiesWithoutTheMemo(runners=collections.deque([plc]))
    assert fold_check._isolated(lambda _s: holder.runners[0], plc) is None


def test_an_own_deepcopy_that_keeps_the_function_is_not_asked() -> None:
    plc = _plc()
    k = [0]
    holder = _CopiesItsOwnWay(step=_counting_step(k))
    before = fold_check.STATS["predicate skipped: not copyable"]
    fold_check._checked("run_until", plc, _stepper(plc, 9), lambda s: holder.step(None, s))
    assert fold_check.STATS["predicate skipped: not copyable"] == before + 1
    assert k == [0]


def test_an_own_deepcopy_returning_the_callable_is_not_asked() -> None:
    plc = _plc()
    k = [0]

    @functools.cache
    def predicate(s: Any) -> bool:
        k[0] += 1
        return False

    before = fold_check.STATS["predicate skipped: not copyable"]
    fold_check._checked("run_until", plc, _stepper(plc, 5), predicate)
    assert fold_check.STATS["predicate skipped: not copyable"] == before + 1
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
            math(Cnt + 1, Cnt)
    plc = PLC(program, dt=0.01)
    plc.patch({"Go": True})
    return plc


def _short_of(scans: int):
    """_run_compiled that stops *scans* early and says it is done."""
    real = compiled_run._run_compiled

    def run(plc, kernel, budget, predicate, *rest):
        real(plc, kernel, budget - scans, predicate, *rest)
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


def test_a_run_charging_past_the_units_asked_is_caught() -> None:
    plc = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="asked for 5 units .* landed at 9 having charged 9"),
    ):
        fold_check._checked("coast", plc, _stepper(plc, 9), budget=5, loop_scans_per_unit=100)


def test_a_run_short_of_the_units_asked_is_caught() -> None:
    """A unit left over would have paid for 100 more scans, however few were run."""
    plc = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="100 of 104 scans left and the stop predicate false"),
    ):
        fold_check._checked(
            "coast", plc, _stepper(plc, 4), lambda _s: False, budget=5, loop_scans_per_unit=100
        )


def test_a_run_spending_every_unit_stopped_on_its_budget() -> None:
    plc = _plc()
    fold_check._checked(
        "coast", plc, _stepper(plc, 5), lambda _s: False, budget=5, loop_scans_per_unit=100
    )
    assert plc.state.scan_id == 5


def test_a_coast_in_units_through_install_is_held_to_the_coast_own_charge(installed) -> None:
    """The loop's scans come at their price, the rest a unit each, and every scan is compared."""
    coast_run = installed
    fold_check.install()
    plc = _timed()
    coast_run.coast_until(
        plc, lambda _s: False, budget=7, predicate_reads=frozenset(), loop_scans_per_unit=100
    )
    assert plc.state.scan_id > 7


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
            math(Cnt + 1, Cnt)
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

    def spurious(plc, kernel, budget, predicate, *rest):
        real(plc, kernel, budget // 2, predicate, *rest)
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

    def spurious(plc, kernel, budget, predicate, *rest):
        real(plc, kernel, budget // 2, predicate, *rest)
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


@pytest.mark.parametrize(
    "reset",
    [lambda plc: (plc.stop(), plc.start()), lambda plc: plc.reboot()],
    ids=["restart", "reboot"],
)
def test_own_twin_lets_off_a_reset_both_runners_took(reset) -> None:
    plc = _plc()
    twin = _plc()
    with fold_check.own_twin(plc) as compare:
        for runner in (plc, twin):
            runner.run(3)
            reset(runner)
            runner.run(2)
        compare(twin)


def test_own_twin_catches_a_reset_only_one_runner_took() -> None:
    plc = _plc()
    twin = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch),
        fold_check.own_twin(plc) as compare,
    ):
        plc.run(3)
        twin.run(3)
        plc.stop()
        plc.start()
        plc.run(2)
        twin.run(2)
        compare(twin)


def test_own_twin_catches_a_reset_only_the_twin_took() -> None:
    plc = _plc()
    twin = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch),
        fold_check.own_twin(plc) as compare,
    ):
        plc.run(3)
        twin.run(3)
        twin.stop()
        twin.start()
        plc.run(2)
        twin.run(2)
        compare(twin)


def test_own_twin_sees_a_base_the_run_left_off() -> None:
    plc = _plc()
    twin = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="the runner differs at scan 3: .*rtc base: folded="),
        fold_check.own_twin(plc) as compare,
    ):
        plc.run(3)
        plc._rtc_base += timedelta(hours=1)
        twin.run(3)
        compare(twin)


def test_own_twin_sees_a_base_the_stepped_runner_moved_and_the_run_did_not() -> None:
    plc = _plc()
    twin = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="the runner differs at scan 3: "),
        fold_check.own_twin(plc) as compare,
    ):
        plc.run(3)
        twin.set_rtc(datetime(2030, 1, 2, 3, 4, 5))
        twin.run(3)
        compare(twin)


def test_own_twin_lets_two_runners_start_at_their_own_times() -> None:
    plc = _plc()
    twin = _plc()
    twin._rtc_base = plc._rtc_base + timedelta(days=1)
    with fold_check.own_twin(plc) as compare:
        plc.run(3)
        twin.run(3)
        compare(twin)


def test_own_twin_holds_a_base_both_were_set_to() -> None:
    plc = _plc()
    twin = _plc()
    twin._rtc_base = plc._rtc_base + timedelta(days=1)
    when = datetime(2030, 1, 2, 3, 4, 5)
    with fold_check.own_twin(plc) as compare:
        for runner in (plc, twin):
            runner.run(1)
            runner.set_rtc(when)
            runner.run(2)
        compare(twin)
    assert plc._rtc_base == twin._rtc_base == when


def test_own_twin_sees_two_bases_set_apart() -> None:
    plc = _plc()
    twin = _plc()
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="the runner differs at scan 3: "),
        fold_check.own_twin(plc) as compare,
    ):
        for runner, hour in ((plc, 3), (twin, 4)):
            runner.run(1)
            runner.set_rtc(datetime(2030, 1, 2, hour, 4, 5))
            runner.run(2)
        compare(twin)


def test_own_twin_holds_a_base_both_were_set_to_before_the_block() -> None:
    plc = _plc()
    twin = _plc()
    twin._rtc_base = plc._rtc_base + timedelta(days=1)
    for runner in (plc, twin):
        runner.run(1)
        runner.set_rtc(datetime(2030, 1, 2, 3, 4, 5))
    with fold_check.own_twin(plc) as compare:
        plc.run(2)
        twin.run(2)
        compare(twin)


def test_own_twin_sees_two_bases_set_apart_before_the_block() -> None:
    plc = _plc()
    twin = _plc()
    for runner, hour in ((plc, 3), (twin, 4)):
        runner.run(1)
        runner.set_rtc(datetime(2030, 1, 2, hour, 4, 5))
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="the runner differs at scan 3: "),
        fold_check.own_twin(plc) as compare,
    ):
        plc.run(2)
        twin.run(2)
        compare(twin)


def _applies_the_time() -> PLC:
    with Program(strict=False) as program:
        with Rung(Go):
            copy(10, system.rtc.new_second)
            copy(5, system.rtc.new_minute)
            copy(1, system.rtc.new_hour)
            out(system.rtc.apply_time)
    return PLC(program)


def _applies_the_date() -> PLC:
    with Program(strict=False) as program:
        with Rung(Go):
            copy(2031, system.rtc.new_year4)
            copy(4, system.rtc.new_month)
            copy(2, system.rtc.new_day)
            out(system.rtc.apply_date)
    return PLC(program)


def _applies_both() -> PLC:
    with Program(strict=False) as program:
        with Rung(Go):
            copy(2031, system.rtc.new_year4)
            copy(4, system.rtc.new_month)
            copy(2, system.rtc.new_day)
            out(system.rtc.apply_date)
        with Rung(Go):
            copy(10, system.rtc.new_second)
            copy(5, system.rtc.new_minute)
            copy(1, system.rtc.new_hour)
            out(system.rtc.apply_time)
    return PLC(program)


_ON_EITHER_BASE = pytest.mark.parametrize(
    "applies", [_applies_the_time, _applies_the_date, _applies_both], ids=["time", "date", "both"]
)


def _two_bases_apart(applies) -> tuple[PLC, PLC]:
    """Two runners, each base with its own date, time of day and microsecond."""
    plc, twin = applies(), applies()
    plc._rtc_base = datetime(2030, 1, 2, 3, 4, 5, 111)
    twin._rtc_base = datetime(2030, 2, 3, 13, 14, 15, 222)
    return plc, twin


def _apply_on_both(plc: PLC, twin: PLC) -> None:
    for runner in (plc, twin):
        runner.run(1)
        runner.patch({"Go": True})
        runner.run(2)


@_ON_EITHER_BASE
def test_own_twin_holds_what_a_rung_applied_on_each_base(applies) -> None:
    plc, twin = _two_bases_apart(applies)
    with fold_check.own_twin(plc) as compare:
        _apply_on_both(plc, twin)
        compare(twin)
    assert plc._rtc_base != twin._rtc_base


@pytest.mark.parametrize(
    "off", [timedelta(hours=1), timedelta(days=1), timedelta(microseconds=1)], ids=str
)
@_ON_EITHER_BASE
def test_own_twin_sees_an_apply_that_set_something_else(applies, off) -> None:
    """The rung ran the same on both, so only the log and the base can show it."""
    plc, twin = _two_bases_apart(applies)
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="the runner differs at scan 3: .*log: rtc: "),
        fold_check.own_twin(plc) as compare,
    ):
        _apply_on_both(plc, twin)
        log = plc._scan_log._rtc_base_changes
        scan = max(log)
        log[scan] = (log[scan][0] + off, log[scan][1])
        plc._rtc_base += off
        compare(twin)


def test_own_twin_sees_a_base_the_run_left_off_after_an_apply() -> None:
    plc, twin = _two_bases_apart(_applies_the_time)
    with (
        fold_check.deliberate(),
        pytest.raises(FoldMismatch, match="the runner differs at scan 3: .*rtc base: folded="),
        fold_check.own_twin(plc) as compare,
    ):
        _apply_on_both(plc, twin)
        plc._rtc_base += timedelta(hours=1)
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
                "test_own_twin_lets_off_a_reset_both_runners_took",
                "test_own_twin_catches_a_reset_only_one_runner_took",
                "test_own_twin_catches_a_reset_only_the_twin_took",
                "test_own_twin_sees_a_base_the_run_left_off",
                "test_own_twin_sees_a_base_the_stepped_runner_moved_and_the_run_did_not",
                "test_own_twin_lets_two_runners_start_at_their_own_times",
                "test_own_twin_holds_a_base_both_were_set_to",
                "test_own_twin_sees_two_bases_set_apart",
                "test_own_twin_holds_a_base_both_were_set_to_before_the_block",
                "test_own_twin_sees_two_bases_set_apart_before_the_block",
                "test_own_twin_holds_what_a_rung_applied_on_each_base",
                "test_own_twin_sees_an_apply_that_set_something_else",
                "test_own_twin_sees_a_base_the_run_left_off_after_an_apply",
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


def test_deliberate_closes_on_a_test_failing_inside_it() -> None:
    # pytest's failure is a BaseException: a block left open would swallow the next mismatch.
    before = len(fold_check.MISMATCHES)
    with pytest.raises(pytest.fail.Exception):
        with fold_check.deliberate():
            pytest.fail("the test's own failure")
    assert fold_check._open_deliberate_blocks() == []
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
            math(n + 1, n)
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
    coast_run = installed
    names = ("ENTRY_WAIT", "STRETCH_CHECKPOINT_INTERVALS", "MAX_CHECKPOINTS", "MAX_RECORD_RUNS")
    before = [getattr(coast_run, name) for name in names]
    fold_check.install()
    assert [getattr(coast_run, name) for name in names] == before


def test_a_real_thinned_coast_passes(installed, monkeypatch) -> None:
    coast_run = installed
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
        coast_run = installed
        monkeypatch.setattr(coast_run, "MAX_RECORD_RUNS", 5)
        fold_check.install()
        plc = _timed()
        before = fold_check.STATS[self.SETTLED]
        coast_run.coast_until(plc, lambda _s: False, budget=5000)
        assert plc._unrecorded_scans, "a later question still has to find them left out"
        left_out = sum(high - low + 1 for low, high in plc._unrecorded_scans)
        assert fold_check.STATS[self.SETTLED] - before == left_out

    def test_a_wrong_answer_for_them_is_caught(self, installed, monkeypatch) -> None:
        coast_run = installed
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
        coast_run = installed
        fold_check.install()
        plc = _timed()
        memo = coast_run.memo_of(plc)
        memo.work_left = lambda: 5
        # A program with no kernel pays a unit a scan, so the ledger stops it.
        monkeypatch.setattr(coast_run, "_kernel_for", lambda *_a: None)
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
        fold_check._hold_to_the_ledger("coast", 5, {"unit_scans": 5, "out_of_work": 1})
        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="stopped for its ledger"):
            fold_check._hold_to_the_ledger("coast", 5, {"unit_scans": 4, "out_of_work": 1})
        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="stopped for its ledger"):
            fold_check._hold_to_the_ledger("coast", None, {"unit_scans": 4, "out_of_work": 1})

    def test_a_coast_past_its_ledger_is_caught(self) -> None:
        fold_check._hold_to_the_ledger("coast", 5, {"unit_scans": 6})  # its landing
        fold_check._hold_to_the_ledger("coast", None, {"unit_scans": 700})  # no search
        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="with 5 left"):
            fold_check._hold_to_the_ledger("coast", 5, {"unit_scans": 7})

    def test_a_raised_coast_says_nothing_about_it(self) -> None:
        fold_check._hold_to_the_ledger("coast", 5, {})
        assert fold_check._priced_by_the_rule("coast", _timed(), 50, 0, None) == 0

    def test_its_price_is_held_to_how_it_ran(self) -> None:
        priced = fold_check._priced_by_the_rule
        no_kernel = _timed()
        no_kernel._compiled_replay_kernel = False
        assert priced("coast", no_kernel, 50, 0, 50) == 50
        assert priced("coast", no_kernel, 2, 0, 2) == 2
        with_kernel = _timed()
        assert with_kernel._compiled_replay_supported_kernel() is not None
        assert priced("coast", with_kernel, 50, 40, 3) == 3
        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="charged 4 of 50"):
            priced("coast", with_kernel, 50, 0, 4)
        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="paid by the scan"):
            priced("coast", with_kernel, 50, 40, 50)
        with fold_check.deliberate(), pytest.raises(FoldMismatch, match="with no kernel"):
            priced("coast", no_kernel, 50, 0, 3)


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
    temp = Real(
        "Temp", physical=Physical("TempSensor", approach_to=100.0, approach_rate=5.0), link="Go"
    )
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
    # Not the hour it applies, whatever the time is; a set_rtc() would be in the log itself.
    plc._set_rtc_internal(datetime(2030, 1, 2, 9, 0, 0), plc.state.timestamp)
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
