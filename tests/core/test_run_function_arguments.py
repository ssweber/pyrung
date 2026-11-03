"""What a ``run_function`` is handed and what it returns land as stepping lands them, on every path."""

from __future__ import annotations

import math
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
from pyrung.core.analysis.prove.lockfile import program_hash
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.runner import PLC
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
        assert _a_run_lands_where_stepping_does(make, B) == (SCANS,)

    def test_a_dict_steps_in_a_run_and_grows_on_every_scan(self):
        def make():
            return _called(counts, ins={"v": A, "seen": {}})

        assert not _has_a_kernel(make())
        assert _a_run_lands_where_stepping_does(make, B) == (SCANS,)

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
        assert _a_run_lands_where_stepping_does(make, B) == (SCANS,)

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


class TestNames:
    @pytest.mark.parametrize(
        "name, length",
        [("class", 5), ("a b", 3), ("not", 3), ("1a", 2), ("a-b", 3), ("'", 1), ('"', 1)],
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

    @pytest.mark.parametrize("name", ["class", "a b"])
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
