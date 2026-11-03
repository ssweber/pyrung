"""A kernel and a code.py embed a function's text: that text must be the function."""

from __future__ import annotations

import ast
import importlib
import itertools
import linecache
from pathlib import Path

import pytest

from pyrung import Bool, Int, Program, Rung, calc, run_function
from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.core.function_source import embedded_source, is_the_function
from pyrung.core.runner import PLC

NEVER = Bool("Never")
A, B, C, Run = Int("A"), Int("B"), Int("C"), Bool("Run")
SCANS = 1500

_modules = itertools.count()


def _program(*functions):
    with Program(strict=False) as program:
        with Rung():
            calc(A + 1, A)
        with Rung(Run):
            for fn, out in zip(functions, (B, C), strict=False):
                run_function(fn, ins={"v": A}, outs={"out": out})
    return program


def _run(program, *, stepped: bool) -> PLC:
    plc = PLC(program, dt=0.01)
    plc.patch({"Run": True})
    if stepped:
        for _ in range(SCANS):
            plc.run_until(NEVER, max_cycles=1, fold=False)
    else:
        plc.run_until(NEVER, max_cycles=SCANS)
    return plc


def _seeks_as_it_ran(program, tag: str = "B") -> int:
    """The tag after a run, a stepped twin and a cold seek: all three are the same."""
    ran = _run(program, stepped=False)
    twin = _run(program, stepped=True)
    assert ran.state.tags[tag] == twin.state.tags[tag]
    ran._recent_state_cache.clear()
    ran._replay_slabs.clear()
    scan = ran.state.scan_id - 200
    assert ran.history.at(scan).tags[tag] == twin.history.at(scan).tags[tag]
    return ran.state.tags[tag]


def _has_a_kernel(program) -> bool:
    return PLC(program, dt=0.01)._compiled_replay_supported_kernel() is not None


def _from_code_py(fn, name: str):
    """Call the ``def`` a generated code.py carries for *fn*."""
    hardware = P1AM()
    hardware.slot(1, "P1-08SIM")
    with Program(strict=False) as program:
        with Rung(Run):
            run_function(fn, ins={"v": A}, outs={"out": B})
    code = generate_circuitpy(program, hardware, target_scan_ms=10.0).code
    for node in ast.parse(code).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            namespace: dict = {}
            exec(compile(ast.Module([node], []), "code.py", "exec"), namespace)  # noqa: S102
            return namespace[name](0)
    raise AssertionError(f"no def {name} in code.py")


def _import(tmp_path: Path, monkeypatch, text: str):
    name = f"embedded_source_case_{next(_modules)}"
    path = tmp_path / f"{name}.py"
    path.write_text(text, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    return importlib.import_module(name), path


def _rewrite(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    linecache.checkcache(str(path))


class Holder:
    @staticmethod
    def method(v):
        s = """x
        y"""
        return {"out": len(s)}


def _nested():
    def inner(v):
        s = """x
        y"""
        return {"out": len(s)}

    return inner


@pytest.mark.parametrize("fn", [Holder.method, _nested()], ids=["staticmethod", "nested"])
def test_a_multiline_string_in_an_indented_function_keeps_its_text(fn):
    program = _program(fn)
    assert _has_a_kernel(program)
    assert _seeks_as_it_ran(program) == 11


@pytest.mark.parametrize(
    "fn,name", [(Holder.method, "method"), (_nested(), "inner")], ids=["staticmethod", "nested"]
)
def test_code_py_carries_the_same_string(fn, name):
    assert fn(0) == {"out": 11}
    assert _from_code_py(fn, name) == {"out": 11}


def test_a_whitespace_only_line_in_a_string_at_column_0_is_kept(tmp_path, monkeypatch):
    module, _ = _import(
        tmp_path,
        monkeypatch,
        'def spaces(v):\n    s = """x\n   \ny"""\n    return {"out": len(s)}\n',
    )
    assert module.spaces(0) == {"out": 7}
    assert embedded_source(module.spaces).count("\n   \n") == 1
    assert _seeks_as_it_ran(_program(module.spaces)) == 7
    assert _from_code_py(module.spaces, "spaces") == {"out": 7}


def test_a_source_file_edited_after_the_import_is_not_embedded(tmp_path, monkeypatch):
    module, path = _import(tmp_path, monkeypatch, "def edited(v):\n    return {'out': 1}\n")
    _rewrite(path, "def edited(v):\n    return {'out': 2222}\n")
    program = _program(module.edited)
    assert not is_the_function(embedded_source(module.edited), module.edited)
    assert not _has_a_kernel(program)
    assert _seeks_as_it_ran(program) == 1
    with pytest.raises(ValueError, match="not the function"):
        _from_code_py(module.edited, "edited")


def test_a_line_shift_after_the_import_is_not_embedded(tmp_path, monkeypatch):
    module, path = _import(tmp_path, monkeypatch, "def shifted(v):\n    return {'out': 1}\n")
    _rewrite(
        path,
        "def other(v):\n    return {'out': 99}\n\ndef shifted(v):\n    return {'out': 1}\n",
    )
    program = _program(module.shifted)
    assert not _has_a_kernel(program)
    assert _seeks_as_it_ran(program) == 1
    with pytest.raises(ValueError, match="not the function"):
        _from_code_py(module.shifted, "shifted")


def test_a_closure_is_refused_for_code_py():
    base = 7

    def closes_over(v):
        return {"out": v + base}

    with pytest.raises(ValueError, match="not the function"):
        _from_code_py(closes_over, "closes_over")


def _walks_a_format_in_a_loop(v):
    total = 0
    for spec in ("{0.__module__}",):
        total = len(spec.format(_walks_a_format_in_a_loop))
    return {"out": total}


def _walks_a_format_map(v):
    return {"out": len("{f.__module__}".format_map({"f": _walks_a_format_map}))}


def _builds_the_format_at_run_time(v):
    spec = "{0." + "_" * 2 + "name" + "_" * 2 + "}"
    return {"out": len(spec.format(len))}


@pytest.mark.parametrize(
    "fn", [_walks_a_format_in_a_loop, _walks_a_format_map, _builds_the_format_at_run_time]
)
def test_a_format_method_is_a_route_past_the_functions_scope(fn):
    program = _program(fn)
    assert not _has_a_kernel(program)
    assert _seeks_as_it_ran(program) == fn(0)["out"]


def test_the_format_builtin_stays_with_the_original():
    def reads_format(v):
        return {"out": len(format(v))}

    assert not _has_a_kernel(_program(reads_format))


def test_the_comparison_sees_each_part_of_a_function():
    def source_of(text):
        namespace: dict = {}
        exec(text, namespace)  # noqa: S102
        return namespace["f"]

    fn = source_of("def f(v):\n    return {'out': v + 1}\n")
    same = "def f(v):\n    return {'out': v + 1}\n"
    assert is_the_function(same, fn)
    assert is_the_function("\n\n# moved\n" + same, fn)
    assert not is_the_function(same.replace("v + 1", "v + 2"), fn)
    assert not is_the_function(same.replace("v + 1", "v + 1.0"), fn)
    assert not is_the_function(same.replace("(v)", "(w)").replace("v + 1", "w + 1"), fn)
    assert not is_the_function("@dec\n" + same, fn)
    assert not is_the_function(same + same, fn)
    assert not is_the_function("def f(:\n", fn)

    outer = source_of("def f(v):\n    def g():\n        return 1\n    return {'out': g()}\n")
    assert not is_the_function(
        "def f(v):\n    def g():\n        return 2\n    return {'out': g()}\n", outer
    )


@pytest.mark.parametrize(
    "name,expected",
    [("min", 99), ("math", 99)],
)
def test_a_function_named_like_what_the_kernel_calls_does_not_break_the_kernel(
    tmp_path, monkeypatch, name, expected
):
    module, _ = _import(tmp_path, monkeypatch, f"def {name}(v):\n    return {{'out': 99}}\n")
    caller, _ = _import(tmp_path, monkeypatch, "def caller(v):\n    return {'out': min(v, 5)}\n")
    fn = getattr(module, name)
    program = _program(fn, caller.caller)
    assert not _has_a_kernel(program)
    assert _seeks_as_it_ran(program) == expected
    assert _seeks_as_it_ran(program, "C") == 5
    with pytest.raises(ValueError, match="would shadow"):
        _from_code_py(fn, name)
