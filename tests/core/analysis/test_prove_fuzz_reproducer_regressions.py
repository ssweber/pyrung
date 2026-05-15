"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    PLC,
    Block,
    Bool,
    Counter,
    Dint,
    Int,
    Or,
    Program,
    Real,
    Rung,
    TagType,
    Timer,
    Word,
    branch,
    calc,
    call,
    copy,
    count_down,
    count_up,
    fall,
    forloop,
    latch,
    off_delay,
    on_delay,
    out,
    reset,
    return_early,
    rise,
    search,
    subroutine,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    Proven,
    TraceStep,
    prove,
    reachable_states,
)

prove_module = importlib.import_module("pyrung.core.analysis.prove")


def _replay_trace(program: Program, trace: list[TraceStep]) -> PLC:
    """Replay a prove() counterexample trace on the concrete PLC."""
    plc = PLC(program, dt=0.010)
    for step in trace:
        plc.patch(step.inputs)
        for _ in range(step.scans):
            plc.step()
    return plc


def _assert_soundness(
    logic: Program,
    condition,
    *,
    max_states: int = 10_000,
    depth_budget: int = 20,
) -> None:
    """Assert that optimized and unoptimized prove() agree on the result type."""
    optimized = prove(
        logic, condition, max_states=max_states, depth_budget=depth_budget, journal=True
    )
    unoptimized = prove(
        logic,
        condition,
        max_states=max_states,
        depth_budget=depth_budget,
        _skip_optimizations=True,
        journal=True,
    )
    if isinstance(optimized, Intractable) or isinstance(unoptimized, Intractable):
        pytest.skip("one side intractable")
    assert type(optimized) is type(unoptimized), (
        f"optimized={type(optimized).__name__}, unoptimized={type(unoptimized).__name__}\n"
        f"--- optimized journal ---\n{optimized.journal}\n"
        f"--- unoptimized journal ---\n{unoptimized.journal}"
    )


# ===================================================================
# Fuzz reproducer regressions
# ===================================================================


def test_fuzz_timer_copy_soundness():
    In0 = Bool("In0", external=True)
    B0 = Bool("B0")
    W0 = Word("W0")
    T0 = Timer.clone("T0")

    with Program(strict=False) as logic:
        with Rung(In0):
            out(B0)
        with Rung(In0):
            on_delay(T0, 100)
            copy(T0.Acc, W0)
        with Rung(W0 >= 25):
            out(B0)
        with Rung(B0):
            out(B0)

    _assert_soundness(logic, ~B0)


def test_fuzz_timer_pending_settlement_checks_base_oneshot_pulse():
    In0 = Bool("In0", external=True)
    B0 = Bool("B0")
    B1 = Bool("B1")
    T0 = Timer.clone("T0")

    with Program(strict=False) as logic:
        with Rung(In0):
            on_delay(T0, 50)
        with Rung(T0.Acc >= 10):
            out(B0)
        with Rung(In0):
            out(B0)
        with Rung(B0):
            out(B1, oneshot=True)

    optimized = prove(logic, B1 == False, max_states=10_000, depth_budget=20)  # noqa: E712
    unoptimized = prove(
        logic,
        B1 == False,  # noqa: E712
        max_states=10_000,
        depth_budget=20,
        _skip_optimizations=True,
    )

    assert isinstance(optimized, Counterexample)
    assert isinstance(unoptimized, Counterexample)


def test_fuzz_internal_edge_source_is_not_elided():
    In0 = Bool("In0", external=True)
    B0 = Bool("B0")
    R0 = Real("R0")
    W0 = Word("W0")

    with Program(strict=False) as logic:
        with Rung(In0):
            out(B0)
        with Rung(rise(B0)):
            out(B0)
        with Rung(In0):
            copy(W0, R0)
        with Rung(In0):
            copy(0, R0)
        with Rung(In0, W0 == 0):
            reset(B0)
            copy(R0, R0)
            calc(R0 + 1, W0)

    optimized = prove(logic, B0 == False, max_states=10_000, depth_budget=20)  # noqa: E712
    unoptimized = prove(
        logic,
        B0 == False,  # noqa: E712
        max_states=10_000,
        depth_budget=20,
        _skip_optimizations=True,
    )

    assert isinstance(optimized, Counterexample)
    assert isinstance(unoptimized, Counterexample)


def test_fuzz_entry_sensitive_hidden_word_is_not_elided():
    In0 = Bool("In0", external=True)
    B0 = Bool("B0")
    R0 = Real("R0")
    W0 = Word("W0")

    with Program(strict=False) as logic:
        with Rung(In0):
            calc(R0 + 1, R0)
        with Rung(In0):
            out(B0)
        with Rung(In0):
            copy(W0, R0)
        with Rung(B0):
            copy(0, R0)
        with Rung(W0 == 0):
            reset(B0)
            calc(R0 + 1, W0)

    _assert_soundness(logic, ~B0)


def test_fuzz_entry_dependent_exit_value_is_not_concrete_elided():
    In0 = Bool("In0", external=True)
    In1 = Bool("In1", external=True)
    B0 = Bool("B0")
    R0 = Real("R0")
    W0 = Word("W0")

    with Program(strict=False) as logic:
        with Rung(In0):
            copy(W0, R0)
        with Rung(B0):
            copy(10, R0)
        with Rung(rise(In0)):
            out(B0)
        with Rung(rise(In1)):
            out(B0)
        with Rung(In1, W0 == 0):
            reset(B0)
            copy(1, R0)
            calc(R0 + R0, W0)

    _assert_soundness(logic, R0 < 3)


def test_fuzz_counter_reset_from_oneshot_does_not_poison_hidden_event_cache():
    """count_up with combinational reset (oneshot B0) must still reach Done."""
    B0 = Bool("B0")
    C0 = Counter.clone("C0")

    with Program(strict=False) as logic:
        with Rung():
            count_up(C0, 3).reset(B0)
        with Rung():
            with forloop(Int("N0", min=-27, max=6)):
                out(B0, oneshot=True)

    states = reachable_states(logic, project=["B0", "C0_Done"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", False), ("C0_Done", True)}) in states


@pytest.mark.parametrize("decrement", [10, 100])
def test_fuzz_timer_preset_overwritten_after_owner_scan_reaches_done(decrement: int):
    """on_delay reads a tag preset before a later write mutates that tag."""
    n0 = Int("N0", min=-42, max=19)
    n2 = Int("N2", min=-31, max=21)
    t0 = Timer.clone("T0")

    with Program(strict=False) as logic:
        with Rung():
            copy(50, n0)
        with Rung():
            on_delay(t0, n0)
        with Rung():
            with forloop(n2):
                calc(n0 - decrement, n0)

    states = reachable_states(logic, project=["T0_Done"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("T0_Done", True)}) in states


def test_fuzz_off_delay_under_unwritten_condition_done_stays_false():
    n2 = Int("N2")
    t1 = Timer.clone("T1")

    with Program(strict=False) as logic:
        with Rung(n2):
            off_delay(t1, 27, unit="Tms")

    states = reachable_states(logic, project=["T1_Done"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("T1_Done", False)}) in states
    assert frozenset({("T1_Done", True)}) not in states


def test_fuzz_latched_rise_count_down_initial_state_reachable():
    b0 = Bool("B0")
    b2 = Bool("B2")
    c1 = Counter.clone("C1")

    with Program(strict=False) as logic:
        with Rung():
            latch(b2)
        with Rung(rise(b2)):
            count_down(c1, 4).reset(b0)

    states = reachable_states(
        logic, project=["B0", "B2", "C1_Done"], max_states=10_000, depth_budget=20
    )
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", False), ("B2", True), ("C1_Done", False)}) in states


def test_fuzz_unconditional_timer_and_output_initial_projected_state():
    b0 = Bool("B0")
    t0 = Timer.clone("T0")

    with Program(strict=False) as logic:
        with Rung():
            on_delay(t0, 50)
        with Rung():
            out(b0)

    states = reachable_states(logic, project=["B0", "T0_Done"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", True), ("T0_Done", False)}) in states


def test_fuzz_bidirectional_counter_with_oneshot_reset_includes_first_scan_output():
    b0 = Bool("B0")
    c0 = Counter.clone("C0")

    with Program(strict=False) as logic:
        with Rung():
            count_up(c0, 5).down(b0).reset(b0)
        with Rung():
            with forloop(1):
                out(b0, oneshot=True)

    states = reachable_states(logic, project=["B0", "C0_Done"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", True), ("C0_Done", False)}) in states


def test_fuzz_count_down_reset_reachability():
    """count_down + count_up with cross-reset — BFS misses C0_Done=True."""
    C0 = Counter.clone("C0")
    C1 = Counter.clone("C1")
    B0 = Bool("B0")
    In0 = Bool("In0", external=True)

    with Program(strict=False) as logic:
        with Rung(In0):
            count_up(C1, 5).reset(C1.Done)
        with Rung(C1.Done):
            out(B0)
        with Rung(In0):
            count_down(C0, 5).reset(B0)
        with Rung(C0.Acc <= -3):
            out(B0)

    states = reachable_states(
        logic, project=["B0", "C0_Done", "C1_Done"], max_states=10_000, depth_budget=20
    )
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", True), ("C0_Done", True), ("C1_Done", False)}) in states


def test_fuzz_internal_edge_prev_keeps_counter_gate_stateful():
    """Internal demoted fall() edge must be included in elision warm-prev proofs.

    Reproducer: reachability_20260514_151001_000.  B0 is an internal edge
    source whose prev value is forwarded by BFS after demotion.  Concrete
    elision also has to test B0_prev=True; otherwise fall(B0) looks dead
    and N0 is wrongly removed from the state key.
    """
    In0 = Bool("In0", external=True)
    B0 = Bool("B0")
    N0 = Int("N0", min=0, max=3)
    C0 = Counter.clone("C0")

    with Program(strict=False) as logic:
        with Rung():
            out(B0, oneshot=True)
        with Rung(N0 != 0, fall(B0)):
            count_down(C0, 1).reset(B0)
        with Rung(In0):
            calc(N0 + 1, N0)
        with Rung():
            out(B0)

    states = reachable_states(logic, project=["B0", "C0_Done"], max_states=10_000)
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", True), ("C0_Done", True)}) in states


def test_fuzz_calc_from_choices_domain_reaches_truthy_guard():
    """choices= source domains must bound calc() targets used as truthy guards.

    Reproducer family: reachability_20260514_152443_001/_002.  ExtN0 has
    choices but no min/max; D0 is written by calc(ExtN0 * -100, D0) and must
    stay in the BFS state key so a later scan can enter the D0-gated rung.
    """
    In2 = Bool("In2", external=True)
    B1 = Bool("B1")
    ExtN0 = Int("ExtN0", external=True, choices={0: "Idle", 1: "Run", 2: "Done"})
    D0 = Dint("D0")

    with Program(strict=False) as logic:
        with Rung():
            call("sub_0")
        with Rung(~In2):
            calc(ExtN0 * -100, D0)
        with subroutine("sub_0"):
            with Rung(rise(B1)):
                return_early()
            with Rung(D0):
                out(B1)

    states = reachable_states(logic, project=["B1"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("B1", True)}) in states


def test_fuzz_call_guard_reaches_subroutine_writes_in_slice():
    """Call-site guard reads are upstream of writes inside the called subroutine.

    Reproducer: reachability_20260514_152443_000.  D0 only appears on the
    caller rung, but that condition controls whether the subroutine write to
    B1 can execute, so the B1 slice must retain D0 and its upstream input.
    """
    B1 = Bool("B1")
    ExtN0 = Int("ExtN0", external=True, choices={0: "Idle", 1: "Run", 2: "Done"})
    D0 = Dint("D0")

    with Program(strict=False) as logic:
        with Rung(D0):
            call("sub_0")
        with Rung():
            calc(ExtN0 * -100, D0)
        with subroutine("sub_0"):
            with Rung(rise(B1)):
                return_early()
            with Rung():
                out(B1)

    states = reachable_states(logic, project=["B1"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("B1", True)}) in states


class TestJournalIntegration:
    def test_explain_false_no_overhead(self):
        Button = Bool("Button", external=True)
        Light = Bool("Light")
        with Program() as logic:
            with Rung(Button):
                out(Light)

        result = prove(logic, ~Light)
        assert isinstance(result, Counterexample)
        assert result.journal is None

    def test_journal_true_returns_journal(self):
        Button = Bool("Button", external=True)
        Light = Bool("Light")
        with Program() as logic:
            with Rung(Button):
                out(Light)

        result = prove(logic, Or(~Button, Light), journal=True)
        assert isinstance(result, Proven)
        assert result.journal is not None
        assert len(result.journal) > 0
        assert "Button" in result.journal
        assert "Light" in result.journal

    def test_explain_counterexample(self):
        Button = Bool("Button", external=True)
        Light = Bool("Light")
        with Program() as logic:
            with Rung(Button):
                out(Light)

        result = prove(logic, ~Light, journal=True)
        assert isinstance(result, Counterexample)
        assert result.journal is not None
        assert "Button" in result.journal
        button_entry = result.journal["Button"]
        assert button_entry.outcome.startswith("nondeterministic")

    def test_explain_caveats_coexist(self):
        Button = Bool("Button", external=True)
        Light = Bool("Light")
        with Program() as logic:
            with Rung(Button):
                out(Light)

        result_no = prove(logic, Or(~Button, Light))
        result_yes = prove(logic, Or(~Button, Light), journal=True)
        assert isinstance(result_no, Proven)
        assert isinstance(result_yes, Proven)
        assert result_no.caveats == result_yes.caveats

    def test_explain_str_readable(self):
        Button = Bool("Button", external=True)
        Light = Bool("Light")
        with Program() as logic:
            with Rung(Button):
                out(Light)

        result = prove(logic, Or(~Button, Light), journal=True)
        assert isinstance(result, Proven)
        assert result.journal is not None
        text = str(result.journal)
        assert "Button" in text
        assert "Light" in text

    def test_explain_getitem_iter_contains_len(self):
        Button = Bool("Button", external=True)
        Light = Bool("Light")
        with Program() as logic:
            with Rung(Button):
                out(Light)

        result = prove(logic, Or(~Button, Light), journal=True)
        assert isinstance(result, Proven)
        expl = result.journal
        assert expl is not None
        assert "Button" in expl
        assert "NonExistent" not in expl
        entries = list(expl)
        assert len(entries) == len(expl)
        entry = expl["Button"]
        assert entry.name == "Button"
        with pytest.raises(KeyError):
            expl["NonExistent"]

    def test_explain_max_states_intractable(self):
        A = Bool("A", external=True)
        B = Bool("B", external=True)
        C = Bool("C", external=True)
        L1 = Bool("L1")
        L2 = Bool("L2")
        L3 = Bool("L3")
        Out = Bool("Out")
        with Program() as logic:
            with Rung(Or(A, L1)):
                out(L1)
            with Rung(Or(B, L2)):
                out(L2)
            with Rung(Or(C, L3)):
                out(L3)
            with Rung(L1, L2, L3):
                out(Out)

        result = prove(logic, ~Out, max_states=5, journal=True)
        assert isinstance(result, Intractable)
        assert result.journal is not None


def test_search_result_domain_inference():
    """search() result tag must get a domain so it's tracked cross-scan."""
    In0 = Bool("In0", external=True)
    B0 = Bool("B0")
    N0 = Int("N0")
    W0 = Word("W0")
    DS = Block("DS", TagType.INT, 1, 3)

    with Program(strict=False) as logic:
        with Rung():
            out(B0)
            calc(N0 + N0, W0)
        with Rung(In0):
            search(DS.select(1, 1) == 0, result=N0, found=B0)

    _assert_soundness(logic, W0 < 1)


def test_concrete_elision_includes_default_value():
    """Concrete elision must test the default value even when the structural
    domain doesn't include it — a conditionally-written tag retains its
    default when no write fires."""
    In0 = Bool("In0", external=True)
    B0 = Bool("B0")
    N0 = Int("N0", choices={0: "off", 1: "on", 2: "auto"})
    W0 = Word("W0")
    T0 = Timer.clone("T0")
    DS = Block("DS", TagType.INT, 1, 3)

    with Program(strict=False) as logic:
        with Rung(In0):
            on_delay(T0, 50)
        with Rung(T0.Acc >= 10):
            out(B0)
        with Rung(~In0):
            out(B0)
            calc(N0 + N0, W0)
        with Rung(B0):
            out(B0)
            out(B0)
            search(DS.select(1, 1) == 0, result=N0, found=B0)

    _assert_soundness(logic, W0 < 1)


def test_nd_timer_preset_live_input_pruning():
    """ND timer preset must not be pruned by live-input analysis.

    ExtN0 is the on_delay preset.  It only appears in instruction data reads
    (not conditions), so the partial-eval live-input analysis previously
    classified it as dead — causing BFS to miss {B0=True, T0_Done=False}.
    """
    In0 = Bool("In0", external=True)
    B0 = Bool("B0")
    ExtN0 = Int("ExtN0", external=True, choices={0: "Off", 1: "On", 2: "Auto"})
    N0 = Int("N0", min=0, max=1)
    T0 = Timer.clone("T0")

    with Program(strict=False) as logic:
        with Rung(In0):
            with forloop(N0):
                out(B0)
        with Rung(~In0):
            out(B0)
            on_delay(T0, ExtN0, unit="sec").reset(B0)

    projection = ["B0", "T0_Done"]
    bfs = reachable_states(logic, project=projection, max_states=10_000, depth_budget=20)
    assert not isinstance(bfs, Intractable)

    plc = PLC(logic, dt=0.010)
    plc.patch({"In0": False, "ExtN0": 1})
    plc.step()
    tags = plc.current_state.tags
    state = frozenset((name, tags[name]) for name in projection)
    assert state in bfs


def test_fuzz_oneshot_out_elision_false_convergence():
    """Oneshot out(B0) must not be elided by abstract provenance — kernel
    memory makes B0 pulse once then stay False, but abstract analysis lacks
    a memory model and falsely converges to True."""
    B0 = Bool("B0")
    C0 = Counter.clone("C0")

    with Program(strict=False) as logic:
        with Rung():
            calc(C0.Acc + 5, C0.Acc)
        with Rung():
            count_up(C0, 10).reset(B0)
        with Rung():
            with forloop(1):
                out(B0, oneshot=True)

    states = reachable_states(logic, project=["B0", "C0_Done"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", False), ("C0_Done", True)}) in states


def test_fuzz_oneshot_out_elision_with_bidirectional_counter():
    """Oneshot out(B0) feeding count_up .down/.reset — B0 elision hides
    the Done=True state."""
    B0 = Bool("B0")
    B1 = Bool("B1")
    C0 = Counter.clone("C0")

    with Program(strict=False) as logic:
        with Rung():
            count_up(C0, 10).down(B0).reset(B1)
        with Rung():
            out(B0, oneshot=True)

    states = reachable_states(
        logic, project=["B0", "B1", "C0_Done"], max_states=10_000, depth_budget=20
    )
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", False), ("B1", False), ("C0_Done", True)}) in states


def test_fuzz_same_rung_branch_reader_not_combinational():
    """out(B0) + branch(B0): out(B1) — B0 must not be classified as
    combinational because the branch reads the rung-entry snapshot, not
    the freshly-written value."""
    B0 = Bool("B0")
    B1 = Bool("B1")

    with Program(strict=False) as logic:
        with Rung():
            out(B0)
            with branch(B0):
                out(B1)

    states = reachable_states(logic, project=["B0", "B1"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", True), ("B1", True)}) in states


def test_fuzz_return_early_guard_in_scope():
    """return_early guard condition_reads must be visible to upstream_slice.

    N0 increments each scan; when N0==3 the subroutine body past
    return_early() sets B0.  Without the guard propagation, N0 is
    outside B0's upstream cone and gets dropped from the state key.
    """
    from pyrung.core import call, return_early, subroutine

    N0 = Int("N0", min=0, max=5)
    B0 = Bool("B0")

    with Program(strict=False) as logic:
        with Rung():
            calc(N0 + 1, N0)
        with Rung():
            call("sub_0")
        with subroutine("sub_0"):
            with Rung(N0 != 3):
                return_early()
            with Rung():
                out(B0)

    states = reachable_states(logic, project=["B0"], max_states=10_000, depth_budget=10)
    if isinstance(states, Intractable):
        return
    assert frozenset({("B0", True)}) in states


def test_fuzz_latch_target_not_classified_combinational():
    """latch(B2) is retentive — B2 must be stateful even without cross-scan readers."""
    In0 = Bool("In0", external=True)
    In1 = Bool("In1", external=True)
    B0 = Bool("B0")
    B2 = Bool("B2")
    N1 = Int("N1")
    N2 = Int("N2", min=-27, max=12)

    with Program(strict=False) as logic:
        with Rung(In0):
            copy(32767, N1, oneshot=True)
        with Rung(rise(In0)):
            out(B0)
        with Rung(rise(In1), N1):
            latch(B2)
        with Rung():
            copy(N2, N1)

    states = reachable_states(
        logic,
        project=["B0", "B2"],
        joint_inputs=(("In0", "In1"),),
        max_states=10_000,
        depth_budget=20,
    )
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", False), ("B2", True)}) in states


def test_fuzz_off_delay_completion_with_accumulating_condition():
    """off_delay completion must be reachable when R0 accumulates past the enable threshold."""
    ExtN0 = Int("ExtN0", external=True, choices={1: "A", 2: "B"})
    ExtN1 = Int("ExtN1", external=True, min=-10, max=100)
    B0 = Bool("B0")
    R0 = Real("R0")
    T0 = Timer.clone("T0")

    with Program(strict=False) as logic:
        with Rung(R0 <= 76):
            off_delay(T0, ExtN1)
        with Rung():
            calc(R0 + ExtN0, R0)
        with Rung():
            out(B0)

    states = reachable_states(logic, project=["B0", "T0_Done"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", True), ("T0_Done", False)}) in states


def test_fuzz_counter_acc_conditional_calc_not_elided():
    """Counter accumulator written by conditional calc must remain stateful.

    Reproducer: reachability_20260514_120414_000.  C0_Acc is exclusively
    read by count_up; exclusive_reads were invisible to the concrete
    elider's frontier traversal, so C0_Done was missing from the observer
    set and C0_Acc was wrongly elided.
    """
    In0 = Bool("In0", external=True)
    ExtN0 = Int("ExtN0", external=True, choices={1: "A", 2: "B"})
    B0 = Bool("B0")
    N0 = Int("N0", min=-6, max=41)
    C0 = Counter.clone("C0")

    with Program(strict=False) as logic:
        with Rung(In0):
            calc(C0.Acc + 5, C0.Acc)
        with Rung():
            count_up(C0, 10).reset(B0)
        with Rung(C0.Acc >= 5):
            out(B0)
        with Rung(N0 != 0):
            out(B0)
        with Rung():
            copy(ExtN0, N0)

    states = reachable_states(logic, project=["B0", "C0_Done"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", True), ("C0_Done", True)}) in states


def test_fuzz_counter_acc_count_up_not_elided():
    """Counter accumulator with guard on Acc must be retained.

    Reproducer: reachability_20260514_120414_001.  Simpler variant — no
    conditional calc on the accumulator, but the guard C0.Acc >= 5 makes
    the entry value observable.
    """
    In0 = Bool("In0", external=True)
    ExtN0 = Int("ExtN0", external=True, choices={1: "A", 2: "B"})
    B0 = Bool("B0")
    N0 = Int("N0", min=-6, max=41)
    C0 = Counter.clone("C0")
    DS = Block("DS", TagType.INT, 1, 6)

    with Program(strict=False) as logic:
        with Rung():
            count_up(C0, 10).reset(B0)
        with Rung(C0.Acc >= 5):
            out(B0)
        with Rung():
            copy(ExtN0, N0)
        with Rung(In0):
            calc(DS.select(1, 2).sum(), N0)
        with Rung(N0 != 0):
            out(B0)

    states = reachable_states(logic, project=["B0", "C0_Done"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", False), ("C0_Done", True)}) in states


def test_fuzz_counter_acc_count_down_not_elided():
    """Count-down accumulator with negative threshold must be retained.

    Reproducer: reachability_20260514_120414_002.  count_down variant
    where C0.Acc <= -3 guards B0, but a later rung overwrites B0.
    """
    ExtN0 = Int("ExtN0", external=True, choices={1: "A", 2: "B"})
    B0 = Bool("B0")
    C0 = Counter.clone("C0")

    with Program(strict=False) as logic:
        with Rung():
            count_down(C0, 5).reset(B0)
        with Rung(C0.Acc <= -3):
            out(B0)
        with Rung(ExtN0 <= 0):
            out(B0)

    states = reachable_states(logic, project=["B0", "C0_Done"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", False), ("C0_Done", True)}) in states


def test_fuzz_oneshot_copy_write_only_not_elided():
    """Oneshot copy writes N2 but N2 is never read within the scan.

    Abstract elision must not elide N2: on subsequent True scans the
    oneshot does not fire, so N2 retains its cross-scan entry value.
    Reproducer: soundness_20260514_125309_003.
    """
    ExtN0 = Int("ExtN0", external=True, choices={0: "Off", 1: "On", 2: "Auto"})
    N2 = Int("N2")

    with Program(strict=False) as logic:
        with Rung():
            copy(ExtN0, N2, oneshot=True)

    _assert_soundness(logic, N2 < 1)


def test_fuzz_self_resetting_counter_done_reachable():
    """Self-resetting counter Done=True state must be reachable.

    count_up(C1, 5).reset(C1.Done) resets when Done fires; the
    transient Done=True state is still reachable for one scan.
    The hidden-event mechanism must not abort the event because a
    *different* counter was reset as a side effect.
    Reproducer: reachability_20260514_125309_000.
    """
    In0 = Bool("In0", external=True)
    B0 = Bool("B0")
    C0 = Counter.clone("C0")
    C1 = Counter.clone("C1")

    with Program(strict=False) as logic:
        with Rung(In0):
            count_up(C1, 5).reset(C1.Done)
        with Rung(C1.Done):
            out(B0)
        with Rung():
            count_up(C0, 1).down(B0).reset(B0)

    states = reachable_states(
        logic,
        project=["B0", "C0_Done", "C1_Done"],
        max_states=10_000,
        depth_budget=20,
    )
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", True), ("C0_Done", False), ("C1_Done", True)}) in states


def test_fuzz_indirect_block_write_not_excluded():
    """copy(10, DS[DS[1] + 2]) writes to DS[2] via computed index.

    Both the PDG and literal-write domain extractor failed to resolve
    indirect expression targets, causing block elements to be excluded
    as unwritten_internal and their values untracked in BFS.
    Reproducer: reachability_20260515_145425_000.
    """
    D0 = Dint("D0")
    DS = Block("DS", TagType.INT, 1, 4)
    B0 = Bool("B0")

    with Program(strict=False) as logic:
        with Rung():
            calc(DS.select(1, 2).sum(), D0)
        with Rung(D0 != 0):
            out(B0)
        with Rung():
            copy(10, DS[DS[1] + 2])

    states = reachable_states(logic, project=["B0"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", True)}) in states
    assert frozenset({("B0", False)}) in states


def test_fuzz_oneshot_out_traced_elision_misses_memory_state():
    """out(B0, oneshot=True) with external input gating the condition.

    Traced elision must not classify B0 as scan-local: the _oneshot:
    memory key carries cross-scan state that changes B0's exit value
    from True (scan 1) to False (scan 2+).
    Reproducer: reachability_20260515_141910_000.
    """
    B0 = Bool("B0")
    ExtN0 = Int("ExtN0", external=True, min=0, max=100)
    R0 = Real("R0")

    with Program(strict=False) as logic:
        with Rung():
            calc(ExtN0 + 0, R0)
        with Rung(R0 == 0):
            out(B0)
        with Rung(~B0):
            out(B0, oneshot=True)

    states = reachable_states(logic, project=["B0"], max_states=10_000, depth_budget=20)
    assert not isinstance(states, Intractable)
    assert frozenset({("B0", True)}) in states
    assert frozenset({("B0", False)}) in states
