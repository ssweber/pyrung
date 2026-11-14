"""A run_until condition's reads, and the landing on the first scan it can flip.

``_extract_condition_reads(condition)`` collects every tag a ``run_until`` condition reads
(bare contacts, edges, both sides of a comparison).  Past that, every program here is run
twice from the same fork, in the guarded loop and stepped, and must land on the same scan
with the same tags: a tag only the condition reads, a system clock no rung reads, a signal
that changes every scan.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from pyrung import (
    Block,
    Bool,
    Counter,
    Int,
    Program,
    Rung,
    TagType,
    Timer,
    copy,
    count_up,
    latch,
    math,
    on_delay,
    out,
)
from pyrung.core import And, Or, fall, rise, system
from pyrung.core.analysis.pilot.coast import (
    TARGET,
    CoastSession,
    CoastTrigger,
    _trigger_reads,
)
from pyrung.core.condition import _as_condition
from pyrung.core.read_trace import _exact_condition_reads, _extract_condition_reads
from pyrung.core.runner import PLC

pytestmark = pytest.mark.usefixtures("loop_from_first_scan")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolved(plc: PLC, name: str) -> object:
    """Read a resolved-on-read system point (clocks, scan_counter) that is not
    stored in ``state.tags``."""
    _found, value = plc._system_runtime.resolve(name, plc.state)
    return value


def _assert_landing_parity(folded: PLC, stepped: PLC) -> None:
    """The loop and stepping landed on the same scan with identical tag values."""
    assert folded.state.scan_id == stepped.state.scan_id
    assert dict(folded.state.tags) == dict(stepped.state.tags)
    assert folded.state.timestamp == stepped.state.timestamp


def _fold_nofold(
    prog: Program,
    condition: object,
    *,
    patch: dict[str, object] | None = None,
    presteps: int = 1,
    max_cycles: int = 40_000,
) -> tuple[PLC, PLC]:
    """Run the SAME run_until from two identical forks: one in the loop, one stepped."""
    base = PLC(prog, dt=0.010)
    if patch:
        base.patch(patch)
    for _ in range(presteps):
        base.step()

    folded = base.fork()
    stepped = base.fork()
    folded.run_until(condition, max_cycles=max_cycles, jit=True)
    stepped.run_until(condition, max_cycles=max_cycles, jit=False)
    return folded, stepped


# ---------------------------------------------------------------------------
# 1. _extract_condition_reads — pure collection over the Atom vocabulary
# ---------------------------------------------------------------------------


class TestExtractConditionReads:
    """`_extract_condition_reads` returns every tag name a condition reads,
    regardless of contact form, so all of them become protected reads."""

    def test_bare_bool_contact(self) -> None:
        assert _extract_condition_reads(_as_condition(Bool("A"))) == frozenset({"A"})

    def test_normally_closed_contact(self) -> None:
        # ~tag → NormallyClosedCondition (xio), still a read of A.
        assert _extract_condition_reads(~Bool("A")) == frozenset({"A"})

    def test_rising_edge(self) -> None:
        assert _extract_condition_reads(rise(Bool("A"))) == frozenset({"A"})

    def test_falling_edge(self) -> None:
        assert _extract_condition_reads(fall(Bool("A"))) == frozenset({"A"})

    def test_comparison_against_literal_collects_left_only(self) -> None:
        # `A >= 5`: the literal 5 is not a tag, so only A is collected.
        assert _extract_condition_reads(Int("A") >= 5) == frozenset({"A"})

    def test_tag_to_tag_comparison_collects_both_sides(self) -> None:
        # `A >= B`: the right operand is a (writable) tag → its name is a read.
        assert _extract_condition_reads(Int("A") >= Int("B")) == frozenset({"A", "B"})

    def test_and_tree_collects_all(self) -> None:
        cond = And(Bool("A"), Int("B") >= 5)
        assert _extract_condition_reads(cond) == frozenset({"A", "B"})

    def test_or_tree_collects_all(self) -> None:
        # Or() is an AnyCondition — both disjuncts contribute their reads.
        cond = Or(Bool("A"), Bool("C"))
        assert _extract_condition_reads(cond) == frozenset({"A", "C"})

    def test_nested_groups_collect_every_leaf(self) -> None:
        cond = And(Bool("A"), Or(Bool("B"), Int("C") >= Int("D")))
        assert _extract_condition_reads(cond) == frozenset({"A", "B", "C", "D"})

    def test_single_condition_normalized_to_all(self) -> None:
        # A one-term And normalizes to the bare term; still a single read.
        assert _extract_condition_reads(_as_condition(Int("A") > 0)) == frozenset({"A"})

    # NOTE: `ArithAtom` (compound `(A + B) > k`) is handled defensively in
    # `_extract_condition_reads`, but `_condition_to_expr` never emits one from
    # a `run_until` Condition (ArithAtoms are minted only in analysis/graph.py),
    # so there is no Condition-level input that exercises that branch here.


# ---------------------------------------------------------------------------
# 2. First-bump parity
# ---------------------------------------------------------------------------


def _bool_flip_program(preset_ms: int = 200) -> tuple[Program, Bool]:
    """An ordinary written Bool that flips true when a timer completes."""
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    Done = Bool("Done")
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, preset_ms, "ms")
        with Rung(Tmr.Done):
            out(Done)
    return prog, Done


def _clock_counter_program(preset: int = 100) -> tuple[Program, Counter]:
    """A counter counting the rises of ``sys.clock_100ms`` while Enable is on.

    A counter counts rising edges, so under a held Enable it is the clock that
    advances it: one count per 100 ms.
    """
    Enable = Bool("Enable", external=True)
    Ctr = Counter.clone("Ctr")
    Reset = Bool("Reset", external=True)
    with Program(strict=False) as prog:
        with Rung(Enable, system.sys.clock_100ms):
            count_up(Ctr, preset).reset(Reset)
    return prog, Ctr


class TestFirstBumpParity:
    """Folded and unfolded run_until land on the same first scan the armed
    condition changes, with identical tag values."""

    def test_ordinary_bool_after_timer(self) -> None:
        # (a) a written Bool flips after a timer completes.
        prog, done = _bool_flip_program(200)
        folded, stepped = _fold_nofold(prog, done, patch={"Enable": True})
        assert folded.state.tags["Done"] is True
        _assert_landing_parity(folded, stepped)

    def test_counter_accumulator_ge_threshold(self) -> None:
        # (b) a counter counting clock edges, with `Acc >= N`.
        prog, ctr = _clock_counter_program(100)
        folded, stepped = _fold_nofold(prog, ctr.Acc >= 30, patch={"Enable": True})
        assert folded.state.tags["Ctr_Acc"] == 30
        _assert_landing_parity(folded, stepped)

    def test_counter_accumulator_eq_exact_landing(self) -> None:
        # (c) `Acc == N` on a counter counting clock edges — exact landing.
        prog, ctr = _clock_counter_program(100)
        folded, stepped = _fold_nofold(prog, ctr.Acc == 50, patch={"Enable": True})
        assert folded.state.tags["Ctr_Acc"] == 50
        _assert_landing_parity(folded, stepped)

    def test_rise_edge_on_written_tag(self) -> None:
        # (d) rise() edge on an ordinary written tag (Done flips at timer done).
        prog, done = _bool_flip_program(200)
        folded, stepped = _fold_nofold(prog, rise(done), patch={"Enable": True})
        assert folded.state.tags["Done"] is True
        _assert_landing_parity(folded, stepped)


# ---------------------------------------------------------------------------
# 3. Protected churn-excluded read — the headline case
# ---------------------------------------------------------------------------


def _churn_plus_timer_program(preset_ms: int = 100_000) -> Program:
    """A self-incrementing churn tag X (only reader is its own writer rung, the
    rung writes nothing else, affine self-referential calc) PLUS an unrelated
    timer so the fold has a plateau to fold.

    Unprotected, X moves by a constant the scan loop checks: one unconditional
    ``math(X + 1, X)`` into an INT."""
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    X = Int("X")
    with Program() as prog:
        with Rung():
            math(X + 1, X)  # X := X + 1, unconditional, self-referential
        with Rung(Enable):
            on_delay(Tmr, preset_ms, "ms")
    return prog


class TestChurnedTagInTheCondition:
    """A tag changing every scan, read by the run_until condition: the run must stop on the
    first scan it crosses, not at the timer preset."""

    def test_run_until_lands_on_exact_first_crossing(self) -> None:
        prog = _churn_plus_timer_program()
        X = Int("X")
        folded, stepped = _fold_nofold(prog, X >= 30, patch={"Enable": True})
        assert folded.state.tags["X"] == 30
        _assert_landing_parity(folded, stepped)


# ---------------------------------------------------------------------------
# 4. An output nothing reads
# ---------------------------------------------------------------------------


class TestUnreadOutputs:
    def test_unread_unconditionally_redefined_output_matches_stepping(self) -> None:
        Terminal = Int("Terminal")
        Gate = Bool("Gate", external=True)
        with Program() as program:
            with Rung():
                math(1, Terminal)
            with Rung(Gate):
                math(2, Terminal)

        folded, stepped = _fold_nofold(program, Bool("Never"), max_cycles=50)
        _assert_landing_parity(folded, stepped)

    def test_unread_conditional_latch_matches_stepping(self) -> None:
        Terminal = Int("Terminal")
        Gate = Bool("Gate", external=True)
        with Program() as program:
            with Rung(Gate):
                math(2, Terminal)

        folded, stepped = _fold_nofold(program, Bool("Never"), patch={"Gate": True}, max_cycles=50)
        assert folded.state.tags["Terminal"] == 2
        _assert_landing_parity(folded, stepped)

    def test_output_the_condition_reads_lands_with_it(self) -> None:
        Terminal = Int("Terminal")
        with Program() as program:
            with Rung():
                math(1, Terminal)

        folded, stepped = _fold_nofold(program, Terminal == 1)
        assert folded.state.tags["Terminal"] == 1
        _assert_landing_parity(folded, stepped)


# ---------------------------------------------------------------------------
# 5. Condition-read system clock (no rung reads it)
# ---------------------------------------------------------------------------


def _timer_no_clock_program(preset_ms: int = 5_000) -> Program:
    """A foldable timer.  No rung reads any system clock — the clock bound must
    come purely from the run_until condition."""
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, preset_ms, "ms")
    return prog


class TestConditionReadSystemClock:
    """A system clock read only by the run_until condition: the run stops on its edge, though
    the program's rungs never read it."""

    def test_condition_clock_lands_same_scan_as_nofold(self) -> None:
        prog = _timer_no_clock_program()
        folded, stepped = _fold_nofold(prog, system.sys.clock_1s, patch={"Enable": True})
        # clock_1s first reads high at its half-period edge (0.5 s).
        assert _resolved(folded, system.sys.clock_1s.name) is True
        _assert_landing_parity(folded, stepped)


# ---------------------------------------------------------------------------
# 6. A signal in the condition that changes every scan
# ---------------------------------------------------------------------------


class TestScanDerivedInCondition:
    def test_scan_counter_condition_lands_exactly(self) -> None:
        prog = _timer_no_clock_program(preset_ms=100_000)
        folded, stepped = _fold_nofold(prog, system.sys.scan_counter == 40, patch={"Enable": True})
        assert _resolved(folded, system.sys.scan_counter.name) == 40
        _assert_landing_parity(folded, stepped)


# ---------------------------------------------------------------------------
# 7. Timed accumulator `==` hazard (documented)
# ---------------------------------------------------------------------------


def _timed_timer_program(preset_ms: int = 100_000) -> tuple[Program, Timer]:
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, preset_ms, "ms")
    return prog, Tmr


class TestTimedAccumulatorEqualityHazard:
    """A timed (ms-based) accumulator advances by one dt per scan.  `>=` lands on the first
    scan Acc crosses the target; `==` lands only on a whole multiple of the increment."""

    def test_ge_on_timed_accumulator_lands_exactly(self) -> None:
        prog, tmr = _timed_timer_program()
        # dt=0.010 s → +10 ms/scan; `>= 250` first holds at Acc == 250.
        folded, stepped = _fold_nofold(prog, tmr.Acc >= 250, patch={"Enable": True})
        assert folded.state.tags["Tmr_Acc"] == 250
        _assert_landing_parity(folded, stepped)

    def test_eq_on_reachable_whole_multiple_lands(self) -> None:
        # 500 is a whole multiple of the 10 ms per-scan increment, so no dt steps over it.
        prog, tmr = _timed_timer_program()
        folded, stepped = _fold_nofold(prog, tmr.Acc == 500, patch={"Enable": True})
        assert folded.state.tags["Tmr_Acc"] == 500
        _assert_landing_parity(folded, stepped)


# ---------------------------------------------------------------------------
# A coast trigger: its stop is where stepping would stop
# ---------------------------------------------------------------------------

_CoastT = Timer.clone("CoastT")
_Mirror = Int("CoastMirror")
_Bank = Block("CoastBank", TagType.INT, 1, 4)
_Idx = Int("CoastIdx", default=2)
_Clock = system.sys.clock_1s


def _coast_plc() -> PLC:
    """Nothing here stops the loop before scan 69 but a trigger's own reads."""
    with Program(strict=False) as program:
        with Rung():
            on_delay(_CoastT, preset=7350)
        with Rung():
            copy(_CoastT.Acc, _Mirror)
        with Rung(_CoastT.Acc >= 700):
            latch(Bool("CoastLate"))
        with Rung():
            copy(_CoastT.Acc, _Bank[2])
    plc = PLC(program, dt=0.010)
    plc.set_rtc(datetime(2026, 1, 1))
    plc.step()
    return plc


@pytest.mark.parametrize(
    "condition",
    [
        _Clock,
        ~_Clock,
        _Mirror,
        _Mirror >= 300,
        _Bank[_Idx] >= 300,
        _Mirror >= _Bank[_Idx],
        _Mirror + 1 >= 300,
        Or(_Bank[_Idx] >= 300, _Mirror >= 400),
        And(_Clock, _Mirror >= 1),
    ],
    ids=[
        "bare-clock",
        "negated-clock",
        "bare-int",
        "compare",
        "indirect-compare",
        "indirect-operand",
        "expression-compare",
        "or-with-indirect",
        "and-with-clock",
    ],
)
def test_a_condition_trigger_stops_where_stepping_does(condition: object) -> None:
    probe = _coast_plc()
    predicate = probe._compile_condition_predicate(condition, method="run_until")

    stepped = _coast_plc()
    for _ in range(1_500):
        stepped.step()
        if predicate(stepped.state):
            break
    assert predicate(stepped.state), "the condition never comes true"

    coasted = _coast_plc()
    result = CoastSession(coasted, kind="t").seek(
        [CoastTrigger("t", TARGET, predicate, condition=condition)], budget=1_500
    )

    assert result.stop_reason == "reached"
    _assert_landing_parity(coasted, stepped)


_CoastHeld = Int("CoastHeld")
_CoastLimit = Int("CoastLimit", default=1)
_CoastOther = Bool("CoastOther")
_CoastLate = Bool("CoastLate")


def _quiet_until_late_plc() -> PLC:
    """A long timer runs; what the conditions below read holds still until scan 70."""
    with Program(strict=False) as program:
        with Rung():
            on_delay(_CoastT, preset=7350)
        with Rung(_CoastT.Acc >= 700):
            latch(_CoastLate)
            copy(1, _CoastHeld)
        with Rung(_CoastOther, _CoastHeld == _CoastLimit, _CoastLimit == 9):
            out(Bool("CoastSeen"))
    plc = PLC(program, dt=0.010)
    plc.step()
    return plc


@pytest.mark.parametrize(
    ("condition", "reads"),
    [
        (_CoastLate, {"CoastLate"}),
        (_CoastHeld, {"CoastHeld"}),
        (_CoastHeld >= 1, {"CoastHeld"}),
        (_CoastHeld >= _CoastLimit, {"CoastHeld", "CoastLimit"}),
        (And(_CoastLate, ~_CoastOther), {"CoastLate", "CoastOther"}),
        (Or(_CoastLate, _CoastOther), {"CoastLate", "CoastOther"}),
    ],
    ids=["bare-bool", "bare-int", "compare", "tag-operand", "and", "or"],
)
def test_an_exact_condition_stretches_on_what_it_names(
    condition: object, reads: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming its reads spares a trace: this is the reason to name them at all."""

    def _no_trace(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an exact condition does not need tracing")

    monkeypatch.setattr("pyrung.core.coast_run._traced_call", _no_trace)
    predicate = _quiet_until_late_plc()._compile_condition_predicate(condition, method="run_until")
    trigger = CoastTrigger("t", TARGET, predicate, condition=condition)
    assert _trigger_reads([trigger]) == reads

    stepped = _quiet_until_late_plc()
    for _ in range(1_500):
        stepped.step()
        if predicate(stepped.state):
            break

    coasted = _quiet_until_late_plc()
    session = CoastSession(coasted, kind="t")
    result = session.seek([trigger], budget=1_500)

    assert result.stop_reason == "reached"
    assert session._last_coast_stats["stretches"] >= 1
    _assert_landing_parity(coasted, stepped)


class TestExactConditionReads:
    def test_bare_tags_read_themselves(self) -> None:
        contact, word = Bool("A"), Int("W")
        assert _extract_condition_reads(contact) == {"A"}
        assert _extract_condition_reads(word) == {"W"}
        assert _exact_condition_reads(contact) == {"A"}
        assert _exact_condition_reads(word) == {"W"}

    def test_a_named_condition_is_exact(self) -> None:
        a, b, w = Bool("A"), Bool("B"), Int("W")
        assert _exact_condition_reads(And(a, Or(~b, rise(a)), w >= 3, w == Int("V"))) == {
            "A",
            "B",
            "W",
            "V",
        }

    @pytest.mark.parametrize(
        "condition",
        [
            _Bank[_Idx] >= 1,
            _Mirror >= _Bank[_Idx],
            _Mirror + 1 >= 1,
            _Mirror >= Int("Fixed", readonly=True, default=5),
            Or(Bool("A"), _Bank[_Idx] == 1),
            And(Bool("A"), _Mirror + 1 >= 1),
        ],
        ids=["indirect", "indirect-operand", "expression", "readonly-operand", "or", "and"],
    )
    def test_a_condition_hiding_a_read_will_not_say(self, condition: object) -> None:
        assert _exact_condition_reads(condition) is None
