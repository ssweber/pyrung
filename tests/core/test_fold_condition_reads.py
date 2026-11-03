"""A run_until condition's reads, and the landing on the first scan it can flip.

``_extract_condition_reads(condition)`` collects every tag a ``run_until`` condition reads
(bare contacts, edges, both sides of a comparison).  Past that, every program here is run
twice from the same fork, in the guarded loop and stepped, and must land on the same scan
with the same tags: a tag only the condition reads, a system clock no rung reads, a signal
that changes every scan.
"""

from __future__ import annotations

import pytest

from pyrung import Bool, Counter, Int, Program, Rung, Timer, calc, count_up, on_delay, out
from pyrung.core import And, Or, fall, rise, system
from pyrung.core.condition import _as_condition
from pyrung.core.read_trace import _extract_condition_reads
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

    Unprotected, X is an exact self-calc fold source (``_selfcalc_sources``):
    one unconditional ``calc(X + 1, X)`` into an INT."""
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    X = Int("X")
    with Program() as prog:
        with Rung():
            calc(X + 1, X)  # X := X + 1, unconditional, self-referential
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
                calc(1, Terminal)
            with Rung(Gate):
                calc(2, Terminal)

        folded, stepped = _fold_nofold(program, Bool("Never"), max_cycles=50)
        _assert_landing_parity(folded, stepped)

    def test_unread_conditional_latch_matches_stepping(self) -> None:
        Terminal = Int("Terminal")
        Gate = Bool("Gate", external=True)
        with Program() as program:
            with Rung(Gate):
                calc(2, Terminal)

        folded, stepped = _fold_nofold(program, Bool("Never"), patch={"Gate": True}, max_cycles=50)
        assert folded.state.tags["Terminal"] == 2
        _assert_landing_parity(folded, stepped)

    def test_output_the_condition_reads_lands_with_it(self) -> None:
        Terminal = Int("Terminal")
        with Program() as program:
            with Rung():
                calc(1, Terminal)

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
