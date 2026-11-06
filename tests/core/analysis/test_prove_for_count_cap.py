"""A For count the verifier would run past its cap answers Intractable.

A Dint count is read whole and signed (``control.for_loop_iterations``), so a
positive one can run up to 2147483647 iterations in one scan, and the search
(``never``, ``reachable_states``, ``check_lock``) would not return.  The
verifier's kernel stops a 32-bit count past ``PROVE_FOR_ITERATION_CAP`` and the
search names the loop and its count; the slice elision's interpreter scans
skip a program with such a count.  A count the cap allows still answers, and a
negative one runs none.

How many times the body runs is the count's value, so the search explores every
value a free count can hold (``classify._tag_compared_names``): a count with no
declared domain, or one too wide to list, is an unbounded domain.

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
    Word,
    call,
    copy,
    forloop,
    math,
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


#: The largest Dint: the most iterations a 32-bit count runs.
_MAX_DINT = 2**31 - 1


def _huge_count() -> Dint:
    return Dint("N", external=True, choices={3: "few", _MAX_DINT: "max"})


def test_a_huge_dint_count_is_refused_naming_the_loop_and_its_tag() -> None:
    logic, prop = _loop_program(_huge_count)
    result = never(logic, prop)
    _assert_refused(result, _MAX_DINT, ["N"])
    assert "forloop(N)" in result.reason
    assert any("min=/max=/choices=" in hint for hint in result.hints)


def test_a_negative_dint_count_runs_none_and_answers() -> None:
    # A DD of -1 runs no iteration (hardware 2026-11-06): Y2 holds and the goal is reached.
    for count in (
        lambda: Dint("N", external=True, min=-1, max=1),
        lambda: Dint("N", external=True, choices={-1: "minus", 3: "few"}),
        lambda: Dint("N", external=True, choices={-(2**31): "lowest", 3: "few"}),
    ):
        logic, prop = _loop_program(count)
        result = never(logic, prop)
        assert isinstance(result, Counterexample), result
        assert any(step.inputs.get("N", 1) <= 0 for step in result.trace), result.trace


def test_a_count_computed_past_its_declared_bounds_is_refused() -> None:
    # N's declared 0..50 does not hold: X is copied in.  The kernel stops it.
    x = Dint("X", external=True, choices={0: "none", 2_000_000: "many"})

    def write_n(n: object) -> None:
        with Rung():
            copy(x, n)

    logic, prop = _loop_program(lambda: Dint("N", min=0, max=50), write_n)
    _assert_refused(never(logic, prop), 2_000_000, ["N"])


def test_an_indexed_dint_count_names_its_pointer() -> None:
    dd = Block("DD", TagType.DINT, 1, 4)
    p = Int("P", external=True, min=1, max=4)

    def write_dd(_count: object) -> None:
        with Rung():
            copy(2_000_000, dd[2])

    logic, prop = _loop_program(lambda: dd[p], write_dd)
    result = never(logic, prop)
    assert isinstance(result, Intractable), result
    assert "forloop(DD[P])" in result.reason
    assert result.tags == ["P"]


def _assert_unbounded(result: object) -> None:
    assert isinstance(result, Intractable), result
    assert result.reason == "unbounded domain on N"
    assert any("read as a forloop count" in hint for hint in result.hints), result.hints


def test_a_count_too_wide_to_list_is_an_unbounded_domain() -> None:
    # 70001 values: more than the search lists for a value it needs whole.
    _assert_unbounded(never(*_loop_program(lambda: Dint("N", external=True, min=0, max=70000))))


def test_a_count_with_no_declared_domain_is_an_unbounded_domain() -> None:
    for kind in (Int, Dint):
        _assert_unbounded(never(*_loop_program(lambda kind=kind: kind("N", external=True))))


def test_a_count_at_the_cap_answers_and_one_past_it_is_refused() -> None:
    at = PROVE_FOR_ITERATION_CAP
    logic, prop = _loop_program(lambda: Dint("N", external=True, choices={3: "few", at: "cap"}))
    assert isinstance(never(logic, prop), Proven)
    logic, prop = _loop_program(
        lambda: Dint("N", external=True, choices={3: "few", at + 1: "past"})
    )
    _assert_refused(never(logic, prop), at + 1, ["N"])


def test_a_16_bit_count_is_never_capped() -> None:
    # -1 runs 65535 iterations, under the cap.
    logic, prop = _loop_program(lambda: Int("N", external=True, min=-1, max=1))
    assert isinstance(never(logic, prop), Counterexample)
    assert "_ForIterationsBeyondCap" not in compile_kernel(logic, proof_metadata=True).source


def test_reachable_states_and_check_lock_refuse_it_too(tmp_path: Path) -> None:
    logic, _prop = _loop_program(_huge_count)
    _assert_refused(reachable_states(logic, project=["Y3"]), _MAX_DINT, ["N"])
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


@pytest.mark.parametrize("kind", [Int, Dint], ids=["int", "dint"])
def test_a_called_body_that_counts_its_iterations_is_not_proven(kind: type) -> None:
    # A call guard's N != 0 split N at 0 alone, and the search took -1, 0 and 1
    # for every value: never(X == 2) came back Proven.
    logic, x = _counting_program(lambda: kind("N", external=True))
    _assert_unbounded(never(logic, x == 2))


@pytest.mark.parametrize("kind", [Int, Dint], ids=["int", "dint"])
def test_a_counting_body_with_a_declared_count_is_exact(kind: type) -> None:
    logic, x = _counting_program(lambda: kind("N", external=True, min=0, max=5))
    result = never(logic, x == 2)
    assert isinstance(result, Counterexample), result
    assert result.trace[-1].inputs["N"] == 2
    # A negative Int runs 65533..65535 iterations and a negative Dint none: never 2.
    logic, x = _counting_program(lambda: kind("N", external=True, min=-3, max=1))
    assert isinstance(never(logic, x == 2), Proven)


def _counting_program(count: Callable[[], object]) -> tuple[Program, Dint]:
    n = count()
    x = Dint("X")
    with Program(strict=False) as logic:
        with Rung():
            copy(0, x)
        with Rung():
            with forloop(n):
                call("work")
        with subroutine("work"):
            with Rung():
                math(x + 1, x)
    return logic, x


def _direct_counting_program(
    count: object, before: Callable[[], None] | None = None
) -> tuple[Program, Dint]:
    x = Dint("X")
    with Program(strict=False) as logic:
        if before is not None:
            before()
        with Rung():
            copy(0, x)
        with Rung():
            with forloop(count):
                math(x + 1, x)
    return logic, x


@pytest.mark.parametrize("kind", [Int, Dint, Word], ids=["int", "dint", "word"])
def test_an_unwritten_internal_count_is_not_held_at_its_default(kind: type) -> None:
    # An unwritten tag nothing compared was constant storage, held at 0: never(X == 2)
    # came back Proven.  It is free, as an unwritten tag a copy reads is.
    logic, x = _direct_counting_program(kind("N"))
    _assert_unbounded(never(logic, x == 2))
    logic, x = _direct_counting_program(kind("N", min=0, max=5))
    result = never(logic, x == 2)
    assert isinstance(result, Counterexample), result


def test_a_count_copied_from_an_unwritten_internal_tag_is_not_held_either() -> None:
    m = Int("M")
    n = Int("N")

    def write_n() -> None:
        with Rung():
            copy(m, n)

    logic, x = _direct_counting_program(n, write_n)
    result = never(logic, x == 2)
    assert isinstance(result, Intractable), result
    assert result.reason == "unbounded domain on M"
    assert any("read as a forloop count, through N" in hint for hint in result.hints), result.hints
