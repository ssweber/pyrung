"""Each instruction refuses an operand when the program is built, or writes a result with it.

The cases come from each instruction's ``_operands`` and ``_operand_rules``.  For every
field that they name, the sweep tries every operand in ``OPERANDS``.

- If the parse refuses the operand, the error names the call.  When the field's
  declaration refuses the operand, the error has the declaration's words.
- If the parse accepts the operand, at least one run with the data in ``DATA`` must
  write a tag.

To cover an instruction that declares operands, add its builder to ``BUILDERS``.  To
sweep another form of the same instruction, such as ``copy()`` with ``convert=``, add
it to ``VARIANTS``.  To cover a rule, add cases to ``RULE_CASES``.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import pytest

from pyrung import (
    PLC,
    Int,
    Program,
    Timer,
    copy,
    fill,
    forloop,
    immediate,
    latch,
    lsh,
    math,
    named_array,
    out,
    pack_bits,
    pack_text,
    pack_words,
    randint,
    run_enabled_function,
    run_function,
    rung,
    search,
    shift,
    sqrt,
    unpack_to_bits,
    unpack_to_words,
)
from pyrung.click import ClickBlocks
from pyrung.core import to_ascii, to_binary, to_text, to_value
from pyrung.core.instruction import (
    CalcInstruction,
    CopyInstruction,
    EnabledFunctionCallInstruction,
    FillInstruction,
    ForLoopInstruction,
    FunctionCallInstruction,
    Instruction,
    LatchInstruction,
    OutInstruction,
    PackBitsInstruction,
    PackTextInstruction,
    PackWordsInstruction,
    SearchInstruction,
    ShiftInstruction,
    UnpackToBitsInstruction,
    UnpackToWordsInstruction,
)
from pyrung.core.instruction.operands import EachOperand
from pyrung.core.rung import Rung

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

# The refusal of a coil's target, which does not start with the call.
_COIL_REFUSAL = "Expected Tag, BlockRange from .select(), or immediate(...) wrapper, got "

# The data that each scan runs with.  Whether an operand writes a result can depend on
# the data, as for a Char tag in math().
# - In the first two sets, DS100, DS101 and DS102 point at the tags 1, 2 and 5, and
#   TXT5 holds a digit.
# - The second set also puts digits in TXT1 and TXT2, and a count in C1, DS1, DS2, DD1,
#   DH1, DF1 and YD1.
# - The third set points below the bank, which sets the address-error flag.
# - The fourth set points past the bank.  A write there is dropped with no flag.
DATA: tuple[dict[Any, Any], ...] = (
    {ds[100]: 1, ds[101]: 2, ds[102]: 5, txt[5]: "7"},
    {
        **{ds[100]: 1, ds[101]: 2, ds[102]: 5, txt[5]: "7", txt[1]: "7", txt[2]: "3"},
        **{c[1]: True, ds[1]: 3, ds[2]: 4, dd[1]: 3, dh[1]: 3, df[1]: 2.0, yd[1]: 3},
    },
    {ds[100]: 0, ds[101]: 0, ds[102]: 0},
    {ds[100]: 9000, ds[101]: 9001, ds[102]: 9000},
)

# One operand of each kind that a field might be given.
OPERANDS: dict[str, Callable[[], Any]] = {
    "list": lambda: [c[1], c[2]],
    "tuple": lambda: (ds[1], ds[2]),
    "dict": lambda: {},
    "int constant": lambda: 5,
    "real constant": lambda: 1.5,
    "whole real constant": lambda: 5.0,
    "True": lambda: True,
    "text constant": lambda: "A",
    "integer text constant": lambda: "5",
    "real text constant": lambda: "1.5",
    "None": lambda: None,
    "expression": lambda: ds[1] + 1,
    "expression with None in it": lambda: ds[1] + None,
    "expression with a list in it": lambda: lsh([c[1]], 1),
    "expression with a tuple product": lambda: ds[1] * (1, 2),
    "expression with a condition in it": lambda: sqrt(ds[1] + (ds[2] > 1)),
    # The scan never reads the bounds of a randint() with a Real bound.  The draw is 0.
    "randint with a Real bound": lambda: randint(df[1], ds[1] + None),
    "condition": lambda: ds[1] > 5,
    "structure": lambda: Timer[1],
    "Bool tag": lambda: c[1],
    "Int tag": lambda: ds[1],
    "Dint tag": lambda: dd[1],
    "Word tag": lambda: dh[1],
    "Real tag": lambda: df[1],
    "Char tag": lambda: txt[1],
    "Char tag holding a digit": lambda: txt[5],
    "Bool pointer": lambda: c[ds[100]],
    "Int pointer": lambda: ds[ds[100]],
    "Char pointer": lambda: txt[ds[100]],
    "Char pointer to a digit": lambda: txt[ds[102]],
    "Int pointer expression": lambda: ds[ds[100] + 1],
    "Word tag in immediate()": lambda: immediate(yd[1]),
    "Bool range": lambda: c.select(1, 2),
    "Int range": lambda: ds.select(1, 2),
    "Dint range": lambda: dd.select(1, 2),
    "Word range": lambda: dh.select(1, 2),
    "Real range": lambda: df.select(1, 2),
    "Char range": lambda: txt.select(1, 2),
    "Int range of 3": lambda: ds.select(1, 3),
    "Bool range with pointers": lambda: c.select(ds[100], ds[101]),
    "Int range with pointers": lambda: ds.select(ds[100], ds[101]),
    "Bool range in immediate()": lambda: immediate(y.select(1, 2)),
}


@named_array(Int, count=2, stride=2)
class Words:
    lo = 0
    hi = 0


def _shift(bit_range: Any) -> None:
    shift(bit_range).clock(x[1]).reset(x[2])


def _forloop(count: Any) -> None:
    with forloop(count):
        out(c[7])


def _search(found: Any, result: Any) -> None:
    search(ds.select(1, 2) >= 0, result=result, found=found)


def _returns_r(*_args: Any, **_kwargs: Any) -> dict[str, str]:
    # A function's result of "1" stores into a tag of each type, a Char tag too.
    return {"r": "1"}


def _run_function(outs: Any) -> None:
    run_function(_returns_r, ins={"a": ds[1]}, outs={"r": outs})


def _run_enabled_function(outs: Any) -> None:
    run_enabled_function(_returns_r, ins={"a": ds[1]}, outs={"r": outs})


@dataclass(frozen=True)
class Builder:
    """The call that builds an instruction, and an operand that it accepts for each argument.

    ``accepted`` maps each argument to a kind in ``OPERANDS``.  ``build`` takes the
    arguments as keywords.  ``name`` tells a variant from the plain call in a case id.
    With ``writes`` False, an operand that the parse accepts must run with some data,
    but need not write.
    """

    call: str
    build: Callable[..., Any]
    accepted: dict[str, str]
    name: str = ""
    writes: bool = True


BUILDERS: dict[type[Instruction], Builder] = {
    PackBitsInstruction: Builder(
        "pack_bits()", pack_bits, {"bit_block": "Bool range", "dest": "Word tag"}
    ),
    PackWordsInstruction: Builder(
        "pack_words()", pack_words, {"word_block": "Int range", "dest": "Dint tag"}
    ),
    PackTextInstruction: Builder(
        "pack_text()", pack_text, {"source_range": "Char range", "dest": "Int tag"}
    ),
    UnpackToBitsInstruction: Builder(
        "unpack_to_bits()", unpack_to_bits, {"source": "Word tag", "bit_block": "Bool range"}
    ),
    UnpackToWordsInstruction: Builder(
        "unpack_to_words()", unpack_to_words, {"source": "Dint tag", "word_block": "Int range"}
    ),
    ShiftInstruction: Builder("shift()", _shift, {"bit_range": "Bool range"}),
    CopyInstruction: Builder("copy()", copy, {"source": "Int tag", "target": "Int tag"}),
    FillInstruction: Builder("fill()", fill, {"value": "Int tag", "dest": "Int range"}),
    CalcInstruction: Builder("math()", math, {"expression": "Int tag", "dest": "Int tag"}),
    SearchInstruction: Builder("search()", _search, {"found": "Bool tag", "result": "Int tag"}),
    ForLoopInstruction: Builder("forloop()", _forloop, {"count": "int constant"}),
    FunctionCallInstruction: Builder("run_function()", _run_function, {"outs": "Int tag"}),
    EnabledFunctionCallInstruction: Builder(
        "run_enabled_function()", _run_enabled_function, {"outs": "Int tag"}
    ),
    OutInstruction: Builder("out()", out, {"target": "Bool tag"}),
    LatchInstruction: Builder("latch()", latch, {"target": "Bool tag"}),
}


def _copy_with(converter: Any) -> Callable[..., None]:
    def build(source: Any, target: Any) -> None:
        copy(source, target, convert=converter)

    return build


# Each converter of copy() reads its source in its own way.  The check does not know
# which sources and targets a converter can convert.  On the others, the converter sets
# a fault flag and writes nothing.  copy(ds[1], ds[3], convert=to_value) is one of them.
# So a case of these builders must run, but need not write.
VARIANTS: list[tuple[type[Instruction], Builder]] = [
    (CopyInstruction, Builder("copy()", _copy_with(converter), accepted, name, writes=False))
    for name, converter, accepted in (
        ("copy(to_value)", to_value, {"source": "Char tag holding a digit", "target": "Int tag"}),
        ("copy(to_ascii)", to_ascii, {"source": "Char tag", "target": "Int tag"}),
        ("copy(to_text())", to_text(), {"source": "Int tag", "target": "Char tag"}),
        ("copy(to_binary)", to_binary, {"source": "Int tag", "target": "Char tag"}),
    )
]

# Operands that the parse refuses because CLICK refuses them, though pyrung could write
# a result with them.  CLICK's For refuses a count with a decimal point.  CLICK's Math,
# Out and Set refuse a TXT.
REFUSED_AS_ON_CLICK = {
    (ForLoopInstruction, "count", "real constant"),
    (ForLoopInstruction, "count", "whole real constant"),
    *(
        (cls, arg, kind)
        for cls, arg in (
            (CalcInstruction, "dest"),
            (OutInstruction, "target"),
            (LatchInstruction, "target"),
        )
        for kind in ("Char tag", "Char tag holding a digit")
    ),
    (OutInstruction, "target", "Char range"),
    (LatchInstruction, "target", "Char range"),
}

# Operands that the parse accepts, and that write nothing with any data.  The parse does
# not read what a count evaluates to.  A randint() with a Real bound always draws 0, and
# a count of 0 runs no iteration.
WRITE_NOTHING = {
    (ForLoopInstruction, "count", "randint with a Real bound"),
}

# Cases for the rules in ``_operand_rules``.  Each case gives the arguments that differ
# from the builder's accepted ones.
RULE_CASES: list[tuple[type[Instruction], dict[str, Callable[[], Any]]]] = [
    (PackBitsInstruction, {"bit_block": lambda: c.select(1, 16)}),
    (PackBitsInstruction, {"bit_block": lambda: c.select(1, 17)}),
    (PackBitsInstruction, {"bit_block": lambda: immediate(x.select(1, 116))}),
    (PackBitsInstruction, {"bit_block": lambda: c.select(1, 32), "dest": lambda: dd[1]}),
    (PackBitsInstruction, {"bit_block": lambda: c.select(1, 33), "dest": lambda: dd[1]}),
    (UnpackToBitsInstruction, {"bit_block": lambda: c.select(1, 17)}),
    (UnpackToBitsInstruction, {"bit_block": lambda: immediate(y.select(1, 116))}),
    (UnpackToBitsInstruction, {"source": lambda: df[1], "bit_block": lambda: c.select(1, 32)}),
    (CopyInstruction, {"source": lambda: [c[1]], "target": lambda: c[3]}),
    (CopyInstruction, {"source": lambda: Timer[1], "target": lambda: txt[3]}),
    (CopyInstruction, {"source": lambda: [c[1]], "target": lambda: dd[3]}),
    (CopyInstruction, {"source": lambda: (1, 2), "target": lambda: ds[ds[100]]}),
    (FillInstruction, {"value": lambda: [c[1]], "dest": lambda: c.select(3, 4)}),
    (FillInstruction, {"value": lambda: {}, "dest": lambda: df.select(3, 4)}),
    (CopyInstruction, {"source": lambda: ds[1] * (1, 2), "target": lambda: c[3]}),
    (CopyInstruction, {"source": lambda: ds[1] * [c[1]], "target": lambda: df[3]}),
    (FillInstruction, {"value": lambda: ds[1] * (1, 2), "dest": lambda: txt.select(3, 4)}),
    (FillInstruction, {"value": lambda: [1], "dest": lambda: ds.select(ds[100], ds[101])}),
    (CalcInstruction, {"expression": lambda: ds.select(1, 2), "dest": lambda: c[3]}),
    (CalcInstruction, {"expression": lambda: "A", "dest": lambda: dh[3]}),
    (CalcInstruction, {"expression": lambda: "-5", "dest": lambda: dh[3]}),
    (CalcInstruction, {"expression": lambda: "1.5", "dest": lambda: df[3]}),
    (CalcInstruction, {"expression": lambda: "1.5", "dest": lambda: dd[3]}),
    (CalcInstruction, {"expression": lambda: "1e3", "dest": lambda: df[3]}),
    (CalcInstruction, {"expression": lambda: txt[1], "dest": lambda: df[3]}),
    (CalcInstruction, {"expression": lambda: ds[1] * (1, 2), "dest": lambda: ds[3]}),
    (OutInstruction, {"target": lambda: c[3]}),
    (LatchInstruction, {"target": lambda: c[3]}),
]

# Cases that the rules refuse because CLICK refuses them.  pyrung could store a number or
# a bit in the TXT.
RULE_CASES_AS_ON_CLICK: list[tuple[type[Instruction], dict[str, Callable[[], Any]]]] = [
    (CalcInstruction, {"expression": lambda: "A", "dest": lambda: txt[3]}),
    (CalcInstruction, {"expression": lambda: ds[1] * (1, 2), "dest": lambda: txt[3]}),
    (OutInstruction, {"target": lambda: txt[3]}),
    (LatchInstruction, {"target": lambda: txt[3]}),
]


def _declaring_classes() -> list[type[Instruction]]:
    """Return pyrung's instruction classes that declare operands or operand rules.

    A test file may define a subclass of its own.  The subclass stays in
    ``__subclasses__()`` after that file runs, so the walk skips any class from outside
    pyrung.
    """
    found: set[type[Instruction]] = set()
    pending = list(Instruction.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if not cls.__module__.startswith("pyrung."):
            continue
        if cls._operands or cls._operand_rules:
            found.add(cls)
    return sorted(found, key=lambda cls: cls.__name__)


def _args(cls: type[Instruction]) -> list[str]:
    """Return the builder's argument for each field that the class checks."""
    names = dict.fromkeys(field for rule in cls._operand_rules for field in rule.fields)
    names.update(dict.fromkeys(cls._operands))
    return [(cls._operands[name].arg if name in cls._operands else "") or name for name in names]


def _cases() -> list[Any]:
    declaring = set(_declaring_classes())
    cases = []
    for cls, builder in [*BUILDERS.items(), *VARIANTS]:
        if cls not in declaring:
            continue
        for arg in _args(cls):
            for kind in OPERANDS:
                case_id = f"{builder.name or builder.call}-{arg}-{kind}"
                cases.append(pytest.param(cls, builder, arg, kind, id=case_id))
    return cases


def _build(builder: Builder, args: dict[str, Callable[[], Any]]) -> Program:
    makers = {name: OPERANDS[kind] for name, kind in builder.accepted.items()}
    makers.update(args)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with Program(strict=False) as program:
            with rung(x[3]):
                builder.build(**{name: make() for name, make in makers.items()})
    return program


def _instructions(items: Iterable[Any]) -> Iterator[Instruction]:
    """Yield each instruction in *items*, and each one in a branch or a loop body."""
    for item in items:
        if isinstance(item, Rung):
            yield from _instructions(item._execution_items)
        else:
            yield item
            yield from _instructions(getattr(item, "body", ()))


@contextmanager
def _recording_writes(program: Program) -> Iterator[set[str]]:
    """Collect the name of each tag that an instruction of *program* writes.

    A fault flag is not collected.  An instruction's write is pending until the scan
    ends, so the pending writes show what its execute() wrote.
    """
    written: set[str] = set()
    instructions = list(_instructions(program.rungs))

    def watch(execute: Callable[[Any, bool], None]) -> Callable[[Any, bool], None]:
        def watched(ctx: Any, enabled: bool) -> None:
            before = dict(ctx._tags_pending)
            execute(ctx, enabled)
            for name, value in ctx._tags_pending.items():
                if not name.startswith("fault.") and (name not in before or before[name] != value):
                    written.add(name)

        return watched

    for instruction in instructions:
        instruction.execute = watch(instruction.execute)  # ty: ignore[invalid-assignment]
    try:
        yield written
    finally:
        for instruction in instructions:
            del instruction.execute


def _run(program: Program, data: dict[Any, Any]) -> set[str]:
    """Step *program* with *data*, then run it for 0.3 s, and return the tags it wrote."""
    with warnings.catch_warnings(), _recording_writes(program) as written:
        warnings.simplefilter("ignore")
        plc = PLC(program)
        for inputs in ({x[3]: True}, {x[3]: True, x[1]: True}, {x[3]: False, x[2]: True}):
            plc.patch({**inputs, **data})
            plc.step()
        plc.patch({x[3]: True, **data})
        plc.run_for(0.3)
    return set(written)


def _writes_with_some_data(program: Program, *, writes: bool = True) -> None:
    """Return if the program writes a tag with one set of ``DATA``.  Otherwise fail.

    With *writes* False, return if the program runs with one set.
    """
    outcomes = []
    for data in DATA:
        try:
            if _run(program, data) or not writes:
                return
        except Exception as error:  # noqa: BLE001 - the next set of data may write
            outcomes.append(f"{type(error).__name__}: {error}")
        else:
            outcomes.append("wrote nothing")
    pytest.fail(f"No set of data writes a tag: {outcomes}")


def _declared_refusal(
    cls: type[Instruction], builder: Builder, args: dict[str, Callable[[], Any]]
) -> str | None:
    """Return the words of the refusal that the declaration of a field in *args* gives.

    Return None if no declaration refuses its value.
    """
    for field, operand in cls._operands.items():
        arg = operand.arg or field
        if arg in args:
            value = args[arg]()
            if isinstance(operand, EachOperand):
                # The function builders pass the operand as outs={"r": ...}.
                value = {"r": value}
            try:
                operand.parse(builder.call, arg, value)
            except (TypeError, ValueError) as error:
                return str(error)
    return None


def _check_case(
    builder: Builder,
    cls: type[Instruction],
    args: dict[str, Callable[[], Any]],
    *,
    refused_as_on_click: bool = False,
    writes: bool = True,
) -> None:
    """Refuse the operands at build with the parse's words, or write a result with them.

    With *refused_as_on_click*, the operands must be refused.
    """
    try:
        program = _build(builder, args)
    except (TypeError, ValueError) as refusal:
        message = str(refusal)
    else:
        assert not refused_as_on_click, "the parse accepts the operand"
        _writes_with_some_data(program, writes=writes and builder.writes)
        return
    assert message.isascii()
    assert message.startswith(f"{builder.call} ") or message.startswith(_COIL_REFUSAL), message
    declared = _declared_refusal(cls, builder, args)
    # copy() and fill() refuse some operands with words of their own before they parse
    # their fields, such as a missing source or a range as copy()'s target.
    if declared is not None and builder.call not in ("copy()", "fill()"):
        assert message == declared


def test_every_instruction_that_checks_operands_has_a_builder() -> None:
    missing = [cls.__name__ for cls in _declaring_classes() if cls not in BUILDERS]
    assert missing == []


@pytest.mark.parametrize(("cls", "builder", "arg", "kind"), _cases())
def test_an_operand_is_refused_at_build_or_writes(
    cls: type[Instruction],
    builder: Builder,
    arg: str,
    kind: str,
) -> None:
    _check_case(
        builder,
        cls,
        {arg: OPERANDS[kind]},
        refused_as_on_click=(cls, arg, kind) in REFUSED_AS_ON_CLICK,
        writes=(cls, arg, kind) not in WRITE_NOTHING,
    )


@pytest.mark.parametrize(
    ("cls", "args"),
    RULE_CASES,
    ids=[f"{BUILDERS[cls].call}-{i}" for i, (cls, _) in enumerate(RULE_CASES)],
)
def test_a_rule_refuses_at_build_or_writes(
    cls: type[Instruction], args: dict[str, Callable[[], Any]]
) -> None:
    _check_case(BUILDERS[cls], cls, args)


@pytest.mark.parametrize(
    ("cls", "args"),
    RULE_CASES_AS_ON_CLICK,
    ids=[f"{BUILDERS[cls].call}-{i}" for i, (cls, _) in enumerate(RULE_CASES_AS_ON_CLICK)],
)
def test_a_rule_refuses_what_click_refuses(
    cls: type[Instruction], args: dict[str, Callable[[], Any]]
) -> None:
    _check_case(BUILDERS[cls], cls, args, refused_as_on_click=True)


def test_every_rule_has_a_refused_case_and_a_running_case() -> None:
    """Each class's rules see at least one case that builds and one that the rule refuses."""
    outcomes: dict[type[Instruction], set[bool]] = {}
    for cls, args in [*RULE_CASES, *RULE_CASES_AS_ON_CLICK]:
        try:
            _build(BUILDERS[cls], args)
        except (TypeError, ValueError):
            outcomes.setdefault(cls, set()).add(False)
        else:
            outcomes.setdefault(cls, set()).add(True)
    with_rules = [cls for cls in _declaring_classes() if cls._operand_rules]
    assert {cls: outcomes.get(cls, set()) for cls in with_rules} == {
        cls: {True, False} for cls in with_rules
    }


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (
            lambda: pack_bits(c.select(1, 17), dh[1]),
            "pack_bits() got 17 bits as bit_block, and DH1 holds 16 bits. Pass at most 16 "
            "bits, or a Dint or Real tag as dest.",
        ),
        (
            lambda: pack_bits(immediate(x.select(1, 116)), dd[1]),
            "pack_bits() got 48 bits as bit_block, and DD1 holds 32 bits. Pass at most 32 bits.",
        ),
        (
            lambda: unpack_to_bits(ds[ds[100]], c.select(1, 17)),
            "unpack_to_bits() got 17 bits as bit_block, and DS[DS100] holds 16 bits. Pass at "
            "most 16 bits, or a Dint or Real tag as source.",
        ),
        (
            lambda: pack_words(ds.select(1, 3), dd[1]),
            "pack_words() got the range DS1..DS3 of 3 Int tags as word_block. Pass a range "
            "of 2 Int or Word tags from .select(), such as ds.select(1, 2).",
        ),
        (
            lambda: pack_bits(Words.instance_select(1, 2), dh[1]),
            "pack_bits() got the range Words.instance_select(1, 2) of Int tags as bit_block. "
            "Pass a range of Bool tags from .select(), such as c.select(1, 16).",
        ),
        (
            lambda: copy(1, 5),
            "copy() got the constant 5 as target. Pass one tag, or a pointer to one.",
        ),
        (
            lambda: copy(1, None),
            "copy() got None as target. Pass one tag, or a pointer to one.",
        ),
        (
            lambda: copy([ds[1]], ds[3]),
            "copy() got a list as source, and DS3 holds a number. Pass a number, a tag, a "
            "pointer to one or an expression.",
        ),
        (
            lambda: math(None, ds[3]),
            "math() got None as expression. Pass one tag, a pointer to one, a constant or an "
            "expression.",
        ),
        (
            lambda: math(ds[1] + None, ds[3]),
            "math() got None inside the expression. Pass a number, a tag or an expression in "
            "its place.",
        ),
        (
            lambda: copy(ds[1] + (ds[2] > 1), c[3]),
            "copy() got the condition DS2 > 1 inside the source. Pass a number, a tag or an "
            "expression in its place.",
        ),
        (
            lambda: math(ds[1] * (1, 2), ds[3]),
            "math() got a tuple inside the expression, and DS3 holds a number. Pass a number, "
            "a tag or an expression in its place.",
        ),
        (
            lambda: math("1.5", ds[3]),
            "math() got the text '1.5' as expression, and DS3 holds a number. Pass a number, "
            "a tag, a pointer to one or an expression.",
        ),
        (
            lambda: math(ds.select(ds[100], ds[101]), ds[3]),
            "math() got the range DS[DS100..DS101] of Int tags as expression, and DS3 holds a "
            "number. Pass a number, a tag, a pointer to one or an expression.",
        ),
        (
            lambda: _forloop("A"),
            "forloop() got the text 'A' as count. Pass a whole number, a tag, a pointer to "
            "one or an expression.",
        ),
        (
            lambda: _forloop(3.5),
            "forloop() got the constant 3.5 as count. Pass a whole number or a tag. A "
            "constant count has no decimal point.",
        ),
        (
            lambda: _forloop(5.0),
            "forloop() got the constant 5.0 as count. Pass a whole number or a tag. A "
            "constant count has no decimal point.",
        ),
        (
            lambda: math(ds[1], 5),
            "math() got the constant 5 as dest. Pass one tag.",
        ),
        (
            lambda: search(ds.select(1, 2) >= 1, result=ds[ds[100]], found=c[1]),
            "search() got the Int pointer DS[DS100] as result. Pass one Int or Dint tag. "
            "Then copy() that tag to the pointer.",
        ),
        (
            lambda: _forloop(0),
            "forloop() got the constant 0 as count. Pass a whole number from 1 to 2147483647, "
            "or a tag.",
        ),
        (
            lambda: copy(ds[1], ds[1] + Timer[1]),
            "copy() got the expression (DS1 + Timer[1]) as target. Pass one tag, or a pointer "
            "to one.",
        ),
        (
            lambda: math(ds[1], ds[1] + "5"),
            "math() got the expression (DS1 + '5') as dest. Pass one tag.",
        ),
        (
            lambda: run_function(_returns_r, outs={"r": 5}),
            "run_function() got the constant 5 as outs['r']. Pass one tag, or a pointer to one.",
        ),
    ],
    ids=[
        "17 bits into Word",
        "48 bits in immediate() into Dint",
        "Int to 17 bits",
        "3 words",
        "named_array instances",
        "copy into a constant",
        "copy into None",
        "copy a list into a number",
        "None as the expression",
        "None inside an expression",
        "condition inside an expression",
        "tuple inside an expression into a number",
        "real text into an Int",
        "range with pointers into a number",
        "text count",
        "Real count",
        "whole Real count",
        "constant as math dest",
        "pointer as search result",
        "count of 0",
        "structure inside a target expression",
        "text inside a dest expression",
        "constant in outs",
    ],
)
def test_a_refusal_says_what_to_pass(build: Callable[[], None], message: str) -> None:
    with pytest.raises((TypeError, ValueError)) as refused:
        with Program(strict=False):
            with rung(x[3]):
                build()
    assert str(refused.value) == message


def test_calc_names_calc_in_its_refusal() -> None:
    from pyrung import calc

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with Program(strict=False):
            with rung(x[3]):
                with pytest.raises(TypeError, match=r"^calc\(\) got the text 'A' as expression"):
                    calc("A", ds[3])


@pytest.mark.parametrize(
    "build",
    [
        lambda: math("5", ds[3]),
        lambda: math(txt[1], ds[3]),
        lambda: math(txt[ds[100]], df[3]),
        lambda: _forloop("3"),
        lambda: _forloop(txt[1]),
        lambda: _forloop(txt[ds[100]]),
    ],
    ids=[
        "integer text",
        "Char tag",
        "Char pointer",
        "text count",
        "Char count",
        "Char pointer count",
    ],
)
def test_text_or_a_char_that_holds_a_digit_writes(build: Callable[[], None]) -> None:
    """Text that parses, or a Char tag that holds a digit, becomes a number on the scan."""
    with Program(strict=False) as program:
        with rung(x[3]):
            build()
    assert _run(program, DATA[1])


class _NoSignature:
    """A callable whose signature ``inspect`` cannot read, so ``outs`` goes unchecked."""

    __signature__ = "none"

    def __call__(self, **_kwargs: Any) -> dict[str, int]:
        return {"r": 1}


def test_outs_may_be_any_mapping() -> None:
    with Program(strict=False) as program:
        with rung(x[3]):
            run_function(_NoSignature(), outs=MappingProxyType({"r": ds[5]}))
    assert _run(program, DATA[0]) == {"DS5"}


def test_shift_refuses_a_wrong_range_before_clock_and_reset() -> None:
    with Program(strict=False):
        with rung(x[3]):
            with pytest.raises(TypeError, match=r"^shift\(\) got the range DS1\.\.DS2 of Int"):
                shift(ds.select(1, 2))


def test_forloop_refuses_a_wrong_count_before_its_body() -> None:
    with Program(strict=False):
        with rung(x[3]):
            with pytest.raises(TypeError, match=r"^forloop\(\) got None as count"):
                forloop(None)


def test_search_refuses_found_and_result_before_the_operator() -> None:
    from pyrung.core.memory_block import RangeComparison

    # The DSL's operators make only valid comparisons, so this one is made by hand.
    comparison = RangeComparison(ds.select(1, 2), "<>", 0)
    with Program(strict=False):
        with rung(x[3]):
            with pytest.raises(TypeError) as refused:
                search(comparison, result=ds[5], found=ds[6])
            assert str(refused.value) == "search() got the Int tag DS6 as found. Pass one Bool tag."
            with pytest.raises(TypeError) as refused:
                search(comparison, result=c[5], found=c[6])
            assert str(refused.value) == (
                "search() got the Bool tag C5 as result. Pass one Int or Dint tag."
            )
