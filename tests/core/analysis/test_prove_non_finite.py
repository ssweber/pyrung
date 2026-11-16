"""Which Real tags may hold NaN or Inf, and the facts of one (``prove/non_finite.py``)."""

from __future__ import annotations

from typing import Any

import pytest

from pyrung import (
    Block,
    Bool,
    Int,
    Program,
    Real,
    Rung,
    copy,
    math,
    out,
    pack_text,
    pack_words,
    receive,
)
from pyrung.core import PLC
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove import passes
from pyrung.core.analysis.prove.non_finite import non_finite_reals
from pyrung.core.expression import tan
from pyrung.core.tag import TagType


def _non_finite(program: Any) -> set[str]:
    graph = build_program_graph(program)
    return set(
        non_finite_reals(program, graph, frozenset(passes._collect_receive_dest_names(program)))
    )


def _with(*rungs: Any) -> Any:
    with Program(strict=False) as prog:
        for condition, write in rungs:
            with Rung(condition):
                write()
    return prog


GO = Bool("Go", external=True)
BIG = Real("Big")
OUT = Real("Result")
COPY = Real("Copied")


def test_a_math_past_the_real_range_may_store_inf() -> None:
    prog = _with((GO, lambda: copy(3.0e38, BIG)), (GO, lambda: math(BIG * 10, OUT)))
    assert _non_finite(prog) == {"Result"}


def test_a_math_that_stays_in_range_is_finite() -> None:
    raw = Int("Raw", external=True)
    scaled = Real("Scaled")
    prog = _with(
        (GO, lambda: math(raw * 0.1 + 32.0, scaled)), (GO, lambda: math(scaled * 1.8, OUT))
    )
    assert _non_finite(prog) == set()


def test_adding_a_constant_to_itself_never_reaches_inf() -> None:
    # An added 1.0 rounds away past 2 ** 53, long before the Real range, so the sum never reaches Inf.
    prog = _with((GO, lambda: math(OUT + 1.0, OUT)))
    assert _non_finite(prog) == set()


def test_scaling_itself_up_reaches_inf() -> None:
    prog = _with((GO, lambda: copy(1.0, OUT)), (GO, lambda: math(OUT * 2.0, OUT)))
    assert _non_finite(prog) == {"Result"}


@pytest.mark.parametrize(
    "formula",
    [
        lambda d: 1.0 / d,
        lambda d: d**-1,
        lambda d: d**0.5,
    ],
    ids=["division by a Real", "negative power", "fractional power"],
)
def test_a_formula_this_cannot_bound_may_store_inf(formula: Any) -> None:
    divisor = Real("Divisor", min=-1.0, max=1.0)
    prog = _with((GO, lambda: math(formula(divisor), OUT)))
    assert _non_finite(prog) == {"Result"}


def test_a_division_by_a_whole_number_is_finite() -> None:
    # A division by 0 is 0, and any other Int is at least 1 in size.
    divisor = Int("WholeDivisor", external=True)
    prog = _with((GO, lambda: math(3.0e38 / divisor, OUT)))
    assert _non_finite(prog) == set()


def test_a_copy_of_one_may_hold_one_too() -> None:
    prog = _with(
        (GO, lambda: copy(3.0e38, BIG)),
        (GO, lambda: math(BIG * 10, OUT)),
        (GO, lambda: copy(OUT, COPY)),
    )
    assert _non_finite(prog) == {"Result", "Copied"}


def test_words_packed_into_a_real_may_be_nan() -> None:
    words = Block("Words", TagType.INT, 1, 2)
    prog = _with((GO, lambda: pack_words(words.select(1, 2), OUT)))
    assert _non_finite(prog) == {"Result"}


def test_text_just_past_the_real_range_parses_to_inf() -> None:
    text = Block("Text", TagType.CHAR, 1, 13)
    parsed = Real("Parsed")
    with Program(strict=False) as prog:
        with Rung(GO):
            pack_text(text.select(1, 13), parsed)
        with Rung(parsed > 1.0):
            out(Bool("ParsedHigh"))
        with Rung(GO):
            copy(parsed, COPY)
    assert _non_finite(prog) == {"Parsed", "Copied"}

    plc = PLC(prog, dt=0.01)
    plc.patch({"Go": True, **{f"Text{i}": ch for i, ch in enumerate("3.40282352E38", start=1)}})
    plc.step()
    assert plc.current_state.tags["Parsed"] == float("inf")


def test_a_tangent_is_unbounded() -> None:
    # tan(45.553093477052) is about 1.6E+18, so its square times 1E+5 passes the Real range.
    angle = Real("Angle", external=True, min=0.0, max=100.0)
    prog = _with((GO, lambda: math(tan(angle) * tan(angle) * 1e5, OUT)))
    assert _non_finite(prog) == {"Result"}


def test_the_non_finite_set_holds_an_element_past_the_element_cap() -> None:
    """This checks only the set.  The classification still gives BigR5 a domain, because it
    counts no writer for it (roadmap 1b)."""
    big = Block("BigR", TagType.REAL, 1, 2000)
    pointer = Int("WritePointer", external=True)
    with Program(strict=False) as prog:
        with Rung(GO):
            copy(3.0e38, BIG)
        with Rung(Bool("Scale", external=True)):
            math(BIG * 10, OUT)
            copy(OUT, big[pointer])
        with Rung(big[5] > 1.0):
            out(Bool("Big5High"))
    assert "BigR5" in _non_finite(prog)


def test_a_real_the_outside_and_a_rung_both_write_has_no_domain() -> None:
    hmi = Real("HmiSetpoint", external=True)
    with Program(strict=False) as prog:
        with Rung(GO):
            copy(1.0, hmi)
        with Rung(hmi > 2.0):
            out(Bool("SetpointHigh"))
    ctx = passes._PassContext(program=prog, dt=0.01)
    prior = passes._run_prior_pipeline(ctx)
    assert "HmiSetpoint" not in prior.stateful_dims
    assert "HmiSetpoint" not in prior.nondeterministic_dims


def test_the_outside_and_a_peer_may_write_one() -> None:
    hmi = Real("HmiReal", external=True)
    bounded = Real("BoundedReal", external=True, min=0.0, max=10.0)
    payload = Real("Payload")

    def poll() -> None:
        receive(
            target="peer",
            remote_start="DF1",
            dest=payload,
            receiving=Bool("Receiving"),
            success=Bool("ReceiveOk"),
            error=Bool("ReceiveError"),
            exception_response=Int("ReceiveCode"),
        )

    prog = _with(
        (hmi > 1.0, lambda: out(Bool("HmiHigh"))),
        (bounded > 1.0, lambda: copy(bounded, COPY)),
        (GO, poll),
    )
    assert _non_finite(prog) == {"HmiReal", "Payload"}


def test_a_real_that_may_hold_nan_has_no_domain() -> None:
    nan = Real("MaybeNan")
    shown = Int("NanShown")
    with Program(strict=False) as prog:
        with Rung(GO):
            copy(3.0e38, BIG)
        with Rung(Bool("Scale", external=True)):
            math(BIG * 10, OUT)
            math(OUT - OUT, nan)
        with Rung(nan >= 0):
            out(Bool("NotBelow"))
        with Rung(Bool("Show", external=True)):
            copy(nan, shown)
    ctx = passes._PassContext(program=prog, dt=0.01)
    prior = passes._run_prior_pipeline(ctx)
    assert "MaybeNan" not in prior.stateful_dims
    assert "MaybeNan" in ctx._unclassified_written
    # Big holds 0 and 3.0E+38 and no more, so it keeps its domain.
    assert prior.stateful_dims["Big"] == (0.0, 3.0e38)


def test_check_makes_no_claim_on_a_nan_compared_with_itself() -> None:
    """A guard, since main passes it too.  A NaN is not equal to itself, so check() must not
    call ``x == x`` always true."""
    nan = Real("MaybeNan")
    with Program(strict=False) as prog:
        with Rung(GO):
            copy(3.0e38, BIG)
        with Rung(Bool("Scale", external=True)):
            math(BIG * 10, OUT)
            math(OUT - OUT, nan)
        with Rung(nan == nan):
            out(Bool("IsANumber"))
        with Rung(nan != nan):
            out(Bool("IsNan"))
    assert not prog.check(select={"CMP_ALWAYS_FALSE", "CMP_ALWAYS_TRUE"})


def test_an_input_real_copied_into_an_int_keeps_a_domain() -> None:
    # A copy of NaN into an Int stores 0, and of +/-Inf stores 32767 or -32768.
    temp = Real("DryerTemp", external=True)
    shown = Int("TempShown")
    with Program(strict=False) as prog:
        with Rung(GO):
            copy(temp, shown)
        with Rung(temp > 150.0):
            out(Bool("TooHot"))
        with Rung(shown >= Int("Setpoint", external=True)):
            out(Bool("AtSetpoint"))
    ctx = passes._PassContext(program=prog, dt=0.01)
    prior = passes._run_prior_pipeline(ctx)
    domain = prior.stateful_dims["TempShown"]
    assert {0, 32767, -32768} <= set(domain)

    plc = PLC(prog, dt=0.01)
    for value in (float("nan"), float("inf"), float("-inf")):
        plc.patch({"Go": True, "DryerTemp": value})
        plc.step()
        assert plc.current_state.tags["TempShown"] in domain
