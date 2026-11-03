"""An in-process kernel calls the ``run_function`` itself: nothing is copied, so nothing differs."""

from __future__ import annotations

import functools
import math
import types

import pytest

from pyrung import Bool, Int, Or, Program, Rung, calc, run_function
from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core.analysis.prove import Counterexample, Intractable, Proven, always
from pyrung.core.analysis.prove.lockfile import program_hash
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.runner import PLC, _may_be_called_again

NEVER = Bool("Never")
A, B, Run = Int("A"), Int("B"), Bool("Run")
Lamp, In1 = Bool("Lamp"), Bool("In1", external=True)
SCANS = 2500

MODULE_VALUE = 7


def _program(fn, *, ins=None) -> Program:
    with Program(strict=False) as program:
        with Rung():
            calc(A + 1, A)
        with Rung(Run):
            run_function(fn, ins=ins if ins is not None else {"v": A}, outs={"out": B})
    return program


def _plc(program) -> PLC:
    plc = PLC(program, dt=0.01)
    plc.patch({"Run": True})
    return plc


def _run(plc: PLC, scans: int = SCANS) -> None:
    plc.run_until(NEVER, max_cycles=scans)


def _step(plc: PLC, scans: int = SCANS) -> None:
    for _ in range(scans):
        plc.run_until(NEVER, max_cycles=1, jit=False)


def _has_a_kernel(program) -> bool:
    return PLC(program, dt=0.01)._compiled_replay_supported_kernel() is not None


def plain(v):
    return {"out": v + 1}


def floors(v):
    return {"out": math.floor(v / 2)}


def reads_pi(v):
    return {"out": int(math.pi)}


def reads_a_global(v):
    return {"out": MODULE_VALUE}


def _closure():
    base = 7

    def closed(v):
        return {"out": v + base}

    return closed


class _Callable:
    def __call__(self, v):
        return {"out": v}


class _Method:
    def bound(self, v):
        return {"out": v}


def test_a_kernel_binds_the_function_itself():
    kernel = compile_kernel(_program(plain))

    assert kernel.step_fn.__globals__["_fn_plain"] is plain


def test_a_kernel_runs_no_text_of_the_function_and_a_lock_hashes_it():
    kernel = compile_kernel(_program(plain))

    assert "def plain" not in kernel.source
    assert "_fn_plain = plain" not in kernel.source
    assert "_fn_plain(v=" in kernel.source
    assert "def plain(v):" in kernel.lock_source
    assert "_fn_plain = plain" in kernel.lock_source


def test_a_program_with_no_function_locks_the_source_it_runs():
    with Program(strict=False) as program:
        with Rung():
            calc(A + 1, A)
    kernel = compile_kernel(program)

    assert kernel.lock_source == kernel.source


@pytest.mark.parametrize(
    "fn",
    [
        pytest.param(_closure(), id="closure"),
        pytest.param(floors, id="a method of an import"),
        pytest.param(functools.partial(plain), id="partial"),
        pytest.param(_Callable(), id="callable object"),
        pytest.param(_Method().bound, id="bound method"),
    ],
)
def test_analysis_takes_any_callable_a_scan_can_call(fn):
    with Program(strict=False) as program:
        with Rung(In1):
            run_function(fn, ins={"v": A}, outs={"out": B})
        with Rung(B == 1):
            calc(1, A)
        with Rung(In1):
            calc(1, Lamp)

    assert compile_kernel(program, blockless=True, proof_metadata=True).source
    assert program_hash(program) == program_hash(program)
    result = always(program, Or(~Lamp, In1))
    assert isinstance(result, Proven | Counterexample | Intractable)
    CompiledPLC(program, dt=0.01).step()


def test_a_callable_with_no_text_has_a_lock_that_outlives_its_address():
    assert program_hash(_program(functools.partial(plain))) == program_hash(
        _program(functools.partial(plain))
    )
    assert program_hash(_program(_Callable())) == program_hash(_program(_Callable()))
    assert program_hash(_program(_Callable())) != program_hash(_program(functools.partial(plain)))


def test_a_lock_changes_with_the_functions_text():
    def twice(v):
        return {"out": v * 2}

    first = program_hash(_program(twice))

    def twice(v):  # noqa: F811
        return {"out": v * 3}

    assert program_hash(_program(twice)) != first


def test_a_lock_does_not_change_when_the_function_does_not():
    first = program_hash(_program(plain))
    assert program_hash(_program(plain)) == first


def _lands(program, go) -> tuple:
    plc = _plc(program)
    go(plc)
    return plc.state.scan_id, plc.state.tags["A"], plc.state.tags["B"]


def _same_both_ways(program) -> tuple:
    ran = _lands(program, _run)
    assert ran == _lands(program, _step)
    return ran


class _Holder:
    @staticmethod
    def method(v):
        return {"out": len(repr(lambda: 0).split(" at ")[0])}


def _hand_set_defaults():
    def d(v, a=1, b=2):
        return {"out": a * 10 + b}

    d.__defaults__ = (1,)  # Now a is required, b is 1: the text says otherwise.
    return d


def _hand_set_kwdefaults():
    def d(v, *, a=1, b=2):
        return {"out": a * 10 + b}

    d.__kwdefaults__ = {"a": 5, "b": 2}
    return d


def test_a_nested_functions_own_name_is_the_one_it_has():
    assert _has_a_kernel(_program(_Holder.method))
    assert _same_both_ways(_program(_Holder.method))[2] == _Holder.method(0)["out"]


def test_defaults_set_by_hand_are_the_ones_a_kernel_calls_with():
    fn = _hand_set_defaults()
    program = _program(fn, ins={"v": A, "a": 0})
    assert _has_a_kernel(program)
    assert _same_both_ways(program)[2] == 1

    assert _same_both_ways(_program(_hand_set_kwdefaults()))[2] == 52


@pytest.mark.parametrize("go", [_run, _step], ids=["run", "step"])
def test_defaults_changed_between_runs_are_seen(go):
    def d(v, a=1):
        return {"out": a}

    plc = _plc(_program(d))
    go(plc, 1500)
    assert plc.state.tags["B"] == 1
    d.__defaults__ = (5,)
    go(plc, 1500)
    assert plc.state.tags["B"] == 5


@pytest.mark.parametrize("go", [_run, _step], ids=["run", "step"])
def test_code_swapped_between_runs_is_what_runs(go):
    def one(v):
        return {"out": 1}

    def two(v):
        return {"out": 2}

    plc = _plc(_program(one))
    go(plc, 1500)
    assert plc.state.tags["B"] == 1
    one.__code__ = two.__code__
    go(plc, 1500)
    assert plc.state.tags["B"] == 2


@pytest.mark.parametrize("go", [_run, _step], ids=["run", "step"])
def test_a_module_global_over_a_builtin_set_between_runs_is_what_runs(go):
    namespace: dict = {}
    exec("def f(v):\n    return {'out': abs(v)}\n", namespace)  # noqa: S102

    plc = _plc(_program(namespace["f"]))
    go(plc, 1500)
    assert plc.state.tags["B"] == 1500
    namespace["abs"] = lambda v: 41
    go(plc, 1500)
    assert plc.state.tags["B"] == 41


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_a_base_exception_lands_where_stepping_does(exception):
    namespace: dict = {}
    exec(  # noqa: S102
        f"def stops(v):\n"
        f"    if v == 1500:\n"
        f"        raise {exception.__name__}\n"
        f"    return {{'out': v}}\n",
        namespace,
    )
    program = _program(namespace["stops"])
    assert not _has_a_kernel(program)

    def lands(go) -> tuple:
        plc = _plc(program)
        with pytest.raises(exception):
            go(plc)
        return plc.state.scan_id, plc.state.tags["A"], plc.state.tags["B"]

    assert lands(_run) == lands(_step) == (1499, 1499, 1499)


def _raises_value_error(v):
    if v < -1:
        raise ValueError(v)
    return {"out": v}


def _uses_only_builtins(v):
    return {"out": max(abs(v), len(str(v)))}


def _tuple_default(v, t=(1, 2), *, n=None):
    return {"out": t[0]}


def _mutable_default(v, memo=[]):  # noqa: B006
    memo.append(v)
    return {"out": len(memo)}


def _mutable_kwdefault(v, *, memo={}):  # noqa: B006
    return {"out": len(memo)}


def _object_default(v, thing=_Holder()):  # noqa: B008
    return {"out": v}


def _imports(v):
    import random

    return {"out": random.randint(0, 1)}


def _stores_a_global(v):
    global MODULE_VALUE
    MODULE_VALUE = v
    return {"out": v}


def _reads_a_dunder(v):
    return {"out": len(_reads_a_dunder.__globals__)}


def _sets_an_attribute_of_math(v):
    math.pyrung_memo = v
    return {"out": v}


def _prints(v):
    print(v)
    return {"out": v}


def _raises_keyboard_interrupt(v):
    raise KeyboardInterrupt


def _raises_system_exit(v):
    raise SystemExit


def _raises_generator_exit(v):
    raise GeneratorExit


def _with_own_builtins():
    return types.FunctionType(plain.__code__, {"__builtins__": {"abs": lambda v: 77}}, "plain")


@pytest.mark.parametrize(
    "fn",
    [
        plain,
        floors,
        reads_pi,
        _raises_value_error,
        _uses_only_builtins,
        _tuple_default,
        _Holder.method,
        _hand_set_defaults(),
    ],
)
def test_a_function_that_keeps_nothing_may_be_called_again(fn):
    assert _may_be_called_again(fn)


@pytest.mark.parametrize(
    "fn",
    [
        pytest.param(_closure(), id="closure"),
        pytest.param(functools.partial(plain), id="partial"),
        pytest.param(_Callable(), id="callable object"),
        pytest.param(_Method().bound, id="bound method"),
        pytest.param(abs, id="builtin"),
        pytest.param(_mutable_default, id="mutable default"),
        pytest.param(_mutable_kwdefault, id="mutable keyword default"),
        pytest.param(_object_default, id="an object as a default"),
        pytest.param(_imports, id="import"),
        pytest.param(reads_a_global, id="reads a global"),
        pytest.param(_stores_a_global, id="stores a global"),
        pytest.param(_reads_a_dunder, id="dunder"),
        pytest.param(_sets_an_attribute_of_math, id="sets an attribute of math"),
        pytest.param(_prints, id="print"),
        pytest.param(_raises_keyboard_interrupt, id="KeyboardInterrupt"),
        pytest.param(_raises_system_exit, id="SystemExit"),
        pytest.param(_raises_generator_exit, id="GeneratorExit"),
        pytest.param(_with_own_builtins(), id="its own builtins"),
    ],
)
def test_a_function_that_could_keep_something_steps(fn):
    assert not _may_be_called_again(fn)


def test_its_own_builtins_are_what_it_calls_on_every_path():
    def uses_abs(v):
        return {"out": abs(v)}

    own = types.FunctionType(uses_abs.__code__, {"__builtins__": {"abs": lambda v: 77}}, "uses_abs")
    assert _same_both_ways(_program(own))[2] == 77
