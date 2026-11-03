"""The text a ``run_function`` body is embedded as, and whether that text is the function.

A generated ``code.py`` carries a function's source, not the function: it is the one place
a copy exists, since a replay kernel runs in this process and calls the function itself.
Source is read from the file, so it can be the wrong text altogether (edited since the
import, or a line shift that lands on another function), and taking a method or a nested
function out of its indentation can change what its string literals say.  So the text is
taken with care here, and then compiled and compared with the function it stands for:
bytecode, constants and names, recursively.
"""

from __future__ import annotations
import __future__ as future_flags

import ast
import inspect
import symtable
import textwrap
from types import CodeType
from typing import Any

#: ``co_flags`` that change how code runs. The rest (nested, future imports) say where it
#: was compiled, and an embedded copy is compiled somewhere else by definition.
_SEMANTIC_CO_FLAGS = (
    inspect.CO_OPTIMIZED
    | inspect.CO_NEWLOCALS
    | inspect.CO_VARARGS
    | inspect.CO_VARKEYWORDS
    | inspect.CO_GENERATOR
    | inspect.CO_COROUTINE
    | inspect.CO_ITERABLE_COROUTINE
    | inspect.CO_ASYNC_GENERATOR
)


def _template_string_types() -> tuple[type, ...]:
    template = getattr(ast, "TemplateStr", None)  # 3.14
    return (template,) if template is not None else ()


def _lines_inside_a_literal(source: str) -> set[int]:
    """Indices of *source*'s lines that begin inside a string: their whitespace is its text."""
    literal_types = (ast.Constant, ast.JoinedStr, *_template_string_types())
    inside: set[int] = set()
    for node in ast.walk(ast.parse("if 1:\n" + source)):
        if not isinstance(node, ast.expr) or not isinstance(node, literal_types):
            continue
        if isinstance(node, ast.Constant) and not isinstance(node.value, str | bytes):
            continue
        # Line numbers count the "if 1:" line, so each one is the 0-based index of the
        # source line after it: where the inside of the string starts, and stops.
        inside.update(range(node.lineno - 1, (node.end_lineno or node.lineno) - 1))
    return inside


def _out_of_its_margin(source: str) -> str:
    first_line = source.split("\n", 1)[0]
    margin = len(first_line) - len(first_line.lstrip(" \t"))
    if margin == 0:
        return source
    try:
        inside = _lines_inside_a_literal(source)
    except SyntaxError:
        return textwrap.dedent(source)
    lines = source.split("\n")
    for index, line in enumerate(lines):
        if index not in inside:
            lines[index] = line[min(margin, len(line) - len(line.lstrip(" \t"))) :]
    return "\n".join(lines)


def _without_staticmethod(source: str) -> str:
    """Drop a ``@staticmethod`` line: outside its class the plain function is what it was."""
    try:
        body = ast.parse(source).body
    except SyntaxError:
        return source
    if len(body) != 1 or not isinstance(body[0], ast.FunctionDef | ast.AsyncFunctionDef):
        return source
    decorators = body[0].decorator_list
    if len(decorators) != 1:
        return source
    only = decorators[0]
    if not isinstance(only, ast.Name) or only.id != "staticmethod":
        return source
    return "\n".join(source.split("\n")[body[0].lineno - 1 :])


def embedded_source(fn: Any) -> str:
    """The text to embed for *fn*: its source at column 0, its string literals untouched.

    Raises what ``inspect.getsource`` raises.  The text is only as good as the file: ask
    ``is_the_function`` before trusting it.
    """
    return _without_staticmethod(_out_of_its_margin(inspect.getsource(fn).rstrip()))


def _names_in(code: CodeType) -> set[str]:
    names = set(code.co_names)
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            names |= _names_in(constant)
    return names


def _imports_it_names(code: CodeType) -> str:
    """``import`` lines for each name in *code* that its file imports at module level.

    The compiler asks that of a name before it calls a method on it, so ``math.floor(v)``
    is other bytecode, with the same meaning, once ``import math`` is out of sight.
    """
    try:
        lines, _ = inspect.findsource(code)
        table = symtable.symtable("".join(lines), code.co_filename, "exec")
    except (OSError, SyntaxError, ValueError):
        return ""
    names = _names_in(code)
    return "".join(
        f"import {symbol.get_name()}\n"
        for symbol in table.get_symbols()
        if symbol.is_imported() and symbol.get_name() in names
    )


def is_the_function(text: str, fn: Any) -> bool:
    """Whether *text*, compiled as *fn* was, defines exactly the code *fn* runs."""
    code = getattr(fn, "__code__", None)
    if not isinstance(code, CodeType):
        return False
    flags = code.co_flags & future_flags.annotations.compiler_flag
    try:
        tree = ast.parse(text)
        module = compile(
            _imports_it_names(code) + text, "<embedded>", "exec", flags=flags, dont_inherit=True
        )
    except (SyntaxError, ValueError):
        return False
    if (
        len(tree.body) != 1
        or not isinstance(tree.body[0], ast.FunctionDef | ast.AsyncFunctionDef)
        or tree.body[0].decorator_list
    ):
        return False
    defined = [
        constant
        for constant in module.co_consts
        if isinstance(constant, CodeType) and constant.co_name == code.co_name
    ]
    return len(defined) == 1 and _same_code(defined[0], code)


def _same_code(left: CodeType, right: CodeType) -> bool:
    return (
        left.co_code == right.co_code
        and left.co_names == right.co_names
        and left.co_varnames == right.co_varnames
        and left.co_freevars == right.co_freevars
        and left.co_cellvars == right.co_cellvars
        and left.co_argcount == right.co_argcount
        and left.co_posonlyargcount == right.co_posonlyargcount
        and left.co_kwonlyargcount == right.co_kwonlyargcount
        and left.co_nlocals == right.co_nlocals
        and left.co_stacksize == right.co_stacksize
        and left.co_name == right.co_name
        and left.co_exceptiontable == right.co_exceptiontable
        and left.co_flags & _SEMANTIC_CO_FLAGS == right.co_flags & _SEMANTIC_CO_FLAGS
        and _same_constant(left.co_consts, right.co_consts)
    )


def _same_constant(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, CodeType):
        return _same_code(left, right)
    if isinstance(left, tuple):
        return len(left) == len(right) and all(
            _same_constant(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, frozenset):
        return sorted(map(repr, left)) == sorted(map(repr, right))
    return repr(left) == repr(right)  # repr: 0.0 and -0.0 are not the same constant.
