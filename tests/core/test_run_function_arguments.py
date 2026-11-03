"""What a ``run_function`` is handed and what it returns land as stepping lands them, on every path."""

from __future__ import annotations

import math
import os
import subprocess
import sys
from decimal import Decimal
from enum import Enum

import pytest

from pyrung import (
    Block,
    Bool,
    Int,
    Program,
    Rung,
    TagType,
    calc,
    copy,
    run_enabled_function,
    run_function,
)
from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.circuitpy.codegen import compile_kernel
from pyrung.circuitpy.codegen.context import _text_for_the_lock
from pyrung.core.analysis.prove.lockfile import program_hash
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.runner import PLC, calls_that_depend_on_more_than_their_inputs
from tests import fold_check
from tests.core.test_fault_flags import _run as _run_on

IN_PROCESS = ["interpreted", "compiled", "kernel_legacy", "kernel_blockless"]
GENERATED = ["circuitpy", "circuitpy_rt"]
ENGINES = IN_PROCESS + GENERATED

NEVER = Bool("Never")
A, B = Int("A"), Int("B")
First, Second = Int("First", default=5), Int("Second", default=9)
Idx = Int("Idx", default=1)
Run = Bool("Run")
DS = Block("DS", TagType.INT, 1, 8)
SCANS = 2500


def _called(fn, *, ins=None, outs=None, enabled=False) -> Program:
    call = run_enabled_function if enabled else run_function
    with Program(strict=False) as program:
        with Rung():
            calc(A + 1, A)
        with Rung(Run):
            call(fn, ins=ins if ins is not None else {"v": A}, outs=outs or {"out": B})
    return program


def _always_called(fn, *, ins, outs=None, enabled=False) -> Program:
    call = run_enabled_function if enabled else run_function
    with Program(strict=False) as program:
        with Rung():
            call(fn, ins=ins, outs=outs or {"out": B})
    return program


def _plc(program) -> PLC:
    plc = PLC(program, dt=0.01)
    plc.patch({"Run": True})
    return plc


def _has_a_kernel(program) -> bool:
    return PLC(program, dt=0.01)._compiled_replay_supported_kernel() is not None


def _run(plc: PLC, scans: int = SCANS) -> None:
    plc.run_until(NEVER, max_cycles=scans)


def _step(plc: PLC, scans: int = SCANS) -> None:
    for _ in range(scans):
        plc.run_until(NEVER, max_cycles=1, fold=False)


def _lands(program, go, *tags) -> tuple:
    plc = _plc(program)
    go(plc)
    return tuple(plc.state.tags[getattr(tag, "name", tag)] for tag in tags)


def _a_run_lands_where_stepping_does(make, *tags) -> tuple:
    """*make* builds the program afresh, so that a constant the function changes starts fresh."""
    ran = _lands(make(), _run, *tags)
    assert ran == _lands(make(), _step, *tags)
    return ran


def _a_run_lands_where_its_own_twin_does(make) -> int:
    """*make*'s constant keeps what the function puts in it, so a twin here would share it."""
    plc = _plc(make())
    with fold_check.own_twin(plc) as compare:
        _run(plc)
        twin = _plc(make())
        for _ in range(SCANS):
            twin.step()
        compare(twin)
    return plc.state.tags["B"]


def _generate(program):
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    return generate_circuitpy(program, hw, target_scan_ms=10.0, runstop=None)


def first_keyword(**kw):
    return {"out": next(iter(kw.values()))}


def first_keyword_enabled(enabled, **kw):
    return {"out": next(iter(kw.values()))}


def two_keys(v):
    return {"a": 1, "b": 2}


def index_then_value():
    return {"z": 2, "a": 7}


class TestDeclaredOrder:
    @pytest.mark.parametrize("engine", ENGINES)
    def test_ins_arrive_as_declared(self, engine, monkeypatch):
        program = _always_called(first_keyword, ins={"z": First, "a": Second})
        assert _run_on(program, engine, (B,), monkeypatch) == (5,)

    @pytest.mark.parametrize("engine", ENGINES)
    def test_an_enabled_functions_ins_arrive_as_declared(self, engine, monkeypatch):
        program = _always_called(first_keyword_enabled, ins={"z": First, "a": Second}, enabled=True)
        assert _run_on(program, engine, (B,), monkeypatch) == (5,)

    @pytest.mark.parametrize("enabled", [False, True], ids=["run", "enabled"])
    @pytest.mark.parametrize("engine", ENGINES)
    def test_two_outs_on_a_tag_leave_the_one_declared_last(self, engine, enabled, monkeypatch):
        fn = two_keys_enabled if enabled else two_keys
        program = _always_called(fn, ins={"v": A}, outs={"b": B, "a": B}, enabled=enabled)
        assert _run_on(program, engine, (B,), monkeypatch) == (1,)

    @pytest.mark.parametrize("engine", ENGINES)
    def test_an_out_through_an_index_uses_the_index_as_declared(self, engine, monkeypatch):
        at_one, at_two = Int("AtOne"), Int("AtTwo")
        with Program(strict=False) as program:
            with Rung():
                run_function(index_then_value, outs={"a": DS[Idx], "z": Idx})
                copy(DS[1], at_one)
                copy(DS[2], at_two)
        assert _run_on(program, engine, (Idx, at_one, at_two), monkeypatch) == (2, 7, 0)

    def test_a_run_lands_where_stepping_does_in_each_order(self):
        by_keyword = _called(first_keyword, ins={"z": A, "a": Second})
        assert _a_run_lands_where_stepping_does(lambda: by_keyword, B) == (SCANS,)

        by_out = _called(two_keys, outs={"b": B, "a": B})
        assert _a_run_lands_where_stepping_does(lambda: by_out, B) == (1,)

    def test_a_lock_changes_with_the_order_a_call_is_declared_in(self):
        first = program_hash(_always_called(first_keyword, ins={"z": First, "a": Second}))
        second = program_hash(_always_called(first_keyword, ins={"a": Second, "z": First}))
        assert first != second


def two_keys_enabled(enabled, v):
    return {"a": 1, "b": 2}


def grows(v, memo):
    memo.append(v)
    return {"out": len(memo)}


def counts(v, seen):
    seen[v] = True
    return {"out": len(seen)}


def reads_first(v, table):
    return {"out": table[0]}


def reads_nested(table):
    (one, two), [three], rest = table
    return {"out": one * 1000 + two * 100 + three * 10 + rest["x"]}


def constant(v, value):
    return {"out": 1}


def takes_literals(v, value, more, flag, ratio):
    return {"out": v}


class _Thing:
    n = 0

    def bump(self):
        self.n += 1
        return self.n


def bumps(v):
    return {"out": v.bump()}


class _LooksLikeFive:
    def __repr__(self):
        return "5"


def is_an_int(v):
    return {"out": int(isinstance(v, int))}


def adds_a_pair(v):
    return {"out": len(v)}


class _Color(Enum):
    RED = 1


def measures(v, value):
    if value == math.inf:
        return {"out": 1}
    return {"out": len(value) if isinstance(value, frozenset) else int(value.imag)}


class TestConstants:
    @pytest.mark.parametrize("engine", ENGINES)
    def test_a_list_keeps_what_the_function_put_in_it(self, engine, monkeypatch):
        program = _always_called(grows, ins={"v": A, "memo": []})
        assert _run_on(program, engine, (B,), monkeypatch, scans=3) == (3,)

    @pytest.mark.parametrize("engine", ENGINES)
    def test_a_dict_keeps_what_the_function_put_in_it(self, engine, monkeypatch):
        with Program(strict=False) as program:
            with Rung():
                calc(A + 1, A)
            with Rung():
                run_function(counts, ins={"v": A, "seen": {}}, outs={"out": B})
        assert _run_on(program, engine, (B,), monkeypatch, scans=3) == (3,)

    @pytest.mark.parametrize("engine", ENGINES)
    def test_two_calls_with_one_list_share_it(self, engine, monkeypatch):
        memo: list = []
        with Program(strict=False) as program:
            with Rung():
                run_function(grows, ins={"v": A, "memo": memo}, outs={"out": A})
            with Rung():
                run_function(grows, ins={"v": A, "memo": memo}, outs={"out": B})
        assert _run_on(program, engine, (B,), monkeypatch) == (2,)

    @pytest.mark.parametrize("engine", ENGINES)
    def test_a_nested_constant_comes_through(self, engine, monkeypatch):
        program = _always_called(reads_nested, ins={"table": [(1, 2), [3], {"x": 4}]})
        assert _run_on(program, engine, (B,), monkeypatch) == (1234,)

    def test_a_list_steps_in_a_run_and_grows_on_every_scan(self):
        def make():
            return _called(grows, ins={"v": A, "memo": []})

        assert not _has_a_kernel(make())
        assert _a_run_lands_where_its_own_twin_does(make) == SCANS

    def test_a_dict_steps_in_a_run_and_grows_on_every_scan(self):
        def make():
            return _called(counts, ins={"v": A, "seen": {}})

        assert not _has_a_kernel(make())
        assert _a_run_lands_where_its_own_twin_does(make) == SCANS

    @pytest.mark.parametrize("go", [_run, _step], ids=["run", "step"])
    def test_a_table_changed_between_runs_is_seen(self, go):
        table = [1]
        plc = _plc(_called(reads_first, ins={"v": A, "table": table}))
        go(plc, 1500)
        assert plc.state.tags["B"] == 1
        table[0] = 9
        go(plc, 1500)
        assert plc.state.tags["B"] == 9

    @pytest.mark.parametrize("engine", IN_PROCESS)
    def test_an_object_is_the_object(self, engine, monkeypatch):
        program = _always_called(bumps, ins={"v": _Thing()})
        assert _run_on(program, engine, (B,), monkeypatch, scans=3) == (3,)

    def test_an_object_steps_in_a_run(self):
        def make():
            return _called(bumps, ins={"v": _Thing()})

        assert not _has_a_kernel(make())
        assert _a_run_lands_where_its_own_twin_does(make) == SCANS

    @pytest.mark.parametrize("engine", IN_PROCESS)
    def test_an_object_whose_repr_is_a_number_is_no_number(self, engine, monkeypatch):
        program = _always_called(is_an_int, ins={"v": _LooksLikeFive()})
        assert _run_on(program, engine, (B,), monkeypatch) == (0,)

    @pytest.mark.parametrize("engine", IN_PROCESS)
    def test_a_tuple_holding_tags_reaches_the_function_as_it_is(self, engine, monkeypatch):
        program = _always_called(adds_a_pair, ins={"v": (A, B)})
        assert _run_on(program, engine, (B,), monkeypatch) == (2,)

    @pytest.mark.parametrize(
        "value, expected",
        [
            pytest.param(math.inf, 1, id="inf"),
            pytest.param(frozenset({1, 2}), 2, id="frozenset"),
            pytest.param(1 + 2j, 2, id="complex"),
        ],
    )
    def test_a_constant_with_no_literal_runs_in_a_replay(self, value, expected):
        def make():
            return _called(measures, ins={"v": A, "value": value})

        assert _has_a_kernel(make())
        assert _a_run_lands_where_stepping_does(make, B) == (expected,)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(_Thing(), id="object"),
            pytest.param(_LooksLikeFive(), id="repr is a number"),
            pytest.param((A, B), id="a tuple holding tags"),
            pytest.param(math.inf, id="inf"),
            pytest.param(math.nan, id="nan"),
            pytest.param({1, 2}, id="set"),
            pytest.param(frozenset({1}), id="frozenset"),
            pytest.param(1 + 2j, id="complex"),
            pytest.param(_Color.RED, id="enum"),
            pytest.param([math.inf], id="inf in a list"),
            pytest.param(DS.select(1, 2), id="a range"),
        ],
    )
    def test_code_py_refuses_what_its_text_would_not_rebuild(self, value):
        program = _always_called(constant, ins={"v": A, "value": value})
        with pytest.raises(ValueError, match="can't rebuild"):
            _generate(program)

    def test_code_py_refuses_a_list_held_twice_in_one_constant(self):
        inner = [1]
        program = _always_called(constant, ins={"v": A, "value": [inner, inner]})
        with pytest.raises(ValueError, match="can't rebuild"):
            _generate(program)

    @pytest.mark.parametrize(
        "wrap_first, wrap_second",
        [
            pytest.param(lambda x: x, lambda x: [x], id="itself and in a list"),
            pytest.param(lambda x: [x], lambda x: x, id="in a list and itself"),
            pytest.param(lambda x: x, lambda x: (x,), id="itself and in a tuple"),
            pytest.param(lambda x: [x], lambda x: [x], id="in two lists"),
            pytest.param(lambda x: {"k": x}, lambda x: (1, [x]), id="deeper in each"),
        ],
    )
    def test_code_py_refuses_a_list_two_constants_share(self, wrap_first, wrap_second):
        shared = [0]
        with Program(strict=False) as program:
            with Rung():
                run_function(constant, ins={"v": A, "value": wrap_first(shared)}, outs={"out": B})
                run_function(constant, ins={"v": A, "value": wrap_second(shared)}, outs={"out": A})
        with pytest.raises(ValueError, match="shares a list or dict"):
            _generate(program)

    def test_what_refusing_a_shared_list_saves_a_code_py_from(self):
        """Every path that runs it says the same: the change shows through the other constant."""
        C = Int("C")

        def bump(v, m):
            m[0] += 1
            return {"out": m[0]}

        def read_nested(v, m):
            return {"out": m[0][0]}

        def build() -> Program:
            shared = [0]
            with Program(strict=False) as program:
                with Rung():
                    run_function(bump, ins={"v": A, "m": shared}, outs={"out": C})
                    run_function(read_nested, ins={"v": A, "m": [shared]}, outs={"out": B})
            return program

        plc = PLC(build(), dt=0.01)
        compiled = CompiledPLC(build(), dt=0.01)
        for runner in (plc, compiled):
            for _ in range(4):
                runner.step()
            assert runner.current_state.tags["B"] == 4
            assert runner.current_state.tags["C"] == 4

    def test_code_py_takes_one_list_handed_twice_and_two_equal_lists(self):
        """One object is one binding, and two equal lists are two: the text says each."""

        def bump(v, m):
            m[0] += 1
            return {"out": m[0]}

        C = Int("C")
        shared = [0]
        with Program(strict=False) as one:
            with Rung():
                run_function(bump, ins={"v": A, "m": shared}, outs={"out": B})
                run_function(bump, ins={"v": A, "m": shared}, outs={"out": C})
        with Program(strict=False) as two:
            with Rung():
                run_function(bump, ins={"v": A, "m": [0]}, outs={"out": B})
                run_function(bump, ins={"v": A, "m": [0]}, outs={"out": C})
        assert "_fc_0 = [0]" in _generate(one).code
        assert "_fc_1" not in _generate(one).code
        assert "_fc_1 = [0]" in _generate(two).code

    def test_a_literal_keeps_its_text(self):
        program = _always_called(
            takes_literals,
            ins={"v": 1, "value": "x", "more": (1, None), "flag": True, "ratio": 0.5},
        )
        source = compile_kernel(program).source
        assert "_fn_takes_literals(v=1, value='x', more=(1, None), flag=True, ratio=0.5)" in source
        assert "_fc_" not in source

    def test_a_function_named_like_a_constants_symbol_is_refused_in_code_py(self, tmp_path):
        namespace: dict = {}
        path = tmp_path / "named.py"
        path.write_text("def _fc_0(v, memo):\n    return {'out': v}\n", encoding="utf-8")
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)  # noqa: S102
        program = _always_called(namespace["_fc_0"], ins={"v": A, "memo": []})
        with pytest.raises(ValueError, match="Rename the function"):
            _generate(program)

    @pytest.mark.parametrize(
        "one, other",
        [
            pytest.param(math.inf, -math.inf, id="inf and -inf"),
            pytest.param(math.nan, math.inf, id="nan and inf"),
            pytest.param(frozenset({1}), frozenset({2}), id="frozensets"),
            pytest.param(1j, 2j, id="complex"),
            pytest.param({1}, {2}, id="sets"),
            pytest.param((math.inf, 1), (math.inf, 2), id="in a tuple"),
            pytest.param([math.inf], [-math.inf], id="in a list"),
            pytest.param((A, B), (A, First), id="tags in a tuple"),
            pytest.param(Decimal("1.5"), Decimal("2.5"), id="decimals"),
            pytest.param({"k": {1}}, {"k": {2}}, id="a set in a dict"),
        ],
    )
    def test_a_lock_sees_a_constant_with_a_text(self, one, other):
        def lock_of(value) -> str:
            return program_hash(_always_called(constant, ins={"v": A, "value": value}))

        assert lock_of(one) != lock_of(other)
        assert lock_of(one) == lock_of(one)

    def test_a_sets_text_is_the_same_in_any_process(self):
        script = (
            "from pyrung.circuitpy.codegen.context import _text_for_the_lock as t;"
            "print(t([frozenset({'a', 'b', 'c', 'd'}), {'x', 'y', ('z', 1)}]))"
        )
        said = set()
        for seed in ("1", "2", "3"):
            said.add(
                subprocess.run(
                    [sys.executable, "-c", script],
                    env={**os.environ, "PYTHONHASHSEED": seed},
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
            )
        assert said == {"[frozenset({'a', 'b', 'c', 'd'}), set({'x', 'y', ('z', 1, )})]"}

    def test_a_lock_tells_one_list_held_twice_from_two(self):
        inner = [1]
        shared = _always_called(constant, ins={"v": A, "value": [inner, inner]})
        apart = _always_called(constant, ins={"v": A, "value": [[1], [1]]})
        assert program_hash(shared) != program_hash(apart)

    def test_a_list_holding_itself_has_a_text(self):
        cycle: list = []
        cycle.append(cycle)
        assert _text_for_the_lock(cycle) == "[<as 0>]"

    def test_a_lock_takes_a_function_with_no_source_by_its_name(self):
        """A def nobody can read passes the gate: only a text can say what's inside it."""

        def made_as(name: str):
            scope: dict = {}
            exec(f"def {name}(v):\n    return {{'out': v}}\n", scope)
            return scope[name]

        def lock_of(function) -> str:
            return program_hash(_always_called(function, ins={"v": A}))

        made = made_as("made")
        assert not calls_that_depend_on_more_than_their_inputs(_always_called(made, ins={"v": A}))
        assert lock_of(made) == lock_of(made_as("made"))
        assert lock_of(made) != lock_of(made_as("other"))

    def test_what_has_no_text_gets_its_name_and_no_address(self):
        kept = _text_for_the_lock(_Thing())
        assert kept == _text_for_the_lock(_Thing())
        assert "0x" not in kept
        assert "_Thing" in kept

    def test_analysis_takes_any_constant(self):
        for value in (_Thing(), _LooksLikeFive(), (A, B), math.inf, {1}, [0], {}, DS.select(1, 2)):
            program = _always_called(constant, ins={"v": A, "value": value})
            assert compile_kernel(program, blockless=True, proof_metadata=True).source
            assert program_hash(program)
            CompiledPLC(program, dt=0.01)

    def test_a_lock_reads_a_constant_and_outlives_an_objects_address(self):
        def locked(value) -> str:
            return program_hash(_always_called(constant, ins={"v": A, "value": value}))

        assert locked([1]) == locked([1])
        assert locked([1]) != locked([2])
        assert locked(_Thing()) == locked(_Thing())
        assert locked(_Thing()) != locked(_LooksLikeFive())


def names_it_got(**kw):
    return {"out": len(next(iter(kw)))}


def names_it_got_enabled(enabled, **kw):
    return {"out": len(next(iter(kw)))}


def has_the_angstrom_sign(**kw):
    return {"out": int("Å" in kw)}


def result_1(v):
    return {"out": v + 3}


def result_2(v):
    return {"out": v + 4}


def result_1_enabled(enabled, v):
    return {"out": v + 3}


class TestNames:
    @pytest.mark.parametrize("engine", ENGINES)
    @pytest.mark.parametrize(("fn", "expected"), [(result_1, 8), (result_2, 9)])
    def test_a_function_can_be_named_like_what_holds_its_result(
        self, engine, fn, expected, monkeypatch
    ):
        program = _always_called(fn, ins={"v": First})
        assert _run_on(program, engine, (B,), monkeypatch) == (expected,)

    @pytest.mark.parametrize("engine", ENGINES)
    def test_an_enabled_function_can_be_named_so_too(self, engine, monkeypatch):
        program = _always_called(result_1_enabled, ins={"v": First}, enabled=True)
        assert _run_on(program, engine, (B,), monkeypatch) == (8,)

    @pytest.mark.parametrize(
        "name, length",
        [
            ("class", 5),
            ("a b", 3),
            ("not", 3),
            ("1a", 2),
            ("a-b", 3),
            ("'", 1),
            ('"', 1),
            ("__debug__", 9),
        ],
    )
    @pytest.mark.parametrize("engine", ENGINES)
    def test_a_name_no_keyword_could_spell_arrives(self, engine, name, length, monkeypatch):
        program = _always_called(names_it_got, ins={name: A})
        assert _run_on(program, engine, (B,), monkeypatch) == (length,)

    @pytest.mark.parametrize("engine", ENGINES)
    def test_an_enabled_function_takes_such_a_name_too(self, engine, monkeypatch):
        program = _always_called(names_it_got_enabled, ins={"a b": A}, enabled=True)
        assert _run_on(program, engine, (B,), monkeypatch) == (3,)

    @pytest.mark.parametrize("engine", ENGINES)
    def test_a_name_python_would_normalize_arrives_as_written(self, engine, monkeypatch):
        program = _always_called(has_the_angstrom_sign, ins={"Å": A})
        assert _run_on(program, engine, (B,), monkeypatch) == (1,)

    @pytest.mark.parametrize("name", ["class", "a b", "__debug__"])
    def test_a_run_goes_on_where_stepping_does(self, name):
        def make():
            return _called(names_it_got, ins={name: A})

        assert _has_a_kernel(make())
        assert _a_run_lands_where_stepping_does(make, B) == (len(name),)

    @pytest.mark.parametrize(
        "key",
        ['a"b', "a{b", "a}b", "a\\b", "a\nb", "a'b"],
        ids=["dq", "open", "close", "bs", "nl", "sq"],
    )
    @pytest.mark.parametrize("enabled", [False, True], ids=["run", "enabled"])
    @pytest.mark.parametrize("engine", ENGINES)
    def test_any_out_key_lands(self, engine, enabled, key, monkeypatch):
        fn = _keyed_enabled if enabled else _keyed
        program = _always_called(fn, ins={"v": key}, outs={key: B}, enabled=enabled)
        assert _run_on(program, engine, (B,), monkeypatch) == (3,)

    @pytest.mark.parametrize("enabled", [False, True], ids=["run", "enabled"])
    @pytest.mark.parametrize("engine", ENGINES)
    def test_a_missing_key_says_what_stepping_says(self, engine, enabled, monkeypatch):
        key = 'a"{b\\'
        fn = _empty_enabled if enabled else _empty
        program = _always_called(fn, ins={"v": A}, outs={key: B}, enabled=enabled)
        prefix = "run_enabled_function" if enabled else "run_function"
        fn_name = fn.__name__
        expected = f"{prefix}: {fn_name!r} missing key {key!r}; got []"
        with pytest.raises(KeyError) as raised:
            _run_on(program, engine, (B,), monkeypatch)
        assert raised.value.args[0] == expected


def _keyed(v):
    return {v: 3}


def _keyed_enabled(enabled, v):
    return {v: 3}


def _empty(v):
    return {}


def _empty_enabled(enabled, v):
    return {}
