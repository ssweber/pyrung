"""Tests for simplified Boolean form per terminal."""

from __future__ import annotations

from pyrung.core import Bool, Int, Program, Rung, branch, latch, out, reset
from pyrung.core.analysis.simplified import (
    And,
    Atom,
    Const,
    TerminalForm,
    render,
    simplified_forms,
    simplify,
)
from pyrung.core.analysis.simplified import (
    Or as ExprOr,
)

# ---------------------------------------------------------------------------
# Basic resolution
# ---------------------------------------------------------------------------


def test_single_rung_single_input() -> None:
    A = Bool("A")
    B = Bool("B")
    with Program() as prog:
        with Rung(A):
            out(B)

    forms = simplified_forms(prog)
    assert "B" in forms
    assert render(forms["B"].expr) == "A"


def test_two_rung_chain_resolves_pivot() -> None:
    A = Bool("A")
    P = Bool("P")
    T = Bool("T")
    with Program() as prog:
        with Rung(A):
            out(P)
        with Rung(P):
            out(T)

    forms = simplified_forms(prog)
    assert render(forms["T"].expr) == "A"
    assert forms["T"].pivot_count == 1


def test_deep_chain() -> None:
    A = Bool("A")
    P1 = Bool("P1")
    P2 = Bool("P2")
    P3 = Bool("P3")
    T = Bool("T")
    with Program() as prog:
        with Rung(A):
            out(P1)
        with Rung(P1):
            out(P2)
        with Rung(P2):
            out(P3)
        with Rung(P3):
            out(T)

    forms = simplified_forms(prog)
    assert render(forms["T"].expr) == "A"
    assert forms["T"].pivot_count == 3
    assert forms["T"].depth == 3


def test_and_chain() -> None:
    A = Bool("A")
    B = Bool("B")
    T = Bool("T")
    with Program() as prog:
        with Rung(A, B):
            out(T)

    forms = simplified_forms(prog)
    assert render(forms["T"].expr) == "A, B"


def test_or_topology_via_branches() -> None:
    A = Bool("A")
    B = Bool("B")
    T = Bool("T")
    with Program() as prog:
        with Rung():
            with branch(A):
                out(T)
            with branch(B):
                out(T)

    forms = simplified_forms(prog)
    assert render(forms["T"].expr) == "Or(A, B)"


def test_branches_preserve_factored_form() -> None:
    """Sibling branches factor as And(parent, Or(local₁, local₂))."""
    P = Bool("P")
    X = Bool("X")
    Y = Bool("Y")
    T = Bool("T")
    with Program() as prog:
        with Rung(P):
            with branch(X):
                out(T)
            with branch(Y):
                out(T)

    forms = simplified_forms(prog)
    result = render(forms["T"].expr)
    assert result == "P, Or(X, Y)"


def test_pivot_resolution_preserves_branch_topology() -> None:
    """Resolved pivots with branches produce factored form, not flat DNF."""
    A = Bool("A")
    B = Bool("B")
    P = Bool("P")
    X = Bool("X")
    Y = Bool("Y")
    T = Bool("T")
    with Program() as prog:
        with Rung(A, B):
            with branch(X):
                out(P)
            with branch(Y):
                out(P)
        with Rung(P):
            out(T)

    forms = simplified_forms(prog)
    result = render(forms["T"].expr)
    assert result == "A, B, Or(X, Y)"


def test_and_or_combined() -> None:
    """A, Or(B, C) topology via rung condition + OR branches."""
    A = Bool("A")
    B = Bool("B")
    C = Bool("C")
    P = Bool("P")
    T = Bool("T")
    with Program() as prog:
        with Rung():
            with branch(B):
                out(P)
            with branch(C):
                out(P)
        with Rung(A, P):
            out(T)

    forms = simplified_forms(prog)
    result = render(forms["T"].expr)
    assert result == "A, Or(B, C)"


def test_unconditional_rung() -> None:
    T = Bool("T")
    with Program() as prog:
        with Rung():
            out(T)

    forms = simplified_forms(prog)
    assert render(forms["T"].expr) == "True"


def test_negation_normally_closed() -> None:
    A = Bool("A")
    T = Bool("T")
    with Program() as prog:
        with Rung(~A):
            out(T)

    forms = simplified_forms(prog)
    assert render(forms["T"].expr) == "~A"


def test_negation_resolves_through_pivot() -> None:
    """~P where P = A should resolve to ~A."""
    A = Bool("A")
    P = Bool("P")
    T = Bool("T")
    with Program() as prog:
        with Rung(A):
            out(P)
        with Rung(~P):
            out(T)

    forms = simplified_forms(prog)
    assert render(forms["T"].expr) == "~A"


def test_comparison_condition() -> None:
    Count = Int("Count")
    T = Bool("T")
    with Program() as prog:
        with Rung(Count > 0):
            out(T)

    forms = simplified_forms(prog)
    assert render(forms["T"].expr) == "Count > 0"


# ---------------------------------------------------------------------------
# Edge conditions (not resolvable as pivots)
# ---------------------------------------------------------------------------


def test_rising_edge() -> None:
    from pyrung.core import rise

    A = Bool("A")
    T = Bool("T")
    with Program() as prog:
        with Rung(rise(A)):
            out(T)

    forms = simplified_forms(prog)
    assert render(forms["T"].expr) == "rise(A)"


# ---------------------------------------------------------------------------
# Cycle detection (seal-in)
# ---------------------------------------------------------------------------


def test_seal_in_does_not_infinite_loop() -> None:
    Start = Bool("Start")
    SealIn = Bool("SealIn")
    Running = Bool("Running")
    T = Bool("T")
    with Program() as prog:
        with Rung():
            with branch(Start):
                out(Running)
            with branch(SealIn):
                out(Running)
        with Rung(Running):
            out(SealIn)
        with Rung(Running):
            out(T)

    forms = simplified_forms(prog)
    assert "T" in forms
    result = render(forms["T"].expr)
    assert "Start" in result or "SealIn" in result


# ---------------------------------------------------------------------------
# Simplification rules
# ---------------------------------------------------------------------------


def test_simplify_flatten() -> None:
    a = Atom("A", "xic")
    b = Atom("B", "xic")
    c = Atom("C", "xic")
    expr = And((And((a, b)), c))
    result = simplify(expr)
    assert result == And((a, b, c))


def test_simplify_dedup() -> None:
    a = Atom("A", "xic")
    expr = And((a, a))
    result = simplify(expr)
    assert result == a


def test_simplify_identity_and() -> None:
    a = Atom("A", "xic")
    expr = And((Const(True), a))
    result = simplify(expr)
    assert result == a


def test_simplify_identity_or() -> None:
    a = Atom("A", "xic")
    expr = ExprOr((Const(False), a))
    result = simplify(expr)
    assert result == a


def test_simplify_annihilation_and() -> None:
    a = Atom("A", "xic")
    expr = And((Const(False), a))
    assert simplify(expr) == Const(False)


def test_simplify_annihilation_or() -> None:
    a = Atom("A", "xic")
    expr = ExprOr((Const(True), a))
    assert simplify(expr) == Const(True)


def test_simplify_absorption() -> None:
    a = Atom("A", "xic")
    b = Atom("B", "xic")
    # Or(A, And(A, B)) → A
    expr = ExprOr((a, And((a, b))))
    result = simplify(expr)
    assert result == a


# ---------------------------------------------------------------------------
# Multiple writers (last rung wins)
# ---------------------------------------------------------------------------


def test_latch_reset_pivot_not_resolved() -> None:
    """Pivots written by latch/reset are stateful — left as atoms."""
    Start = Bool("Start")
    Stop = Bool("Stop")
    Running = Bool("Running")
    T = Bool("T")
    with Program() as prog:
        with Rung(Start):
            latch(Running)
        with Rung(Stop):
            reset(Running)
        with Rung(Running):
            out(T)

    forms = simplified_forms(prog)
    result = render(forms["T"].expr)
    assert result == "Running"
    assert forms["T"].pivot_count == 0


def test_last_writer_wins() -> None:
    A = Bool("A")
    B = Bool("B")
    T = Bool("T")
    with Program() as prog:
        with Rung(A):
            out(T)
        with Rung(B):
            out(T)

    forms = simplified_forms(prog)
    assert render(forms["T"].expr) == "B"


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


def test_terminal_form_str() -> None:
    A = Bool("A")
    T = Bool("T")
    with Program() as prog:
        with Rung(A):
            out(T)

    forms = simplified_forms(prog)
    assert str(forms["T"]) == "T = A"


def test_program_simplified_method() -> None:
    A = Bool("A")
    T = Bool("T")
    with Program() as prog:
        with Rung(A):
            out(T)

    forms = prog.simplified()
    assert isinstance(forms["T"], TerminalForm)
    assert render(forms["T"].expr) == "A"


def test_realistic_motor_circuit() -> None:
    """Multi-rung motor circuit: interlock chain + maintenance override."""
    EStop = Bool("EStop")
    RunPermit = Bool("RunPermit")
    StartBtn = Bool("StartBtn")
    Fault = Bool("Fault")
    MaintOverride = Bool("MaintOverride")
    SafetyOK = Bool("SafetyOK")
    Permitted = Bool("Permitted")
    Running = Bool("Running")
    SealIn = Bool("SealIn")
    MotorOut = Bool("MotorOut")

    with Program() as prog:
        with Rung(~EStop):
            out(SafetyOK)
        with Rung(RunPermit, SafetyOK):
            out(Permitted)
        with Rung(Permitted):
            with branch(StartBtn):
                out(Running)
            with branch(SealIn):
                out(Running)
        with Rung(Running):
            out(SealIn)
        # Both paths in same rung via branches (correct OR topology)
        with Rung():
            with branch(Running, ~Fault):
                out(MotorOut)
            with branch(MaintOverride):
                out(MotorOut)

    forms = simplified_forms(prog)
    result = render(forms["MotorOut"].expr)
    # Both paths should appear
    assert "MaintOverride" in result
    assert "~Fault" in result
    assert "~EStop" in result
    assert "RunPermit" in result
