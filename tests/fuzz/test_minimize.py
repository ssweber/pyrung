"""Unit tests for the structural ProgramSpec minimizer."""

from __future__ import annotations

from pyrung.core import Bool, Int

from .minimize import minimize
from .pool import TagPool
from .strategies import (
    BranchSpec,
    CondSpec,
    ForLoopSpec,
    InstrSpec,
    ProgramSpec,
    RungSpec,
    SubroutineSpec,
)


def _pool() -> TagPool:
    return TagPool(
        bool_inputs=[Bool("In0", external=True)],
        bool_internal=[Bool("B0"), Bool("B1")],
        int_tags=[Int("N0")],
    )


def _bit(tag) -> CondSpec:
    return CondSpec(kind="bit", tag=tag)


def _out(tag) -> InstrSpec:
    return InstrSpec(kind="out", args={"target": tag})


def _copy(src, dest) -> InstrSpec:
    return InstrSpec(kind="copy", args={"source": src, "dest": dest, "oneshot": False})


# ---------------------------------------------------------------------------
# Rung removal
# ---------------------------------------------------------------------------


def test_removes_irrelevant_rungs():
    pool = _pool()
    B0, B1 = pool.bool_internal
    In0 = pool.bool_inputs[0]
    spec = ProgramSpec(
        pool=pool,
        rungs=[
            RungSpec(conditions=[_bit(In0)], instructions=[_out(B1)]),
            RungSpec(conditions=[_bit(In0)], instructions=[_out(B0)]),
            RungSpec(conditions=[_bit(B0)], instructions=[_out(B1)]),
            RungSpec(conditions=[], instructions=[_out(B1)]),
        ],
    )

    def check(candidate):
        return any(any(i.args.get("target") is B0 for i in r.instructions) for r in candidate.rungs)

    result = minimize(spec, check)
    assert len(result.rungs) == 1
    assert result.rungs[0].instructions[0].args["target"] is B0


# ---------------------------------------------------------------------------
# Instruction removal
# ---------------------------------------------------------------------------


def test_removes_irrelevant_instructions():
    pool = _pool()
    B0, B1 = pool.bool_internal
    N0 = pool.int_tags[0]
    spec = ProgramSpec(
        pool=pool,
        rungs=[
            RungSpec(
                conditions=[],
                instructions=[_out(B0), _copy(0, N0), _out(B1)],
            ),
        ],
    )

    def check(candidate):
        rung = candidate.rungs[0]
        return any(i.kind == "copy" for i in rung.instructions)

    result = minimize(spec, check)
    assert len(result.rungs) == 1
    assert len(result.rungs[0].instructions) == 1
    assert result.rungs[0].instructions[0].kind == "copy"


# ---------------------------------------------------------------------------
# Condition removal
# ---------------------------------------------------------------------------


def test_removes_irrelevant_conditions():
    pool = _pool()
    B0, B1 = pool.bool_internal
    In0 = pool.bool_inputs[0]
    spec = ProgramSpec(
        pool=pool,
        rungs=[
            RungSpec(
                conditions=[_bit(In0), _bit(B0), _bit(B1)],
                instructions=[_out(B0)],
            ),
        ],
    )

    def check(candidate):
        conds = candidate.rungs[0].conditions
        return any(c.tag is In0 for c in conds)

    result = minimize(spec, check)
    assert len(result.rungs[0].conditions) == 1
    assert result.rungs[0].conditions[0].tag is In0


# ---------------------------------------------------------------------------
# Composite condition simplification
# ---------------------------------------------------------------------------


def test_simplifies_composite_and():
    pool = _pool()
    B0 = pool.bool_internal[0]
    In0 = pool.bool_inputs[0]
    composite = CondSpec(
        kind="composite_and",
        operand=(_bit(In0), _bit(B0)),
    )
    spec = ProgramSpec(
        pool=pool,
        rungs=[
            RungSpec(conditions=[composite], instructions=[_out(B0)]),
        ],
    )

    def check(candidate):
        cond = candidate.rungs[0].conditions[0]
        if cond.kind == "composite_and":
            return True
        return cond.tag is In0

    result = minimize(spec, check)
    assert result.rungs[0].conditions[0].kind == "bit"
    assert result.rungs[0].conditions[0].tag is In0


def test_simplifies_composite_or():
    pool = _pool()
    B0, B1 = pool.bool_internal
    composite = CondSpec(
        kind="composite_or",
        operand=(_bit(B0), _bit(B1)),
    )
    spec = ProgramSpec(
        pool=pool,
        rungs=[
            RungSpec(conditions=[composite], instructions=[_out(B0)]),
        ],
    )

    def check(candidate):
        cond = candidate.rungs[0].conditions[0]
        if cond.kind == "composite_or":
            return True
        return cond.tag is B1

    result = minimize(spec, check)
    assert result.rungs[0].conditions[0].kind == "bit"
    assert result.rungs[0].conditions[0].tag is B1


# ---------------------------------------------------------------------------
# Subroutine removal
# ---------------------------------------------------------------------------


def test_removes_subroutine_with_call_site():
    pool = _pool()
    B0, B1 = pool.bool_internal
    In0 = pool.bool_inputs[0]
    spec = ProgramSpec(
        pool=pool,
        rungs=[
            RungSpec(conditions=[_bit(In0)], instructions=[_out(B0)]),
            RungSpec(
                conditions=[],
                instructions=[InstrSpec(kind="call", args={"name": "sub_0"})],
            ),
        ],
        subroutines=[
            SubroutineSpec(
                name="sub_0",
                rungs=[RungSpec(conditions=[], instructions=[_out(B1)])],
            ),
        ],
    )

    def check(candidate):
        return any(any(i.args.get("target") is B0 for i in r.instructions) for r in candidate.rungs)

    result = minimize(spec, check)
    assert len(result.subroutines) == 0
    assert not any(_has_call(r) for r in result.rungs)


def _has_call(rung: RungSpec) -> bool:
    return any(i.kind == "call" for i in rung.instructions)


# ---------------------------------------------------------------------------
# Branch removal
# ---------------------------------------------------------------------------


def test_removes_irrelevant_branches():
    pool = _pool()
    B0, B1 = pool.bool_internal
    In0 = pool.bool_inputs[0]
    spec = ProgramSpec(
        pool=pool,
        rungs=[
            RungSpec(
                conditions=[_bit(In0)],
                instructions=[_out(B0)],
                branches=[
                    BranchSpec(conditions=[_bit(B0)], instructions=[_out(B1)]),
                    BranchSpec(conditions=[_bit(B1)], instructions=[_out(B0)]),
                ],
            ),
        ],
    )

    def check(candidate):
        return any(any(i.args.get("target") is B0 for i in r.instructions) for r in candidate.rungs)

    result = minimize(spec, check)
    assert len(result.rungs[0].branches) == 0


# ---------------------------------------------------------------------------
# Subroutine inlining
# ---------------------------------------------------------------------------


def test_inlines_subroutine_at_call_site():
    pool = _pool()
    B0, B1 = pool.bool_internal
    In0 = pool.bool_inputs[0]
    spec = ProgramSpec(
        pool=pool,
        rungs=[
            RungSpec(
                conditions=[],
                instructions=[InstrSpec(kind="call", args={"name": "sub_0"})],
            ),
            RungSpec(conditions=[_bit(In0)], instructions=[_out(B1)]),
        ],
        subroutines=[
            SubroutineSpec(
                name="sub_0",
                rungs=[RungSpec(conditions=[], instructions=[_out(B0)])],
            ),
        ],
    )

    def check(candidate):
        return any(any(i.args.get("target") is B0 for i in r.instructions) for r in candidate.rungs)

    result = minimize(spec, check)
    assert len(result.subroutines) == 0
    assert any(any(i.args.get("target") is B0 for i in r.instructions) for r in result.rungs)


# ---------------------------------------------------------------------------
# Forloop unwrapping
# ---------------------------------------------------------------------------


def test_unwraps_forloop():
    pool = _pool()
    B0 = pool.bool_internal[0]
    N0 = pool.int_tags[0]
    spec = ProgramSpec(
        pool=pool,
        rungs=[
            RungSpec(
                conditions=[],
                instructions=[_out(B0)],
                forloop=ForLoopSpec(count=N0),
            ),
        ],
    )

    def check(candidate):
        return any(any(i.args.get("target") is B0 for i in r.instructions) for r in candidate.rungs)

    result = minimize(spec, check)
    assert len(result.rungs) == 1
    assert result.rungs[0].forloop is None
    assert result.rungs[0].instructions[0].args["target"] is B0


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def test_budget_zero_returns_unchanged():
    pool = _pool()
    B0, B1 = pool.bool_internal
    In0 = pool.bool_inputs[0]
    spec = ProgramSpec(
        pool=pool,
        rungs=[
            RungSpec(conditions=[_bit(In0)], instructions=[_out(B0)]),
            RungSpec(conditions=[_bit(B0)], instructions=[_out(B1)]),
            RungSpec(conditions=[], instructions=[_out(B1)]),
        ],
    )
    result = minimize(spec, lambda _: True, budget=0.0)
    assert len(result.rungs) == len(spec.rungs)


# ---------------------------------------------------------------------------
# Fixpoint: removing a rung enables further instruction removal
# ---------------------------------------------------------------------------


def test_fixpoint_across_phases():
    pool = _pool()
    B0, B1 = pool.bool_internal
    In0 = pool.bool_inputs[0]
    N0 = pool.int_tags[0]
    spec = ProgramSpec(
        pool=pool,
        rungs=[
            RungSpec(conditions=[], instructions=[_out(B1)]),
            RungSpec(
                conditions=[_bit(In0)],
                instructions=[_out(B0), _copy(0, N0)],
            ),
        ],
    )

    def check(candidate):
        for r in candidate.rungs:
            if any(i.kind == "copy" for i in r.instructions):
                return True
        return False

    result = minimize(spec, check)
    assert len(result.rungs) == 1
    assert len(result.rungs[0].instructions) == 1
    assert result.rungs[0].instructions[0].kind == "copy"
    assert len(result.rungs[0].conditions) == 0


# ---------------------------------------------------------------------------
# Check function that raises is treated as non-reproducing
# ---------------------------------------------------------------------------


def test_check_exception_treated_as_false():
    pool = _pool()
    B0 = pool.bool_internal[0]
    spec = ProgramSpec(
        pool=pool,
        rungs=[
            RungSpec(conditions=[], instructions=[_out(B0)]),
            RungSpec(conditions=[], instructions=[_out(B0)]),
        ],
    )

    call_count = 0

    def check(candidate):
        nonlocal call_count
        call_count += 1
        if len(candidate.rungs) < 2:
            raise RuntimeError("boom")
        return True

    result = minimize(spec, check)
    assert len(result.rungs) == 2
    assert call_count > 0
