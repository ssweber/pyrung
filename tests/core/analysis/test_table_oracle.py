"""Tests for the constant-table predicate oracle (``pilot/table_oracle.py``).

The oracle inverts a boolean predicate whose operands are lookups into constant
``dh``/``ds`` tables — e.g. PackML state-enablement (``stateMask[State] &
disabledMask[Mode] == 0``) and command validity (``cmdMask[Cmd] &
allowMask[State] == 0``).  It enumerates the free index registers over their
finite domains and returns the satisfying assignments.  These are the two
independent callers of the same shape in ``examples/packml_bench.py``.
"""

from __future__ import annotations

import pytest

from pyrung import PLC
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.table_oracle import solve_table_predicate


@pytest.fixture
def bench():
    """packml_bench stepped past ``~InitDone`` so the dh mask tables are filled."""
    from examples.packml_bench import logic

    plc = PLC(logic)
    for _ in range(3):
        plc.step()
    pdg = build_program_graph(plc._program)
    return plc._program, pdg, dict(plc.current_state.tags)


# --- state-enablement gate: dh[300+State] & dh[200+Mode] == 0 ----------------


def test_holding_enabled_in_production_and_maintenance(bench):
    """Holding mask dh[310]=0x0200 collides only with Manual cfg dh[203]=0x0224."""
    program, pdg, snap = bench
    sol = solve_table_predicate(
        "StateMaskResult",
        0,
        "==",
        snap,
        pdg,
        program,
        fixed={"StateRequested": 10},  # HOLDING
        domains={"UnitModeCurrent": (1, 2, 3)},
    )
    assert sol is not None
    assert sol.per_tag["UnitModeCurrent"] == [1, 2]  # Prod, Maint (not Manual)


def test_held_enabled_in_all_modes(bench):
    """Held mask dh[311]=0x0400 (bit 10) is in no mode's disabled mask."""
    program, pdg, snap = bench
    sol = solve_table_predicate(
        "StateMaskResult",
        0,
        "==",
        snap,
        pdg,
        program,
        fixed={"StateRequested": 11},  # HELD
        domains={"UnitModeCurrent": (1, 2, 3)},
    )
    assert sol is not None
    assert sol.per_tag["UnitModeCurrent"] == [1, 2, 3]


def test_enablement_matches_direct_mask_arithmetic(bench):
    """The oracle's answer must equal recomputing the mask predicate by hand."""
    program, pdg, snap = bench
    modes = (1, 2, 3)
    for state in range(1, 18):
        state_mask = snap[f"DH{300 + state}"]
        expected = [m for m in modes if state_mask & snap[f"DH{200 + m}"] == 0]
        sol = solve_table_predicate(
            "StateMaskResult",
            0,
            "==",
            snap,
            pdg,
            program,
            fixed={"StateRequested": state},
            domains={"UnitModeCurrent": modes},
        )
        assert sol is not None, f"punted on state {state}"
        assert sol.per_tag["UnitModeCurrent"] == expected, f"state {state}"


# --- command-validity gate: dh[100+Cmd] & dh[State] == 0 (second caller) ------


def test_cmd_valid_is_a_second_independent_caller(bench):
    """Same oracle, different program shape (one operand is a direct index)."""
    program, pdg, snap = bench
    states = tuple(range(1, 18))
    for cmd in (2, 4):  # Start, Hold
        cmd_mask = snap[f"DH{100 + cmd}"]
        expected = [s for s in states if cmd_mask & snap[f"DH{s}"] == 0]
        sol = solve_table_predicate(
            "CmdValidResult",
            0,
            "==",
            snap,
            pdg,
            program,
            fixed={"CtrlCmd": cmd},
            domains={"StateCurrent": states},
        )
        assert sol is not None, f"punted on cmd {cmd}"
        assert sol.per_tag["StateCurrent"] == expected, f"cmd {cmd}"


# --- soundness: never fabricate; punt when it is not a table predicate --------


def test_punts_on_multi_writer_target(bench):
    """A tag written by many rungs (not one calc) is not a table predicate."""
    program, pdg, snap = bench
    assert solve_table_predicate("StateCurrent", 0, "==", snap, pdg, program) is None


def test_punts_on_unknown_operator(bench):
    program, pdg, snap = bench
    sol = solve_table_predicate(
        "StateMaskResult",
        0,
        "in",
        snap,
        pdg,
        program,  # unsupported op
        fixed={"StateRequested": 10},
        domains={"UnitModeCurrent": (1, 2, 3)},
    )
    assert sol is None


def test_unsatisfiable_predicate_returns_empty_not_none(bench):
    """A state disabled in every available mode is a real (empty) answer, not a punt."""
    program, pdg, snap = bench
    # Restrict the mode domain to Manual only; Holding is disabled there.
    sol = solve_table_predicate(
        "StateMaskResult",
        0,
        "==",
        snap,
        pdg,
        program,
        fixed={"StateRequested": 10},  # HOLDING, disabled in Manual
        domains={"UnitModeCurrent": (3,)},
    )
    assert sol is not None
    assert sol.per_tag["UnitModeCurrent"] == []
