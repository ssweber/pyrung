"""how() tries only what a tag can hold.

A literal comes off a rung as written.  ``C == "AB"`` compares a CHAR to two characters, so
no scan can see it true, and a patch refuses "AB".  A candidate value is what the tag's store
makes of it, or nothing.  A run neither raises on it nor reports a state that no scan can reach.
"""

from __future__ import annotations

import pytest

from pyrung import Bool, Char, Int, Or, Program, Real, Word, copy, fill, latch, math, out, rung
from pyrung.core import PLC, Block, TagType
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pilot.skiff import _declared_domain
from pyrung.core.analysis.prove import _build_prior_facts
from pyrung.core.analysis.prove.classify import _condition_to_expr, _extract_value_domain
from pyrung.core.crossing import Affine, invert_affine_candidate
from pyrung.core.input_overrides import held_candidates, refuses_value
from pyrung.core.storage import StoreTransform

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
        with rung(condition):
            out(Y)
    return program


def _how(program: Program):
    return PLC(program, dt=0.01).how(Y)


def _held_at_the_goal(program: Program, name: str, default=None):
    """Return what *name* holds on the scan where how() reaches Y."""
    plan = _how(program)
    assert plan.reachable is True
    assert plan.fork.current_state.tags["Y"] is True
    return plan.fork.current_state.tags.get(name, default)


def _domain(program: Program, name: str) -> tuple:
    return _build_prior_facts(program).nondeterministic_dims[name]


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
def test_a_condition_no_held_value_meets_is_unreachable(condition) -> None:
    assert _how(_program(condition())).reachable is False


@pytest.mark.parametrize(
    "condition",
    [
        # Dropping the literal would leave only the default here.
        pytest.param(lambda: In > 3.5, id="above a fraction"),
        pytest.param(lambda: In >= 3.5, id="at or above a fraction"),
        pytest.param(lambda: In < 3.5, id="below a fraction"),
        pytest.param(lambda: In > -3.5, id="above a negative fraction"),
        pytest.param(lambda: In < -3.5, id="below a negative fraction"),
        pytest.param(lambda: In > 0.5, id="above a half"),
        pytest.param(lambda: In < -0.5, id="below a negative half"),
        pytest.param(lambda: In < 99999, id="below past an int"),
        pytest.param(lambda: In > -99999, id="above below an int"),
        pytest.param(lambda: W < 70000, id="below past a word"),
        pytest.param(lambda: Bounded > 3.5, id="bounded, above"),
        pytest.param(lambda: Or(In == "x", In == 7), id="a string or a number"),
    ],
)
def test_a_condition_a_held_value_meets_stays_reachable(condition) -> None:
    assert _how(_program(condition())).reachable is True


def test_a_real_past_its_range_is_met_by_the_inf_a_real_holds() -> None:
    """A store past float32 range holds Inf, and Inf is past the literal."""
    plc = PLC(program := _program(R >= 1e39), dt=0.01)
    plc.patch({R: float("inf")})
    plc.step()
    assert plc.state.tags["Y"] is True

    assert _how(program).reachable is True


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

    assert true_values & set(_domain(program, "C"))
    assert _held_at_the_goal(program, "C", "\x00") in true_values


def test_a_char_past_every_char_is_unreachable() -> None:
    assert _how(_program(C < "\x00")).reachable is False


def test_how_takes_the_other_branch_of_an_or() -> None:
    assert _how(_program(Or(C == "AB", Go))).reachable is True


def test_a_literal_no_tag_holds_blocks_its_rung_not_the_run() -> None:
    M = Bool("M")
    with Program() as program:
        with rung(C == "AB"):
            latch(M)
        with rung(M, Go):
            out(Y)

    assert _how(program).reachable is False


def _copy_into(words) -> None:
    copy(In, words[1])


def _add_five_into(words) -> None:
    math(In + 5, words[1])


def _fill_into(words) -> None:
    fill(In, words.select(1, 2))


@pytest.mark.parametrize(
    "writes, needs",
    [(_copy_into, -1), (_add_five_into, -6), (_fill_into, -1)],
    ids=["copy", "calc", "fill"],
)
def test_how_sets_a_source_what_it_can_hold_to_reach_a_wrapped_value(writes, needs) -> None:
    """An INT source would clamp 65535.  The way to a WORD's 65535 is -1."""
    words = Block("WW", TagType.WORD, 1, 2)
    with Program(strict=False) as program:
        with rung():
            writes(words)
        with rung(words[1] == 65535):
            out(Y)

    plan = _how(program)

    assert plan.status is PlanStatus.REACHED
    assert f"Set In={needs}." in str(plan)


@pytest.mark.parametrize(
    "storage, source, needs",
    [
        (StoreTransform("wrap", 0, 65535), StoreTransform("clamp", -32768, 32767), -1),
        (StoreTransform("wrap", 0, 65535), StoreTransform("clamp", 0, 65535), 65535),
        (StoreTransform("wrap", 0, 65535), None, 65535),
        (StoreTransform("wrap", 0, 65535), StoreTransform("clamp", 1, 9), 65535),
    ],
    ids=["a way it can hold", "already holds it", "not known", "no way it can hold"],
)
def test_a_wrap_candidate_prefers_one_the_source_can_hold(storage, source, needs) -> None:
    claim = Affine("S", storage=storage)
    assert invert_affine_candidate(claim, 65535, source) == needs


def test_a_value_carried_back_through_a_copy_is_what_the_source_holds() -> None:
    """The back-propagated 3.5 lands on an INT source as 3, and a replay holds that too."""
    D = Real("D")
    with Program() as program:
        with rung():
            copy(In, D)
        with rung(D > 3.5):
            out(Y)

    assert all(type(v) is int for v in _domain(program, "In"))
    assert _held_at_the_goal(program, "In") >= 4


@pytest.mark.parametrize(
    ("tag", "given"),
    [
        (R, -1),
        (R, 2),
        (R, 1.5),
        (C, "A"),
    ],
)
def test_a_candidate_a_real_holds_as_an_equal_value_keeps_its_form(tag, given) -> None:
    assert [repr(v) for v in held_candidates(tag, [given])] == [repr(given)]


@pytest.mark.parametrize(
    ("tag", "given", "stored"),
    [
        (In, 3.5, "3"),
        (In, 3.0, "3"),
        (In, True, "1"),
        (W, 7.0, "7"),
        (R, True, "1.0"),
        (In, 99999, "32767"),
        (W, -1, "65535"),
        (R, 1e39, "inf"),
        (C, "", repr("\x00")),
    ],
)
def test_a_candidate_the_store_would_change_is_mapped(tag, given, stored) -> None:
    assert [repr(v) for v in held_candidates(tag, [given])] == [stored]


@pytest.mark.parametrize(
    ("condition", "true_at"),
    [
        pytest.param(lambda: In > 3.0, lambda v: v > 3, id="above"),
        pytest.param(lambda: In == 3.0, lambda v: v == 3, id="at"),
        pytest.param(lambda: In >= 3.0, lambda v: v >= 3, id="at or above"),
        pytest.param(lambda: In < 3.0, lambda v: v < 3, id="below"),
    ],
)
def test_a_whole_float_on_an_int_is_the_int(condition, true_at) -> None:
    program = _program(condition())

    assert all(type(v) is int for v in _domain(program, "In"))
    witness = _held_at_the_goal(program, "In")
    assert type(witness) is int
    assert true_at(witness)


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
