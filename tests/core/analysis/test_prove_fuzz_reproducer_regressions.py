"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    PLC,
    Block,
    Bool,
    Int,
    Or,
    Program,
    Real,
    Rung,
    TagType,
    Timer,
    Word,
    calc,
    copy,
    on_delay,
    out,
    reset,
    rise,
    search,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    Proven,
    TraceStep,
    prove,
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
