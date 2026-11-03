"""how() and never() try only what a tag can hold.

A literal comes off a rung as written.  ``C == "AB"`` compares a CHAR to two characters, so
no scan can see it true, and a patch refuses "AB".  A candidate value is what the tag's store
makes of it, or nothing: a run neither raises on it nor reports a state no scan can reach.
"""

from __future__ import annotations

import pytest

from pyrung import Bool, Char, Int, Or, Program, Real, Rung, Word, copy, latch, out
from pyrung.core import PLC
from pyrung.core.analysis.pilot.skiff import _declared_domain
from pyrung.core.analysis.prove import Counterexample, Proven, never
from pyrung.core.analysis.prove.classify import _condition_to_expr, _extract_value_domain
from pyrung.core.input_overrides import held_candidates, refuses_value

Y = Bool("Y")
Go = Bool("Go", external=True)
C = Char("C", external=True)
TwoCharChoice = Char("TwoCharChoice", external=True, choices={"AB": "two", "A": "one"})
In = Int("In", external=True)
Bounded = Int("Bounded", external=True, min=0, max=10)
W = Word("W", external=True)
R = Real("R", external=True)


def _program(condition) -> Program:
    with Program() as program:
        with Rung(condition):
            out(Y)
    return program


def _how(program: Program):
    return PLC(program, dt=0.01).how(Y)


@pytest.mark.parametrize(
    "condition",
    [
        pytest.param(lambda: C == "AB", id="two chars on a char"),
        pytest.param(lambda: TwoCharChoice == "AB", id="two chars in its choices"),
        pytest.param(lambda: C == 65, id="a number on a char"),
        pytest.param(lambda: In == "x", id="a string on an int"),
        pytest.param(lambda: In == 3.5, id="a fraction on an int"),
        pytest.param(lambda: Bounded == 3.5, id="a fraction on a bounded int"),
        pytest.param(lambda: In == 99999, id="past an int"),
        pytest.param(lambda: In >= 99999, id="at or past an int"),
        pytest.param(lambda: In < -99999, id="below an int"),
        pytest.param(lambda: R == 1e39, id="past a real, equal"),
    ],
)
def test_a_condition_no_held_value_meets_is_proven_and_unreachable(condition) -> None:
    program = _program(condition())

    assert isinstance(never(program, Y), Proven)
    assert _how(program).reachable is False


@pytest.mark.parametrize(
    ("condition", "tag", "true_values"),
    [
        # Dropping the literal would leave only the default here: a false Proven.
        pytest.param(lambda: In > 3.5, "In", range(4, 32768), id="above a fraction"),
        pytest.param(lambda: In >= 3.5, "In", range(4, 32768), id="at or above a fraction"),
        pytest.param(lambda: In < 3.5, "In", range(-32768, 4), id="below a fraction"),
        pytest.param(lambda: In > -3.5, "In", range(-3, 32768), id="above a negative fraction"),
        pytest.param(lambda: In < -3.5, "In", range(-32768, -3), id="below a negative fraction"),
        pytest.param(lambda: In > 0.5, "In", range(1, 32768), id="above a half"),
        pytest.param(lambda: In < -0.5, "In", range(-32768, 0), id="below a negative half"),
        pytest.param(lambda: In < 99999, "In", range(-32768, 32768), id="below past an int"),
        pytest.param(lambda: In > -99999, "In", range(-32768, 32768), id="above below an int"),
        pytest.param(lambda: W < 70000, "W", range(0, 65536), id="below past a word"),
        pytest.param(lambda: Bounded > 3.5, "Bounded", range(4, 11), id="bounded, above"),
        pytest.param(lambda: Or(In == "x", In == 7), "In", [7], id="a string or a number"),
    ],
)
def test_a_condition_a_held_value_meets_stays_reachable(condition, tag, true_values) -> None:
    program = _program(condition())

    result = never(program, Y)
    assert isinstance(result, Counterexample)
    assert result.trace[-1].inputs[tag] in true_values
    assert _how(program).reachable is True


def test_a_real_past_its_range_is_met_by_the_inf_a_real_holds() -> None:
    """A store past float32 range holds Inf, and Inf is past the literal: Proven would be false."""
    plc = PLC(program := _program(R >= 1e39), dt=0.01)
    plc.patch({R: float("inf")})
    plc.step()
    assert plc.state.tags["Y"] is True

    result = never(program, Y)
    assert isinstance(result, Counterexample)
    assert result.trace[-1].inputs["R"] == float("inf")
    assert _how(program).reachable is True


@pytest.mark.parametrize(
    "condition",
    [
        pytest.param(lambda: R <= -1e39, id="below"),
        pytest.param(lambda: R < 1e39, id="any finite is below"),
    ],
)
def test_a_real_range_literal_keeps_both_sides(condition) -> None:
    assert isinstance(never(_program(condition()), Y), Counterexample)


@pytest.mark.parametrize(
    ("condition", "true_values"),
    [
        pytest.param(lambda: C > "A", {"B"}, id="above a char"),
        pytest.param(lambda: C >= "B", {"B", "C"}, id="at or above a char"),
        pytest.param(lambda: C < "A", {"@", "\x00"}, id="below a char"),
        pytest.param(lambda: C > "AB", {"B"}, id="above two chars"),
        pytest.param(lambda: C < "AB", {"@", "A", "\x00"}, id="below two chars"),
        pytest.param(lambda: Or(C == "AB", C > "A"), {"B"}, id="two chars or above a char"),
    ],
)
def test_a_char_compared_by_order_has_neighbours(condition, true_values) -> None:
    program = _program(condition())

    result = never(program, Y)
    assert isinstance(result, Counterexample)
    assert result.trace[-1].inputs.get("C", "\x00") in true_values


def test_a_char_past_every_char_is_proven() -> None:
    assert isinstance(never(_program(C < "\x00"), Y), Proven)


def test_how_takes_the_other_branch_of_an_or() -> None:
    assert _how(_program(Or(C == "AB", Go))).reachable is True


def test_a_literal_no_tag_holds_blocks_its_rung_not_the_run() -> None:
    M = Bool("M")
    with Program() as program:
        with Rung(C == "AB"):
            latch(M)
        with Rung(M, Go):
            out(Y)

    assert isinstance(never(program, Y), Proven)
    assert _how(program).reachable is False


def test_a_value_carried_back_through_a_copy_is_what_the_source_holds() -> None:
    """The back-propagated 3.5 lands on an INT source as 3: a replay holds that too."""
    D = Real("D")
    with Program() as program:
        with Rung():
            copy(In, D)
        with Rung(D > 3.5):
            out(Y)

    result = never(program, Y)
    assert isinstance(result, Counterexample)
    assert result.trace[-1].inputs["In"] >= 4


@pytest.mark.parametrize(
    ("tag", "given"),
    [
        (R, -1),
        (R, 2),
        (R, 1.5),
        (In, 3.0),
        (In, True),
        (W, 7.0),
        (C, "A"),
    ],
)
def test_a_candidate_the_tag_holds_as_an_equal_value_keeps_its_form(tag, given) -> None:
    assert [repr(v) for v in held_candidates(tag, [given])] == [repr(given)]


@pytest.mark.parametrize(
    ("tag", "given", "stored"),
    [
        (In, 3.5, "3"),
        (In, 99999, "32767"),
        (W, -1, "65535"),
        (R, 1e39, "inf"),
        (C, "", repr("\x00")),
    ],
)
def test_a_candidate_the_store_would_change_is_mapped(tag, given, stored) -> None:
    assert [repr(v) for v in held_candidates(tag, [given])] == [stored]


def test_a_witness_on_a_real_keeps_the_form_of_its_literal() -> None:
    temp = Real("FormTemp", choices={-1: "open", 5: "fine"})
    hit = Bool("FormHit")
    with Program(strict=False) as program:
        with Rung(temp == -1):
            out(hit)
    result = never(program, hit)
    assert isinstance(result, Counterexample)
    witness = [step.inputs["FormTemp"] for step in result.trace if "FormTemp" in step.inputs]
    assert [repr(v) for v in witness] == ["-1"]


def test_a_declared_domain_on_a_real_keeps_the_form_of_its_choices() -> None:
    temp = Real("FormTemp", choices={-1: "open", 5: "fine"})
    assert repr(_declared_domain(temp)) == "(-1, 5)"


def test_held_candidates_is_what_the_store_makes_of_each_value() -> None:
    assert held_candidates(In, [3.5, -3.5, 99999, -99999, True, "x"]) == {3, -3, 32767, -32768, 1}
    assert held_candidates(W, [70000, -1]) == {4464, 65535}
    assert held_candidates(C, ["A", "", "AB", 65]) == {"A", "\x00"}
    assert held_candidates(R, [1.5, 1e39, -1e39, "x"]) == {1.5, float("inf"), -float("inf")}


@pytest.mark.parametrize(
    ("tag", "value", "refused"),
    [
        (C, "AB", True),
        (C, 65, True),
        (C, "A", False),
        (C, "", False),
        (In, "x", True),
        (In, 3.5, False),
        (In, 99999, False),
        (R, 1e39, True),
        (R, float("inf"), False),
    ],
)
def test_refuses_value_answers_as_a_patch_does(tag, value, refused) -> None:
    assert refuses_value(tag, value) is refused
    plc = PLC(_program(tag == tag.default), dt=0.01)
    if refused:
        with pytest.raises(ValueError):
            plc.patch({tag: value})
    else:
        plc.patch({tag: value})


def test_a_declared_domain_lists_only_the_choices_a_tag_holds() -> None:
    assert _declared_domain(TwoCharChoice) == ("A",)
    assert _declared_domain(Char("OnlyLong", choices={"AB": "two"})) is None


def test_a_value_domain_lists_only_what_the_tag_holds() -> None:
    program = _program(In > 3.5)
    exprs = [_condition_to_expr(program.rungs[0]._conditions[0])]
    assert _extract_value_domain("In", In, exprs) == (0, 2, 3, 4)
