"""Check which ``run_function`` calls the gate refuses and what stepping writes through one.

A function whose answer may not follow from its inputs fails the gate.  The kernel asks
the same gate.  Each row is measured against stepping, so the gate refuses only what a
scan can tell apart from a plain function.
"""

from __future__ import annotations

import math
import random
from typing import Any

import pytest

from pyrung.core import (
    Block,
    Bool,
    Int,
    Program,
    TagType,
    immediate,
    run_enabled_function,
    run_function,
    rung,
)
from pyrung.core.runner import PLC, calls_that_depend_on_more_than_their_inputs

In1 = Bool("In1", external=True)
Alarm = Bool("Alarm")
LIMIT = 49
_calls = {"n": 0}


def _program(fn: Any, ins: dict[str, Any] | None = None) -> Program:
    with Program(strict=False) as program:
        with rung(In1):
            run_function(fn, ins={"v": In1} if ins is None else ins, outs={"out": Alarm})
    return program


def _alarm_scan_stepping(program: Program, scans: int = 200) -> int | None:
    plc = PLC(program, dt=0.01)
    plc.patch({"In1": True})
    for _ in range(scans):
        plc.step()
        if plc.state.tags.get("Alarm"):
            return plc.state.scan_id
    return None


def _closure() -> Any:
    count = [0]

    def counts(v):
        count[0] += 1
        return {"out": count[0] >= 50}

    return counts


_STORED = 0


def _global_store() -> Any:
    def counts(v):
        global _STORED
        _STORED += 1
        return {"out": _STORED >= 50}

    return counts


class _Counts:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self, v):
        self.n += 1
        return {"out": self.n >= 50}


def _rolls(v):
    return {"out": random.random() < 0.2}


def _reads_a_module_global(v):
    _calls["n"] += 1
    return {"out": _calls["n"] > LIMIT}


def _helper(n):
    return n > LIMIT


def _calls_a_helper(v):
    _calls["n"] += 1
    return {"out": _helper(_calls["n"])}


def _mutable_default(v, seen=[]):  # noqa: B006 - the point of the row
    seen.append(v)
    return {"out": len(seen) >= 50}


def _imports(v):
    import builtins

    n = getattr(builtins, "_pyrung_gate_row", 0) + 1
    builtins._pyrung_gate_row = n  # type: ignore[attr-defined]
    return {"out": n >= 50}


_SEEN: list[Any] = []


# The global is read only inside the comprehension. Python 3.12 inlines a list comprehension
# into the function's own code (it was a nested code object). A generator expression stays
# nested. The gate must find the read in both.
def _keeps_a_list_in_a_comprehension(v):
    return {"out": [_SEEN.append(v) or len(_SEEN) >= 50 for _ in (0,)][0]}


def _keeps_a_list_in_a_generator(v):
    return {"out": next(_SEEN.append(v) or len(_SEEN) >= 50 for _ in (0,))}


def _keeps_what_it_is_handed(v, m):
    m.append(v)
    return {"out": len(m) >= 50}


REFUSED = [
    pytest.param(_closure, None, id="closure"),
    pytest.param(_global_store, None, id="global_store"),
    pytest.param(_Counts, None, id="callable_object"),
    pytest.param(lambda: _rolls, None, id="random"),
    pytest.param(lambda: _reads_a_module_global, None, id="module_global"),
    pytest.param(lambda: _calls_a_helper, None, id="helper"),
    pytest.param(lambda: _mutable_default, None, id="mutable_default"),
    pytest.param(lambda: _imports, None, id="import_in_body"),
    pytest.param(lambda: _keeps_what_it_is_handed, list, id="list_in_ins"),
    pytest.param(lambda: _keeps_a_list_in_a_comprehension, None, id="global_in_comprehension"),
    pytest.param(lambda: _keeps_a_list_in_a_generator, None, id="global_in_generator"),
]


def _build_row(make: Any, handed: Any) -> Program:
    if handed is None:
        return _program(make())
    return _program(make(), {"v": In1, "m": handed()})


@pytest.fixture(autouse=True)
def _fresh_rows():
    import builtins

    def forget() -> None:
        _calls["n"] = 0
        globals()["_STORED"] = 0
        _mutable_default.__defaults__[0].clear()  # type: ignore[index]
        _SEEN.clear()
        if hasattr(builtins, "_pyrung_gate_row"):
            del builtins._pyrung_gate_row  # type: ignore[attr-defined]

    forget()
    random.seed(7)
    yield
    forget()


@pytest.mark.parametrize("make, handed", REFUSED)
def test_a_function_the_gate_refuses_raises_alarm_when_stepped(make: Any, handed: Any) -> None:
    assert calls_that_depend_on_more_than_their_inputs(_build_row(make, handed))
    assert _alarm_scan_stepping(_build_row(make, handed)) is not None


def test_the_reason_says_which_function_and_why() -> None:
    [(call, reason)] = calls_that_depend_on_more_than_their_inputs(_program(_closure()))
    assert call._fn.__qualname__.endswith("counts")
    assert "closure" in reason

    keeper = _program(_keeps_what_it_is_handed, {"v": In1, "m": []})
    [(_, reason)] = calls_that_depend_on_more_than_their_inputs(keeper)
    assert "ins['m']" in reason


def _answers_true(v):
    return {"out": True}


def _answers_false(v):
    return {"out": False}


def _answers_by_input(v):
    return {"out": not v}


def _uses_math(v):
    return {"out": math.floor(2.5) == 3}


def _comprehensions_over_its_input(v):
    squares = {n: n * n for n in range(int(v) + 2)}
    return {"out": sum([x for x in squares.values()]) + sum(n for n in range(2)) > 100}


PASSED = [
    _answers_true,
    _answers_false,
    _answers_by_input,
    _uses_math,
    _comprehensions_over_its_input,
]


def test_the_gate_is_the_one_a_kernel_asks() -> None:
    from pyrung.core.runner import _program_calls_what_a_kernel_may_not

    for make, handed in (row.values[:2] for row in REFUSED):
        program = _build_row(make, handed)
        assert _program_calls_what_a_kernel_may_not(program)
        assert calls_that_depend_on_more_than_their_inputs(program)
    for fn in PASSED:
        assert not _program_calls_what_a_kernel_may_not(_program(fn))
        assert not calls_that_depend_on_more_than_their_inputs(_program(fn))


def test_a_function_the_gate_passes_steps_as_its_answer_says() -> None:
    assert _alarm_scan_stepping(_program(_answers_true)) is not None
    assert _alarm_scan_stepping(_program(_answers_false)) is None
    assert _alarm_scan_stepping(_program(_uses_math)) is None
    assert _alarm_scan_stepping(_program(_answers_by_input)) is None
    assert _alarm_scan_stepping(_program(_comprehensions_over_its_input)) is None


def _unread_output(wrap: Any = lambda tag: tag) -> tuple[Program, Any, dict[str, Any], str, Any]:
    run, alarm = Bool("Run", external=True), Bool("Alarm")

    def passes(enabled, v):
        return {"out": v}

    with Program(strict=False) as program:
        with rung():
            run_enabled_function(passes, ins={"v": run}, outs={"out": wrap(alarm)})
    return program, ~alarm, {"Run": True}, "Alarm", True


def _unread_immediate_output() -> tuple[Program, Any, dict[str, Any], str, Any]:
    return _unread_output(immediate)


def _output_through_a_pointer() -> tuple[Program, Any, dict[str, Any], str, Any]:
    idx = Int("Idx", external=True, default=1, choices={1: "a", 2: "b"})
    slots = Block("Dq", TagType.INT, 1, 3)

    def one(v):
        return {"out": 1 if v else 0}

    with Program(strict=False) as program:
        with rung():
            run_function(one, ins={"v": In1}, outs={"out": slots[idx]})
    return program, slots[2] == 0, {"In1": True, "Idx": 2}, "Dq2", 1


_OUTPUT_ROWS = pytest.mark.parametrize(
    "row",
    [_unread_output, _unread_immediate_output, _output_through_a_pointer],
    ids=["unread", "unread immediate", "through a pointer"],
)


@_OUTPUT_ROWS
def test_a_function_output_writes_when_stepped(row) -> None:
    program, _, patch, name, written = row()
    plc = PLC(program, dt=0.01)
    plc.patch(patch)
    plc.step()
    assert plc.current_state.tags[name] == written
