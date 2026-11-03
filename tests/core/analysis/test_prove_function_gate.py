"""What the verifier says of a ``run_function`` it can't learn anything from by calling.

The search calls a function to explore.  A function whose answer may not follow from its
inputs answers those calls with what stepping might never see, so it gets a named
Intractable, and a function the gate passes stays explored.  Each row is measured against
stepping: the verdict is only worth having if a scan agrees.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path
from typing import Any

import pytest

import pyrung.cli as cli
from pyrung.core import Bool, Program, Rung, run_function
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    Proven,
    always,
    check_lock,
    never,
    reachable_states,
)
from pyrung.core.runner import PLC, calls_that_depend_on_more_than_their_inputs

In1 = Bool("In1", external=True)
Alarm = Bool("Alarm")
LIMIT = 49
_calls = {"n": 0}


def _program(fn: Any, ins: dict[str, Any] | None = None) -> Program:
    with Program(strict=False) as program:
        with Rung(In1):
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
        if hasattr(builtins, "_pyrung_gate_row"):
            del builtins._pyrung_gate_row  # type: ignore[attr-defined]

    forget()
    random.seed(7)
    yield
    forget()


@pytest.mark.parametrize("make, handed", REFUSED)
def test_a_function_stepping_can_tell_apart_is_not_proven(make: Any, handed: Any) -> None:
    assert _alarm_scan_stepping(_build_row(make, handed)) is not None

    result = always(_build_row(make, handed), ~Alarm)
    assert isinstance(result, Intractable)
    assert result.kind == "function"
    assert result.tags == ["Alarm"]
    assert result.hints


@pytest.mark.parametrize("make, handed", REFUSED)
def test_every_verifier_entry_names_the_function(make: Any, handed: Any) -> None:
    program = _build_row(make, handed)
    never_result = never(program, Alarm)
    states = reachable_states(program, project=["Alarm"])
    batch = always(program, [~Alarm, ~In1])

    assert isinstance(never_result, Intractable)
    assert never_result.kind == "function"
    assert isinstance(states, Intractable)
    assert states.kind == "function"
    assert isinstance(batch, list)
    assert [type(result) for result in batch] == [Intractable, Intractable]


def test_the_reason_says_which_function_and_why() -> None:
    result = always(_program(_closure()), ~Alarm)
    assert isinstance(result, Intractable)
    assert "counts" in result.reason
    assert "closure" in result.hints[0]

    keeper = always(_program(_keeps_what_it_is_handed, {"v": In1, "m": []}), ~Alarm)
    assert isinstance(keeper, Intractable)
    assert "ins['m']" in keeper.hints[0]


def test_the_search_does_not_call_a_function_it_declines() -> None:
    counts = _Counts()
    always(_program(counts), ~Alarm)
    never(_program(counts), Alarm)
    reachable_states(_program(counts), project=["Alarm"])
    assert counts.n == 0


def _answers_true(v):
    return {"out": True}


def _answers_false(v):
    return {"out": False}


def _answers_by_input(v):
    return {"out": not v}


def _uses_math(v):
    return {"out": math.floor(2.5) == 3}


def test_the_gate_is_the_one_a_kernel_asks() -> None:
    from pyrung.core.runner import _program_calls_what_a_kernel_may_not

    for make, handed in (row.values[:2] for row in REFUSED):
        program = _build_row(make, handed)
        assert _program_calls_what_a_kernel_may_not(program)
        assert calls_that_depend_on_more_than_their_inputs(program)
    assert not _program_calls_what_a_kernel_may_not(_program(_answers_true))
    assert not calls_that_depend_on_more_than_their_inputs(_program(_answers_true))


def test_a_function_the_gate_passes_stays_explored() -> None:
    assert isinstance(always(_program(_answers_true), ~Alarm), Counterexample)
    assert isinstance(always(_program(_answers_false), ~Alarm), Proven)
    assert isinstance(always(_program(_uses_math), ~Alarm), Proven)
    assert isinstance(always(_program(_answers_by_input), ~Alarm), Proven)
    assert _alarm_scan_stepping(_program(_answers_true)) is not None
    assert _alarm_scan_stepping(_program(_answers_false)) is None
    assert _alarm_scan_stepping(_program(_uses_math)) is None
    assert _alarm_scan_stepping(_program(_answers_by_input)) is None


LOCKED_MODULE = """
from pyrung.core import Bool, Program, Rung, run_function

In1 = Bool("In1", external=True)
Alarm = Bool("Alarm", public=True)
{fn}

with Program(strict=False) as logic:
    with Rung(In1):
        run_function(fn, ins={{"v": In1}}, outs={{"out": Alarm}})
"""

CLOSURE_FN = """
def _make():
    n = [0]
    def counts(v):
        n[0] += 1
        return {"out": n[0] >= 50}
    return counts
fn = _make()
"""

PLAIN_FN = """
def fn(v):
    return {"out": v}
"""


def _lock(tmp_path: Path, fn_text: str, capsys: Any, monkeypatch: Any) -> tuple[int, str, Path]:
    (tmp_path / "gate_row.py").write_text(LOCKED_MODULE.format(fn=fn_text), encoding="utf-8")
    lock = tmp_path / "pyrung.lock"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delitem(sys.modules, "gate_row", raising=False)
    monkeypatch.setattr(sys, "argv", ["pyrung", "lock", "gate_row:logic", "-o", str(lock)])
    try:
        cli.main()
        code = 0
    except SystemExit as exc:
        code = int(exc.code or 0)
    monkeypatch.delitem(sys.modules, "gate_row", raising=False)
    return code, capsys.readouterr().err, lock


def test_a_lock_of_a_function_the_gate_refuses_is_not_written(
    tmp_path: Path, capsys: Any, monkeypatch: Any
) -> None:
    code, err, lock = _lock(tmp_path, CLOSURE_FN, capsys, monkeypatch)
    assert code == 1
    assert "counts" in err
    assert "plain def" in err
    assert not lock.exists()


def test_a_lock_of_a_function_the_gate_passes_is_written(
    tmp_path: Path, capsys: Any, monkeypatch: Any
) -> None:
    code, _, lock = _lock(tmp_path, PLAIN_FN, capsys, monkeypatch)
    assert code == 0
    assert lock.exists()


def test_check_lock_of_a_function_the_gate_refuses_raises_the_reason(
    tmp_path: Path, capsys: Any, monkeypatch: Any
) -> None:
    _, _, lock = _lock(tmp_path, PLAIN_FN, capsys, monkeypatch)
    with pytest.raises(RuntimeError, match="counts"):
        check_lock(_program(_closure()), lock)
