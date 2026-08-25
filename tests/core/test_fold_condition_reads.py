"""Tests for CoastSession phase 1 — run_until condition reads are fold-protected.

Two seams land here (see scratchpad/coastsession/DESIGN.md Part 1):

- ``fold._extract_condition_reads(condition)`` collects *every* tag a
  ``run_until`` condition reads (bare contacts, edges, both sides of a
  comparison), so the caller can thread them into the fold context.
- ``runner.run_until`` (Tag/Condition path only) splits those reads into
  system clocks (bounded as hard clock edges), scan-derived signals (fold
  disabled), and ordinary tags (protected reads → ``target_names``), then
  keyed-caches the resulting fold context per protected-read triple.

The net effect: a condition's tags can no longer be folded past.  A
churn-excluded bare-value read, or a system clock the program's rungs never
read, used to be jumpable; now the fold lands on the exact first scan the
condition can flip.
"""

from __future__ import annotations

from pyrung import Bool, Counter, Int, Program, Rung, Timer, calc, count_up, on_delay, out
from pyrung.core import And, Or, fall, rise, system
from pyrung.core.condition import _as_condition
from pyrung.core.fold import _extract_condition_reads
from pyrung.core.runner import PLC
from pyrung.core.system_points import _CLOCK_HALF_PERIODS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolved(plc: PLC, name: str) -> object:
    """Read a resolved-on-read system point (clocks, scan_counter) that is not
    stored in ``state.tags``."""
    _found, value = plc._system_runtime.resolve(name, plc.state)
    return value


def _behavioral_tags(plc: PLC) -> dict[str, object]:
    """Tag snapshot minus the per-step scan-time diagnostics.

    ``sys.scan_time_{current,min,max}_ms`` record a single interpreter pass's
    ``dt*1000``.  A fold rides the dt knob — one pass spans many scans of
    inflated dt — so these diagnostics legitimately differ from scan-by-scan
    while every behavioral tag stays bit-equal.
    """
    return {k: v for k, v in plc.state.tags.items() if not k.startswith("sys.scan_time_")}


def _assert_landing_parity(folded: PLC, stepped: PLC) -> None:
    """Fold and no-fold landed on the same scan with identical tag values."""
    assert folded.state.scan_id == stepped.state.scan_id
    assert _behavioral_tags(folded) == _behavioral_tags(stepped)


def _fold_nofold(
    prog: Program,
    condition: object,
    *,
    patch: dict[str, object] | None = None,
    presteps: int = 1,
    max_cycles: int = 40_000,
) -> tuple[PLC, PLC]:
    """Run the SAME run_until from two identical forks — one folded, one not."""
    base = PLC(prog, dt=0.010)
    if patch:
        base.patch(patch)
    for _ in range(presteps):
        base.step()

    folded = base.fork()
    stepped = base.fork()
    folded.run_until(condition, max_cycles=max_cycles, fold=True)
    stepped.run_until(condition, max_cycles=max_cycles, fold=False)
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
# 2. Folded / unfolded first-bump parity
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


def _perscan_counter_program(preset: int = 100) -> tuple[Program, Counter]:
    Enable = Bool("Enable", external=True)
    Ctr = Counter.clone("Ctr")
    Reset = Bool("Reset", external=True)
    with Program() as prog:
        with Rung(Enable):
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
        # (b) per-scan counter accumulator with `Acc >= N`.
        prog, ctr = _perscan_counter_program(100)
        folded, stepped = _fold_nofold(prog, ctr.Acc >= 30, patch={"Enable": True})
        assert folded.state.tags["Ctr_Acc"] == 30
        _assert_landing_parity(folded, stepped)

    def test_counter_accumulator_eq_exact_landing(self) -> None:
        # (c) `Acc == N` on a per-scan (integer) counter — exact landing.
        prog, ctr = _perscan_counter_program(100)
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

    X qualifies for `_unread_churn_tags` (fold.py:368-388): readers ⊆ writers,
    the writer writes only X, and the calc reads X itself."""
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    X = Int("X")
    with Program() as prog:
        with Rung():
            calc(X + 1, X)  # X := X + 1, unconditional, self-referential
        with Rung(Enable):
            on_delay(Tmr, preset_ms, "ms")
    return prog


class TestProtectedChurnExcludedRead:
    """A churn-excluded tag read by the run_until condition must break the
    plateau (not be folded past to the timer preset)."""

    def test_setup_is_real_churn_is_excluded_without_protection(self) -> None:
        # The unprotected context (target_names=∅) really does classify X as
        # unobservable churn — the setup being fixed is what the headline needs.
        plc = PLC(_churn_plus_timer_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        ctx = plc._ensure_fold_context()
        assert "X" in ctx.churn_excluded

    def test_protection_removes_x_from_churn_excluded(self) -> None:
        # Threading X in as a protected read (target_names) removes it from the
        # churn set, so the plateau guard once again watches every increment.
        plc = PLC(_churn_plus_timer_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        ctx = plc._ensure_fold_context(frozenset({"X"}))
        assert "X" not in ctx.churn_excluded

    def test_run_until_lands_on_exact_first_crossing(self) -> None:
        # End-to-end: run_until(X >= K) auto-protects X, so the fold lands on the
        # exact first scan X >= K — bit-equal to scan-by-scan.  Without the seam
        # the fold would skip X's churn straight to the timer preset and overshoot.
        prog = _churn_plus_timer_program()
        X = Int("X")
        folded, stepped = _fold_nofold(prog, X >= 30, patch={"Enable": True})
        assert folded.state.tags["X"] == 30
        _assert_landing_parity(folded, stepped)


# ---------------------------------------------------------------------------
# 4. Scan-local terminal exclusion
# ---------------------------------------------------------------------------


class TestScanLocalTerminalExclusion:
    def test_unread_unconditionally_redefined_output_is_excluded(self) -> None:
        Terminal = Int("Terminal")
        Gate = Bool("Gate", external=True)
        with Program() as program:
            with Rung():
                calc(1, Terminal)
            with Rung(Gate):
                calc(2, Terminal)

        plc = PLC(program)
        context = plc._ensure_fold_context()

        assert "Terminal" in context.churn_excluded

    def test_unread_conditional_latch_is_not_excluded(self) -> None:
        Terminal = Int("Terminal")
        Gate = Bool("Gate", external=True)
        with Program() as program:
            with Rung(Gate):
                calc(2, Terminal)

        plc = PLC(program)
        context = plc._ensure_fold_context()

        assert "Terminal" not in context.churn_excluded

    def test_predicate_visible_terminal_is_never_discardable(self) -> None:
        Terminal = Int("Terminal")
        with Program() as program:
            with Rung():
                calc(1, Terminal)

        plc = PLC(program)
        context = plc._ensure_fold_context(frozenset({Terminal.name}))

        assert "Terminal" not in context.churn_excluded


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
    """A system clock read only by the run_until condition still bounds the
    fold to its edges (the program's rungs never read it)."""

    def test_condition_clock_lands_same_scan_as_nofold(self) -> None:
        prog = _timer_no_clock_program()
        folded, stepped = _fold_nofold(prog, system.sys.clock_1s, patch={"Enable": True})
        # clock_1s first reads high at its half-period edge (0.5 s).
        assert _resolved(folded, system.sys.clock_1s.name) is True
        _assert_landing_parity(folded, stepped)

    def test_condition_clock_recorded_in_half_periods(self) -> None:
        # The context the run built carries the condition clock's half-period,
        # even though no rung reads clock_1s.
        prog = _timer_no_clock_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        clock = system.sys.clock_1s.name
        ctx = plc._ensure_fold_context(frozenset(), frozenset({clock}), frozenset())
        assert _CLOCK_HALF_PERIODS[clock] in ctx.clock_half_periods


# ---------------------------------------------------------------------------
# 5. Scan-derived signal in the condition disables folding
# ---------------------------------------------------------------------------


class TestScanDerivedInCondition:
    """A scan-id-derived signal read by the condition (changes every scan) has
    no periodic edge to land on, so it degrades the fold to scan-by-scan."""

    def test_scan_counter_condition_degrades_but_lands_exactly(self) -> None:
        prog = _timer_no_clock_program(preset_ms=100_000)
        folded, stepped = _fold_nofold(prog, system.sys.scan_counter == 40, patch={"Enable": True})
        assert _resolved(folded, system.sys.scan_counter.name) == 40
        _assert_landing_parity(folded, stepped)

    def test_scan_counter_recorded_in_scan_derived_names(self) -> None:
        prog = _timer_no_clock_program(preset_ms=100_000)
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        name = system.sys.scan_counter.name
        ctx = plc._ensure_fold_context(frozenset(), frozenset(), frozenset({name}))
        assert name in ctx.scan_derived_names


# ---------------------------------------------------------------------------
# 6. Fold-context cache semantics (keyed per protected-read triple)
# ---------------------------------------------------------------------------


class TestFoldContextCache:
    """`_ensure_fold_context` keyed-caches per (protected, clock, scan_derived)
    triple; external invalidation clears every key."""

    def test_same_key_returns_identical_object(self) -> None:
        plc = PLC(_timer_no_clock_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        ctx1 = plc._ensure_fold_context(frozenset({"Enable"}))
        ctx2 = plc._ensure_fold_context(frozenset({"Enable"}))
        assert ctx1 is ctx2

    def test_two_different_keys_two_entries(self) -> None:
        plc = PLC(_timer_no_clock_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        plc._fold_context_cache = None  # start clean
        ctx_a = plc._ensure_fold_context(frozenset({"Enable"}))
        ctx_b = plc._ensure_fold_context(frozenset({"Tmr_Acc"}))
        assert ctx_a is not ctx_b
        assert len(plc._fold_context_cache) == 2

    def test_two_run_untils_populate_two_entries(self) -> None:
        # Driving two distinct conditions through run_until leaves two cache keys.
        prog, done = _bool_flip_program(200)
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        plc._fold_context_cache = None
        plc.run_until(Bool("Enable"), max_cycles=10, fold=True)
        plc.run_until(done, max_cycles=10, fold=True)
        assert len(plc._fold_context_cache) >= 2

    def test_external_invalidation_rebuilds_without_error(self) -> None:
        plc = PLC(_timer_no_clock_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        ctx1 = plc._ensure_fold_context(frozenset({"Enable"}))
        plc._fold_context_cache = None  # external invalidation clears all keys
        ctx2 = plc._ensure_fold_context(frozenset({"Enable"}))
        assert ctx2 is not ctx1  # rebuilt fresh


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
    """A timed (ms-based) accumulator advances by one dt per scan.  `>=` always
    lands on the first scan Acc crosses the target; `==` lands only when the
    target is a whole multiple of the per-scan increment (otherwise one dt can
    step over the equality — the DESIGN's `==`→`>=` rewrite hazard, fold.py
    around _progress_bound / _scans_to_cross)."""

    def test_ge_on_timed_accumulator_lands_exactly(self) -> None:
        prog, tmr = _timed_timer_program()
        # dt=0.010 s → +10 ms/scan; `>= 250` first holds at Acc == 250.
        folded, stepped = _fold_nofold(prog, tmr.Acc >= 250, patch={"Enable": True})
        assert folded.state.tags["Tmr_Acc"] == 250
        _assert_landing_parity(folded, stepped)

    def test_eq_on_reachable_whole_multiple_lands(self) -> None:
        # 500 is a whole multiple of the 10 ms per-scan increment, so `== 500`
        # is landed exactly (no dt steps over it).  This documents the safe half
        # of the `==` hazard: on a value NOT reachable exactly the fold would
        # need the session's `==`→`>=`-plus-verify rewrite to stay honest.
        prog, tmr = _timed_timer_program()
        folded, stepped = _fold_nofold(prog, tmr.Acc == 500, patch={"Enable": True})
        assert folded.state.tags["Tmr_Acc"] == 500
        _assert_landing_parity(folded, stepped)
