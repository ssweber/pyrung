"""math() is CLICK's Math by name; calc() stays as a deprecated alias of it."""

from __future__ import annotations

import inspect
import pickle
import sys
import warnings

import pytest

from pyrung import PLC, Int, Program, Rung, calc, math
from pyrung.core.instruction import CalcInstruction


def _only_instruction(program: Program) -> CalcInstruction:
    (rung,) = program.rungs
    (instruction,) = rung._instructions
    assert isinstance(instruction, CalcInstruction)
    return instruction


def test_math_stores_the_expression_and_returns_the_destination() -> None:
    a, result = Int("A"), Int("Result")
    with Program(strict=False) as logic:
        with Rung():
            returned = math(a + 2, result)
    assert returned is result
    plc = PLC(logic)
    plc.patch({"A": 5})
    plc.step()
    assert plc.state.tags["Result"] == 7


def test_math_records_the_line_that_calls_it() -> None:
    with Program(strict=False) as logic:
        with Rung():
            line = sys._getframe().f_lineno + 1
            math(Int("A") + 1, Int("A"))
    instruction = _only_instruction(logic)
    assert instruction.source_file == __file__
    assert instruction.source_line == line


def test_calc_warns_at_the_line_that_calls_it_and_builds_the_same_instruction() -> None:
    with Program(strict=False) as logic:
        with Rung():
            with pytest.warns(FutureWarning, match=r"calc\(\) is now math\(\)") as caught:
                line = sys._getframe().f_lineno + 1
                calc(Int("A") + 1, Int("A"))
    (warning,) = caught.list
    assert warning.filename == __file__
    assert warning.lineno == line
    instruction = _only_instruction(logic)
    assert instruction.source_file == __file__
    assert instruction.source_line == line


def test_math_has_the_signature_calc_had() -> None:
    assert str(inspect.signature(math)) == str(inspect.signature(calc))
    assert math.__name__ == "math"


@pytest.mark.parametrize(
    ("name", "hint"),
    [
        ("sqrt", "from pyrung.core import sqrt"),
        ("pi", "from pyrung.core import PI"),
        ("isclose", "import math as pymath"),
    ],
)
def test_a_standard_math_name_on_pyrungs_math_says_what_to_use(name: str, hint: str) -> None:
    with pytest.raises(AttributeError) as raised:
        getattr(math, name)
    message = str(raised.value)
    assert hint in message
    assert "hides the standard math module" in message
    assert message.isascii()


def test_a_private_name_is_a_plain_attribute_error() -> None:
    with pytest.raises(AttributeError) as raised:
        math._no_such_name  # noqa: B018
    assert str(raised.value) == "_no_such_name"
    assert not hasattr(math, "__wrapped__")


def test_math_pickles_as_itself() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert pickle.loads(pickle.dumps(math)) is math
