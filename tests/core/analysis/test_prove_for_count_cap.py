"""A For count the verifier would run past its cap answers Intractable.

A Dint count is read as unsigned 32-bit (``control.for_count_mask``), so a
negative one runs about 4.29 billion iterations in one scan, and the search
(``never``, ``reachable_states``, ``check_lock``) never returned.  The
verifier's kernel stops a 32-bit count past ``PROVE_FOR_ITERATION_CAP`` and the
search names the loop and its count; the slice elision's interpreter scans
skip a program with such a count.  A count the cap allows still answers.

Every shape: ``work`` copies ``Btn`` to ``Y2`` once per iteration, so ``Y3``
follows ``Btn`` unless a scan runs the loop no times (a count of 0), when
``Y2`` holds and ``And(Y3, ~Btn)`` is reached.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core import (
    And,
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Rung,
    TagType,
    calc,
    call,
    copy,
    forloop,
    out,
    subroutine,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    LockIntractable,
    Proven,
    check_lock,
    never,
    reachable_states,
)
from pyrung.core.analysis.prove.lockfile import program_hash, write_lock
from pyrung.core.kernel import PROVE_FOR_ITERATION_CAP

_HERE = Path(__file__).name


def _loop_program(count: Callable[[], object], before: Callable[[object], None] | None = None):
    btn = Bool("Btn", external=True)
    y2 = Bool("Y2")
    y3 = Bool("Y3")
    n = count()
    with Program(strict=False) as logic:
        if before is not None:
            before(n)
        with Rung():
            with forloop(n):
                call("work")
        with Rung(y2):
            out(y3)
        with subroutine("work"):
            with Rung():
                copy(btn, y2)
    return logic, And(y3, ~btn)


def _assert_refused(result: object, iterations: int, tags: list[str]) -> None:
    assert isinstance(result, Intractable), result
    assert result.reason.startswith("forloop(")
    assert _HERE in result.reason
    assert f"can run {iterations} times in one scan" in result.reason
    assert result.tags == tags


def test_an_unbounded_dint_count_is_refused_naming_the_loop_and_its_tag() -> None:
    logic, prop = _loop_program(lambda: Dint("N", external=True))
    result = never(logic, prop)
    _assert_refused(result, 0xFFFFFFFF, ["N"])
    assert "forloop(N)" in result.reason
    assert any("min=/max=/choices=" in hint for hint in result.hints)


def test_a_dint_count_that_can_be_negative_is_refused() -> None:
    logic, prop = _loop_program(lambda: Dint("N", external=True, min=-1, max=1))
    _assert_refused(never(logic, prop), 0xFFFFFFFF, ["N"])


def test_a_count_computed_below_its_declared_bounds_is_refused() -> None:
    # N's declared 0..50 does not hold: X - 1 is -1 at X = 0.  The kernel stops it.
    x = Int("X", external=True, min=0, max=10)

    def write_n(n: object) -> None:
        with Rung():
            calc(x - 1, n)

    logic, prop = _loop_program(lambda: Dint("N", min=0, max=50), write_n)
    _assert_refused(never(logic, prop), 0xFFFFFFFF, ["N"])


def test_an_indexed_dint_count_names_its_pointer() -> None:
    dd = Block("DD", TagType.DINT, 1, 4)
    p = Int("P", external=True, min=1, max=4)

    def write_dd(_count: object) -> None:
        with Rung():
            copy(-1, dd[2])

    logic, prop = _loop_program(lambda: dd[p], write_dd)
    result = never(logic, prop)
    assert isinstance(result, Intractable), result
    assert "forloop(DD[P])" in result.reason
    assert result.tags == ["P"]


def test_a_bounded_dint_count_past_65535_still_answers() -> None:
    logic, prop = _loop_program(lambda: Dint("N", external=True, min=0, max=70000))
    assert isinstance(never(logic, prop), Counterexample)


def test_a_count_at_the_cap_answers_and_one_past_it_is_refused() -> None:
    at = PROVE_FOR_ITERATION_CAP
    logic, prop = _loop_program(lambda: Dint("N", external=True, choices={3: "few", at: "cap"}))
    assert isinstance(never(logic, prop), Proven)
    logic, prop = _loop_program(
        lambda: Dint("N", external=True, choices={3: "few", at + 1: "past"})
    )
    _assert_refused(never(logic, prop), at + 1, ["N"])


def test_a_16_bit_count_is_never_capped() -> None:
    logic, prop = _loop_program(lambda: Int("N", external=True))
    assert isinstance(never(logic, prop), Counterexample)
    assert "_ForIterationsBeyondCap" not in compile_kernel(logic, proof_metadata=True).source


def test_reachable_states_and_check_lock_refuse_it_too(tmp_path: Path) -> None:
    logic, _prop = _loop_program(lambda: Dint("N", external=True))
    _assert_refused(reachable_states(logic, project=["Y3"]), 0xFFFFFFFF, ["N"])
    lock = tmp_path / "pyrung.lock"
    write_lock(lock, frozenset(), ["Y3"], "hash")
    with pytest.raises(LockIntractable):
        check_lock(logic, lock)


def test_only_the_verifiers_kernel_is_capped() -> None:
    # The runtime's kernel (CompiledPLC) and a code.py run the count as read.
    logic, _prop = _loop_program(lambda: Dint("N", external=True))
    assert "_ForIterationsBeyondCap" in compile_kernel(logic, proof_metadata=True).source
    assert "_ForIterationsBeyondCap" not in compile_kernel(logic).source


def _hash_of_a_loop(blank_lines: int) -> str:
    # The same program, its For built *blank_lines* source lines further down.
    n = Dint("N", external=True)
    y = Bool("Y")
    source = "\n" * blank_lines + "with Rung():\n    with forloop(n):\n        copy(1, y)\n"
    with Program(strict=False) as logic:
        exec(compile(source, "built_elsewhere.py", "exec"), {**globals(), "n": n, "y": y})  # noqa: S102
    return program_hash(logic)


def test_a_capped_loops_place_stays_out_of_the_lock_hash() -> None:
    assert _hash_of_a_loop(0) == _hash_of_a_loop(1)
