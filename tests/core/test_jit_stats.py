"""The jit counters: off unless asked, and exact on small programs whose runs are known."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pyrung import PLC, Bool, Int, Program, Rung, Timer, copy, on_delay
from pyrung.core import compiled_run, jit_stats, scan_trace
from pyrung.core.coast_run import coast_until

T = Timer.clone("JsT")
Go = Bool("JsGo")
N = Int("JsN")
# Under --fold-check each run's twin compiles a kernel of its own.
COMPILES = 2 if getattr(compiled_run, "_fold_check_installed", False) else 1


@pytest.fixture(autouse=True)
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    """As a user runs: an interpreted prefix, then the loop with no firing limit."""
    monkeypatch.setattr(compiled_run, "INTERPRETED_PREFIX", 1000)
    monkeypatch.setattr(scan_trace, "MIN_RUN_SCANS", 1000)
    monkeypatch.setattr(scan_trace, "BURST_SIZES", None)


def _timer(preset_ms: int) -> PLC:
    """A timer that runs while Go is on, and N set to 5 once it is done."""
    with Program(strict=False) as program:
        with Rung(Go):
            on_delay(T, preset_ms, "ms")
        with Rung(T.Done):
            copy(5, N)
    plc = PLC(program, dt=0.01)
    plc.patch({Go.name: True})
    plc.step()
    return plc


def test_nothing_counts_while_no_one_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    assert jit_stats.ACTIVE == []

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("counted while off")

    monkeypatch.setattr(jit_stats, "Run", refused)
    monkeypatch.setattr(jit_stats, "add_cost", refused)
    plc = _timer(3000)
    plc.run_for(30)
    coast_until(plc, lambda _s: False, budget=500, predicate_reads=frozenset())
    assert plc.state.tags[N.name] == 5


def test_a_run_past_its_prefix_runs_in_the_loop_to_its_end() -> None:
    plc = _timer(3000)
    with jit_stats.collect() as stats:
        plc.run_for(30)  # 3000 scans
    assert stats.calls == {"run_for()/run_until()": 1}
    # The prefix, the trace's checking scan, then the loop to the run's end.
    assert stats.scans == {"interpreted": 1000, "compiled": 1, "loop": 1999}
    assert (stats.stretches, stats.stretched_scans, stats.could_have_run) == (1, 1999, 1999)
    assert stats.left_by == {"the run's end": 1}
    assert {name: count for name, (count, _seconds) in stats.costs.items()} == {
        "compile": COMPILES,
        "trace": 1,
    }
    assert stats.events == {"traces": 1}
    assert stats.declines == {}


def test_a_guard_that_fails_ends_its_stretch_and_is_named() -> None:
    plc = _timer(20_000)  # done after 2000 scans
    with jit_stats.collect() as stats:
        plc.run_for(40)  # 4000 scans
    assert plc.state.tags[N.name] == 5
    # The first path runs until the timer is done, and a guard sends it out.  The scan it
    # refused runs compiled, the next is traced and checked, and a second path runs to the end.
    assert stats.scans == {"interpreted": 1000, "compiled": 3, "loop": 2997}
    assert stats.stretches == 2
    assert stats.stretched_scans == stats.could_have_run == 2997
    (guard,) = (key for key in stats.left_by if key.startswith("a guard at "))
    assert guard.startswith("a guard at _kernel_step:")
    assert "_t_JsT_Done" in guard
    assert stats.left_by == {guard: 1, "the run's end": 1}
    assert guard in stats.report()


def test_a_short_run_says_why_it_stayed_interpreted() -> None:
    plc = _timer(3000)
    with jit_stats.collect() as stats:
        plc.run_for(1)
        plc.run_until(lambda _state: False, max_cycles=1500)
    assert stats.scans == {"interpreted": 1600}
    assert stats.declines == {
        "interpreted: ended in the interpreted prefix": 1,
        "interpreted: a callable stop predicate": 1,
    }
    assert stats.stretches == 0


def test_a_coast_counts_its_stretch_and_its_checking_scans() -> None:
    plc = _timer(3000)
    with jit_stats.collect() as stats:
        coast_until(plc, lambda s: s.tags[N.name] == 5, budget=10_000, predicate_reads=frozenset())
    assert plc.state.scan_id == 300
    assert stats.calls == {"coast_until()": 1}
    assert stats.scans["loop"] + stats.scans["interpreted"] == 299
    assert stats.scans["self check"] == 2
    assert stats.stretches == 1
    ((left_by, _one),) = stats.left_by.items()
    assert left_by.startswith("a guard at _kernel_step:") and "_t_JsT_Done" in left_by
    assert stats.costs["compile"][0] == COMPILES
    assert stats.costs["trace"][0] == 2


def test_each_collector_counts_its_own_block() -> None:
    plc = _timer(3000)
    with jit_stats.collect() as outer:
        plc.run_for(1)
        with jit_stats.collect() as inner:
            plc.run_for(1)
    assert outer.scans == {"interpreted": 200}
    assert inner.scans == {"interpreted": 100}
    assert jit_stats.ACTIVE == []


def test_a_stretch_could_have_run_to_the_next_failed_guard() -> None:
    with jit_stats.collect() as stats:
        run = jit_stats.Run("test")
        run.stretch(10, 5, "its scan limit", guard_failed=False)
        run.stretch(20, 5, "a guard", guard_failed=True)
        run.stretch(30, 3, "its scan limit", guard_failed=False)
        run.end(40)
    assert stats.stretches == 3
    assert stats.stretched_scans == 13
    # 15 to the guard at scan 25, 5 for the stretch the guard ended, 10 to the run's end.
    assert stats.could_have_run == 15 + 5 + 10
    assert stats.by_length == {4: [2, 8, 25], 3: [1, 5, 5]}


def test_the_report_is_ascii_and_holds_every_section() -> None:
    plc = _timer(20_000)
    with jit_stats.collect() as stats:
        plc.run_for(1)
        plc.run_for(40)
    text = stats.report()
    assert text.isascii()
    for heading in ("calls:", "scans:", "stretches:", "stretches ended by:", "declines:", "costs:"):
        assert heading in text


def test_the_environment_collects_a_whole_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registered: list[tuple[Any, ...]] = []
    monkeypatch.setattr(jit_stats.atexit, "register", lambda *args: registered.append(args))
    monkeypatch.setattr(jit_stats, "ACTIVE", [])
    target = tmp_path / "jit.txt"
    monkeypatch.setenv("PYRUNG_JIT_STATS", str(target))
    jit_stats._from_the_environment()
    ((dump, stats, where),) = registered
    assert jit_stats.ACTIVE == [stats]
    _timer(3000).run_for(1)
    dump(stats, where)
    dump(stats, where)
    text = target.read_text(encoding="utf-8")
    assert text.count("pyrung jit counters, process") == 2
    assert "100 interpreted" in text
