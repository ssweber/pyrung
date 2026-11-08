"""How many more times the guarded loop repeats a cycle of its paths, found without running it.

Under a bang-bang hold the loop hands over between a few of its paths in the same order,
again and again.  A slow timer or the clock moves a whole number by the same amount on each
turn of that order, and nothing else changes from one turn to the next.  This module takes
one turn, a *cycle*, and says how many turns run exactly as it does.  The caller can then
check that claim against the scans it runs, and later move the state that many turns at once.

Why the prediction is exact
---------------------------

A cycle is a list of (loop path, scans) in order, and the state it starts from.  Each loop
iteration is the scan it stands for, wherever the path's constants hold and its guards pass
(the argument in :mod:`~pyrung.core.scan_trace`).  So a cycle that starts from a state where
every constant holds and every guard passes, on every scan, ends in the state stepping
would reach.

The prediction takes three steps, on values rather than on the kernel:

1. Once on the state now, which gives the state one cycle later.  Each cell the cycle loads
   either ends where it began or, as a whole number, moves by some step.  Any other change
   declines.
2. Once more on a symbolic state where each moving cell holds ``a + b * k`` on cycle k.  Each
   operation of each loop iteration is done on these forms.  Adding, subtracting and
   multiplying by a constant keep a form a form.  Each decision on a form is taken as on
   cycle 0, and the last cycle k on which every decision so far comes out the same is kept.
   A decision is a comparison, a truth test, ``min``, ``max``, ``abs``, a mask, or a floor
   division or remainder by a constant.  Each decision moves with k one way only, or is an
   equality met at one k at most, so that last k is exact.  Any other operation on a form
   declines.  The stop predicate is asked on the values each scan would show it.
3. The clock and RTC values come from the functions the loop uses.  The first two cycles
   give each value as a constant or a form.  Every scan of every later cycle up to the last
   kept is then computed, and the first that strays from its form ends the prediction one
   cycle before it.  Up to that cycle the forms were the true values, so each decision the
   second run took holds there.

If the second run ends with each cell at its form one cycle on, then for each k up to the
last kept, cycle k takes every decision cycle 0 takes.  So cycle k maps the state of cycle k
to that of cycle k + 1, and by induction each of those cycles runs as cycle 0 does.  A path's
constant on a cell no path of the cycle writes holds on every cycle once it holds now.

Why a jump lands where stepping does
------------------------------------

A run jumps only at the start of a cycle k the prediction covers, and first checks that
each cell there holds its form on k.  It then skips the cycles from k to the last kept, as far
as the next checkpoint goes, so each checkpoint still holds a state a scan left.

- Each moving cell takes its form on the cycle it lands on.  Every other cell already holds
  the value the prediction found for it.
- The timestamp adds dt once for each scan skipped, in order, as the loop adds it.  So the
  float is the one stepping reaches.
- The scan hooks read their cells between scans.  The jump declines if one of those moves,
  so the hooks stay quiet on every scan skipped, as they were on the cycles run.
- The stop predicate said no on every scan of the cycles kept.
- The records of the skipped cycles go in as a repeat of the last cycle.  The repeat reads
  the last few cycles of the records, so a jump waits until three cycles of this prediction
  have run (``WARM_CYCLES``).
"""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pyrung.core.scan_trace import (
    _PURE_BUILTINS,
    _PURE_MATH,
    MISSING,
    LoopShape,
    _Guard,
    same,
)
from pyrung.core.system_points import clock_high


class Lin:
    """The whole number ``a + b * k`` on cycle k.  ``b`` is never 0."""

    __slots__ = ("a", "b")

    def __init__(self, a: int, b: int) -> None:
        self.a = a
        self.b = b

    def at(self, k: int) -> int:
        return self.a + self.b * k

    def __repr__(self) -> str:
        return f"Lin({self.a}, {self.b})"


class Unsupported(Exception):
    """An operation on a form that this module does not reason about."""


class _Bound:
    """The last cycle on which every decision taken so far comes out as on cycle 0."""

    __slots__ = ("last",)

    def __init__(self, last: int) -> None:
        self.last = last


def _form(a: int, b: int) -> Any:
    return a if b == 0 else Lin(a, b)


def _whole(value: Any) -> bool:
    return type(value) is int or type(value) is bool


def _first_equal(x: Lin, value: Any) -> int | None:
    """The first cycle k >= 1 on which *x* equals *value*, or None if it never does."""
    if type(value) is float:
        if not value.is_integer():
            return None
        value = int(value)
    elif not _whole(value):
        return None
    gap = int(value) - x.a
    if gap % x.b:
        return None
    k = gap // x.b
    return k if k >= 1 else None


_ORDER = {"lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge}


def _decide(bound: _Bound, x: Lin, name: str, value: Any) -> bool:
    """Compare *x* with a constant, as on cycle 0, and keep the cycles that agree."""
    if name in ("eq", "ne"):
        equal = x.a == value
        if equal:
            bound.last = min(bound.last, 0)
        else:
            hit = _first_equal(x, value)
            if hit is not None:
                bound.last = min(bound.last, hit - 1)
        return equal if name == "eq" else not equal
    compare = _ORDER[name]
    if not (_whole(value) or type(value) is float):
        raise Unsupported(f"comparison with a {type(value).__name__}")
    return _kept(bound, x, lambda at: compare(at, value))


def _kept(bound: _Bound, x: Lin, monotone: Callable[[int], Any]) -> Any:
    """``monotone(x)`` on cycle 0, keeping the cycles on which it is the same.

    *monotone* must never go back as its argument moves one way.  x moves one way too, so
    the cycles that agree with cycle 0 come first, and a bisection finds the last of them.
    """
    first = monotone(x.a)
    last = bound.last
    if last <= 0 or monotone(x.at(last)) == first:
        return first
    low, high = 0, last
    while high - low > 1:
        middle = (low + high) // 2
        if monotone(x.at(middle)) == first:
            low = middle
        else:
            high = middle
    bound.last = low
    return first


def _difference(left: Any, right: Any) -> Any:
    a_left, b_left = (left.a, left.b) if type(left) is Lin else (left, 0)
    a_right, b_right = (right.a, right.b) if type(right) is Lin else (right, 0)
    if not (_whole(a_left) and _whole(a_right)):
        raise Unsupported("comparison of a form with a value that is not a whole number")
    return _form(int(a_left) - int(a_right), b_left - b_right)


_COMPARE_OPERATORS: dict[str, Callable[[Any, Any], Any]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
    "is_": operator.is_,
    "is_not": operator.is_not,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
}
_MIRRORED = {"eq": "eq", "ne": "ne", "lt": "gt", "le": "ge", "gt": "lt", "ge": "le"}


def _cmp(bound: _Bound, name: str, left: Any, right: Any) -> bool:
    left_moves = type(left) is Lin
    right_moves = type(right) is Lin
    if not left_moves and not right_moves:
        return _COMPARE_OPERATORS[name](left, right)
    if name in ("in", "not_in"):
        if right_moves:
            raise Unsupported("membership in a form")
        inside = left.a in right
        if inside:
            bound.last = min(bound.last, 0)
        else:
            for element in right:
                hit = _first_equal(left, element)
                if hit is not None:
                    bound.last = min(bound.last, hit - 1)
        return inside if name == "in" else not inside
    if name not in _MIRRORED:
        raise Unsupported(f"{name} on a form")
    if left_moves and right_moves:
        difference = _difference(left, right)
        if type(difference) is not Lin:
            return _COMPARE_OPERATORS[name](difference, 0)
        return _decide(bound, difference, name, 0)
    if left_moves:
        return _decide(bound, left, name, right)
    return _decide(bound, right, _MIRRORED[name], left)


def _truth(bound: _Bound, value: Any) -> bool:
    if type(value) is Lin:
        return _decide(bound, value, "ne", 0)
    return bool(value)


_BINARY_OPERATORS: dict[str, Callable[[Any, Any], Any]] = {
    "add": operator.add,
    "sub": operator.sub,
    "mul": operator.mul,
    "truediv": operator.truediv,
    "floordiv": operator.floordiv,
    "mod": operator.mod,
    "pow": operator.pow,
    "and_": operator.and_,
    "or_": operator.or_,
    "xor": operator.xor,
    "lshift": operator.lshift,
    "rshift": operator.rshift,
}


def _parts(value: Any) -> tuple[int, int]:
    if type(value) is Lin:
        return value.a, value.b
    if not _whole(value):
        raise Unsupported(f"arithmetic of a form with a {type(value).__name__}")
    return int(value), 0


def _bin(bound: _Bound, name: str, left: Any, right: Any) -> Any:
    if type(left) is not Lin and type(right) is not Lin:
        return _BINARY_OPERATORS[name](left, right)
    if name == "add":
        (a, b), (c, d) = _parts(left), _parts(right)
        return _form(a + c, b + d)
    if name == "sub":
        (a, b), (c, d) = _parts(left), _parts(right)
        return _form(a - c, b - d)
    if name == "mul":
        (a, b), (c, d) = _parts(left), _parts(right)
        if b and d:
            raise Unsupported("a form times a form")
        return _form(a * c, b * c + d * a)
    if name in ("floordiv", "mod") and type(left) is Lin and _whole(right) and int(right):
        divisor = int(right)
        if left.b % divisor == 0:
            # The quotient moves by a whole step, and the remainder stays.
            if name == "mod":
                return left.a % divisor
            return Lin(left.a // divisor, left.b // divisor)
        # Otherwise keep the cycles with the same quotient.  It moves one way, as the value does.
        quotient = _kept(bound, left, lambda value: value // divisor)
        return quotient if name == "floordiv" else Lin(left.a - quotient * divisor, left.b)
    if name == "and_" and type(left) is Lin and _whole(right):
        mask = int(right)
        # A mask of low bits keeps a value from 0 up to the mask as it is.
        if mask >= 0 and (mask + 1) & mask == 0 and 0 <= left.a <= mask:
            _decide(bound, left, "ge", 0)
            _decide(bound, left, "le", mask)
            return left
    raise Unsupported(f"{name} on a form")


def _unary(bound: _Bound, name: str, value: Any) -> Any:
    if name == "not_":
        return not _truth(bound, value)
    if type(value) is not Lin:
        return {"neg": operator.neg, "pos": operator.pos, "invert": operator.invert}[name](value)
    if name == "neg":
        return Lin(-value.a, -value.b)
    if name == "pos":
        return value
    return Lin(-value.a - 1, -value.b)


_PURE: dict[str, Callable[..., Any]] = {
    **{name: pure.function for name, pure in _PURE_BUILTINS.items()},
    **{pure.spelling: pure.function for pure in _PURE_MATH.values()},
}


def _call(bound: _Bound, name: str, *arguments: Any) -> Any:
    if not any(type(argument) is Lin for argument in arguments):
        return _PURE[name](*arguments)
    first = arguments[0]
    if name == "int" and len(arguments) == 1:
        return first
    if name == "bool" and len(arguments) == 1:
        return _truth(bound, first)
    if name == "type" and len(arguments) == 1:
        return int
    if name == "isinstance" and len(arguments) == 2 and type(first) is Lin:
        return isinstance(first.a, arguments[1])
    if name == "abs" and len(arguments) == 1:
        if first.a >= 0:
            _decide(bound, first, "ge", 0)
            return first
        _decide(bound, first, "lt", 0)
        return Lin(-first.a, -first.b)
    if name in ("min", "max") and len(arguments) == 2:
        left, right = arguments
        # Python's min keeps the first unless the second is less.
        take_right = _cmp(bound, "lt" if name == "min" else "gt", right, left)
        return right if take_right else left
    raise Unsupported(f"{name}() of a form")


def _or(bound: _Bound, first: Any, *rest: Callable[[], Any]) -> Any:
    value = first
    for later in rest:
        if _truth(bound, value):
            return value
        value = later()
    return value


def _and(bound: _Bound, first: Any, *rest: Callable[[], Any]) -> Any:
    value = first
    for later in rest:
        if not _truth(bound, value):
            return value
        value = later()
    return value


def _if(bound: _Bound, test: Any, body: Callable[[], Any], orelse: Callable[[], Any]) -> Any:
    return body() if _truth(bound, test) else orelse()


_AST_BINARY: dict[type[ast.operator], str] = {
    ast.Add: "add",
    ast.Sub: "sub",
    ast.Mult: "mul",
    ast.Div: "truediv",
    ast.FloorDiv: "floordiv",
    ast.Mod: "mod",
    ast.Pow: "pow",
    ast.BitAnd: "and_",
    ast.BitOr: "or_",
    ast.BitXor: "xor",
    ast.LShift: "lshift",
    ast.RShift: "rshift",
}
_AST_COMPARE: dict[type[ast.cmpop], str] = {
    ast.Eq: "eq",
    ast.NotEq: "ne",
    ast.Lt: "lt",
    ast.LtE: "le",
    ast.Gt: "gt",
    ast.GtE: "ge",
    ast.Is: "is_",
    ast.IsNot: "is_not",
    ast.In: "in",
    ast.NotIn: "not_in",
}
_AST_UNARY: dict[type[ast.unaryop], str] = {
    ast.Not: "not_",
    ast.USub: "neg",
    ast.UAdd: "pos",
    ast.Invert: "invert",
}


def _helper(name: str, *arguments: ast.expr) -> ast.expr:
    return ast.Call(ast.Name(name, ast.Load()), [ast.Name("R", ast.Load()), *arguments], [])


def _deferred(node: ast.expr) -> ast.expr:
    return ast.Lambda(
        ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]), node
    )


class _Rewrite(ast.NodeTransformer):
    """Each operation of a loop expression as a call that also takes forms."""

    def __init__(self, names: frozenset[str]) -> None:
        self._names = names

    def generic_visit(self, node: ast.AST) -> ast.AST:
        raise Unsupported(f"loop expression {type(node).__name__}")

    def visit_Expression(self, node: ast.Expression) -> ast.AST:
        node.body = self.visit(node.body)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id not in self._names:
            raise Unsupported(f"loop name {node.id}")
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        name = _AST_BINARY[type(node.op)]
        return _helper("_bin", ast.Constant(name), self.visit(node.left), self.visit(node.right))

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        return _helper("_unary", ast.Constant(_AST_UNARY[type(node.op)]), self.visit(node.operand))

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        if len(node.ops) != 1:
            raise Unsupported("chained comparison")
        name = _AST_COMPARE[type(node.ops[0])]
        left, right = self.visit(node.left), self.visit(node.comparators[0])
        return _helper("_cmp", ast.Constant(name), left, right)

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        values = [self.visit(value) for value in node.values]
        name = "_and" if isinstance(node.op, ast.And) else "_or"
        return _helper(name, values[0], *(_deferred(value) for value in values[1:]))

    def visit_IfExp(self, node: ast.IfExp) -> ast.AST:
        test, body, orelse = self.visit(node.test), self.visit(node.body), self.visit(node.orelse)
        return _helper("_if", test, _deferred(body), _deferred(orelse))

    def visit_Call(self, node: ast.Call) -> ast.AST:
        function = node.func
        if node.keywords:
            raise Unsupported("call with keywords")
        if isinstance(function, ast.Name) and function.id in _PURE_BUILTINS:
            spelling = function.id
        elif (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and f"{function.value.id}.{function.attr}" in _PURE
        ):
            spelling = f"{function.value.id}.{function.attr}"
        else:
            raise Unsupported("call of a name not known to be pure")
        arguments = [self.visit(argument) for argument in node.args]
        return _helper("_call", ast.Constant(spelling), *arguments)

    def visit_Tuple(self, node: ast.Tuple) -> ast.AST:
        node.elts = [self.visit(element) for element in node.elts]
        return node


def _rewritten(expression: str, names: frozenset[str]) -> str:
    tree = ast.parse(expression, mode="eval")
    return ast.unparse(ast.fix_missing_locations(_Rewrite(names).visit(tree)))


def _names_in(expression: str) -> frozenset[str]:
    tree = ast.parse(expression, mode="eval")
    return frozenset(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))


class Iteration:
    """A loop iteration that runs on values and forms.  ``run(S, T, R)`` is False if a guard fails.

    ``S`` maps each cell to its value, and the commits go back into it.  ``T`` maps each
    clock and RTC local to its value on this scan.  Only the operations that may see a form
    go through the helpers.  The others run as the loop spells them, which is much faster.
    """

    def __init__(self, shape: LoopShape) -> None:
        self.shape = shape
        locals_ = {local for _cell, local in shape.loads}
        locals_ |= {local for local, _half in shape.clocks}
        locals_ |= {local for local, _field in shape.rtc}
        locals_ |= {op.target for op in shape.body if not isinstance(op, _Guard)}
        self._names = frozenset(locals_ | set(shape.constants))
        self._body = [
            (op, _names_in(op.leave_if if isinstance(op, _Guard) else op.expression))
            for op in shape.body
        ]
        self._commits = [(local, value, _names_in(value)) for local, value in shape.commits]
        used: set[str] = set()
        for _op, uses in self._body:
            used |= uses
        for _local, _value, uses in self._commits:
            used |= uses
        self.cells = tuple(cell for cell, _local in shape.loads)
        self.clocks = tuple((local, half) for local, half in shape.clocks if local in used)
        self.rtc = tuple((local, name) for local, name in shape.rtc if local in used)
        self._loads = tuple(shape.loads)
        self._variants: dict[frozenset[str], Callable[..., bool]] = {}

    def run(self, S: dict[Any, Any], T: Mapping[str, Any], R: _Bound) -> bool:
        moving = frozenset(
            [local for cell, local in self._loads if type(S[cell]) is Lin]
            + [local for local, value in T.items() if type(value) is Lin]
        )
        function = self._variants.get(moving)
        if function is None:
            function = self._variants[moving] = self._variant(moving)
        return function(S, T, R)

    def _variant(self, moving: frozenset[str]) -> Callable[..., bool]:
        """The iteration for when the locals in *moving* hold forms.  Raises Unsupported."""
        shape, names = self.shape, self._names
        lines = ["def _iteration(S, T, R):"]
        namespace: dict[str, Any] = dict(shape.constants)
        for position, (cell, local) in enumerate(shape.loads):
            namespace[f"C{position}"] = cell
            lines.append(f"    {local} = S[C{position}]")
        for local, _half in shape.clocks:
            lines.append(f"    {local} = T.get({local!r})")
        for local, _field in shape.rtc:
            lines.append(f"    {local} = T.get({local!r})")
        # The locals that may hold a form.  Any operation on one of them may give a form.
        tainted = set(moving)
        for op, uses in self._body:
            on_form = bool(uses & tainted)
            if isinstance(op, _Guard):
                test = f"_truth(R, {_rewritten(op.leave_if, names)})" if on_form else op.leave_if
                lines.append(f"    if {test}: return False")
                continue
            if on_form:
                lines.append(f"    {op.target} = {_rewritten(op.expression, names)}")
                tainted.add(op.target)
            else:
                lines.append(f"    {op.target} = {op.expression}")
                tainted.discard(op.target)
        if self._commits:
            cell_of = {local: position for position, (_cell, local) in enumerate(shape.loads)}
            targets = ", ".join(f"S[C{cell_of[local]}]" for local, _value, _uses in self._commits)
            values = ", ".join(
                _rewritten(value, names) if uses & tainted else value
                for _local, value, uses in self._commits
            )
            lines.append(f"    {targets}, = {values},")
        lines.append("    return True")
        # The loop's own names, so an operation spelled as the loop spells it means the same.
        namespace.update(math=math, same=same, clock_high=clock_high)
        namespace.update(
            _truth=_truth, _cmp=_cmp, _bin=_bin, _unary=_unary, _call=_call, _or=_or, _and=_and
        )
        namespace["_if"] = _if
        exec(compile("\n".join(lines), "<cycle iteration>", "exec"), namespace)  # noqa: S102
        return namespace["_iteration"]


def iteration_of(shape: LoopShape) -> Iteration:
    """*shape*'s iteration, to run on values and forms."""
    return Iteration(shape)


@dataclass(frozen=True)
class Step:
    """One segment of a cycle: a path's iteration, its constants, and how many scans."""

    iteration: Iteration
    constants: tuple[tuple[tuple[Any, ...], Any], ...]
    count: int
    asks: bool  # whether the loop asks the stop predicate after a scan of this path


@dataclass
class Prediction:
    """How many cycles run as the first does, and where they leave each cell."""

    cycles: int
    period: int
    start: dict[tuple[Any, ...], Any]
    steps: dict[tuple[Any, ...], int]  # the cells that move, by how much a cycle
    # Each cell's form at the end of each segment, for the record of a jump.
    segment_ends: list[dict[tuple[Any, ...], Any]]

    def cell_at(self, cell: tuple[Any, ...], k: int) -> Any:
        """The cell's value at the start of cycle *k*."""
        step = self.steps.get(cell)
        return self.start[cell] if step is None else self.start[cell] + step * k


def _value_at(value: Any, k: int) -> Any:
    return value.at(k) if type(value) is Lin else value


class _TimeTable:
    """The clock and RTC values each scan of each cycle shows, as constants or forms."""

    def __init__(
        self,
        steps: list[Step],
        ts: float,
        dt: float,
        rtc_fields: Callable[[float], tuple[Mapping[str, Any], float, float]],
        horizon: int,
    ) -> None:
        self.horizon = horizon
        self.why = ""
        self._reads = [
            (step.iteration.clocks, step.iteration.rtc) for step in steps for _ in range(step.count)
        ]
        self.forms: list[dict[str, Any]] = [{} for _ in self._reads]
        # Cycle 0's values, which the first run of a cycle reads.
        self.firsts: list[dict[str, Any]] = [{} for _ in self._reads]
        self._ts = ts  # the time of the first scan after the cycles computed so far
        self._dt = dt
        self._rtc_fields = rtc_fields
        self._moves = any(clocks or rtc for clocks, rtc in self._reads)
        if not self._moves:
            return
        # The first two cycles give the forms.  confirm() checks the cycles after them.
        for k in range(min(2, horizon)):
            if not self._first_cycle(k):
                self.horizon = k
                return

    def _first_cycle(self, k: int) -> bool:
        ts, dt, rtc_fields = self._ts, self._dt, self._rtc_fields
        rtc_now: Mapping[str, Any] = {}
        rtc_from, rtc_until = 0.0, -1.0
        for position, (clocks, rtc) in enumerate(self._reads):
            values: dict[str, Any] = {}
            for local, half in clocks:
                values[local] = clock_high(ts, half)
            if rtc:
                if not rtc_from <= ts < rtc_until:
                    rtc_now, rtc_from, rtc_until = rtc_fields(ts)
                for local, name in rtc:
                    values[local] = rtc_now[name]
            if not self._fold_in(k, position, values, self.firsts):
                return False
            ts += dt
        self._ts = ts
        return True

    def confirm(self, last: int) -> int:
        """The last cycle up to *last* on which every clock and RTC value lies on its form."""
        if not self._moves or last < 2:
            return last
        checks = [
            (
                tuple((half, forms[local]) for local, half in clocks),
                tuple((name, forms[local]) for local, name in rtc),
            )
            for (clocks, rtc), forms in zip(self._reads, self.forms, strict=True)
        ]
        ts, dt, rtc_fields = self._ts, self._dt, self._rtc_fields
        fields: Mapping[str, Any] = {}
        rtc_from, rtc_until = 0.0, -1.0
        for k in range(2, last + 1):
            for clocks, rtc in checks:
                for half, high in clocks:
                    if clock_high(ts, half) != high:
                        return k - 1
                if rtc:
                    if not rtc_from <= ts < rtc_until:
                        fields, rtc_from, rtc_until = rtc_fields(ts)
                    for name, form in rtc:
                        if not same(fields[name], _value_at(form, k)):
                            return k - 1
                ts += dt
        return last

    def _fold_in(
        self, k: int, position: int, values: dict[str, Any], firsts: list[dict[str, Any]]
    ) -> bool:
        forms = self.forms[position]
        for local, value in values.items():
            if k == 0:
                firsts[position][local] = value
                forms[local] = value
                continue
            form = forms[local]
            if k == 1 and not same(form, value):
                first = firsts[position][local]
                if type(first) is not int or type(value) is not int:
                    self.why = "a clock or RTC value changes from one cycle to the next"
                    return False
                forms[local] = form = Lin(first, value - first)
            if not same(_value_at(form, k), value):
                self.why = "a clock or RTC value stops moving by the same step each cycle"
                return False
        return True


def predict(
    steps: list[Step],
    read_cell: Callable[[tuple[Any, ...]], Any],
    ts: float,
    dt: float,
    rtc_fields: Callable[[float], tuple[Mapping[str, Any], float, float]],
    horizon: int,
    stops: Callable[[Mapping[tuple[Any, ...], Any]], bool],
    stop_cells: frozenset[tuple[Any, ...]],
) -> Prediction | str:
    """How many of the next *horizon* cycles run as the next one does, or why none can be told.

    *read_cell* reads the state now, at the start of the cycle.  *stops* is the stop
    predicate, given the values of those of *stop_cells* the cycle loads.
    """
    cells: dict[tuple[Any, ...], None] = {}
    for step in steps:
        cells.update(dict.fromkeys(step.iteration.cells))
    start = {cell: read_cell(cell) for cell in cells}
    if any(value is MISSING for value in start.values()):
        return "a cell the cycle loads is missing"
    # The constants on cells the cycle loads, by path.  The others are checked once, now.
    held: dict[int, tuple[tuple[tuple[Any, ...], Any], ...]] = {}
    for step in steps:
        if id(step.constants) in held:
            continue
        held[id(step.constants)] = tuple(
            (cell, value) for cell, value in step.constants if cell in cells
        )
        for cell, value in step.constants:
            if cell not in cells and not same(read_cell(cell), value):
                return "a path's constant does not hold"
    times = _TimeTable(steps, ts, dt, rtc_fields, horizon)
    if times.horizon < 1:
        return times.why or "no cycle left"
    # Cycle 0, on the values now.
    state = dict(start)
    asked = _Asked(stops, stop_cells & cells.keys())
    if not _run(steps, held, state, times.firsts, _Bound(0), asked, None):
        return "the next cycle does not run as the last one"
    moving: dict[tuple[Any, ...], int] = {}
    for cell, before in start.items():
        after = state[cell]
        if same(before, after):
            continue
        if type(before) is not int or type(after) is not int:
            return "a value other than a whole number changes over a cycle"
        moving[cell] = after - before
    after_one = state
    # Every cycle at once, on forms.
    state = {cell: _form(value, moving.get(cell, 0)) for cell, value in start.items()}
    bound = _Bound(times.horizon - 1)
    ends: list[dict[tuple[Any, ...], Any]] = []
    try:
        if not _run(steps, held, state, times.forms, bound, asked, ends):
            return "the next cycle does not run as the last one"
    except Unsupported as exc:
        return f"unsupported: {exc}"
    for cell, value in state.items():
        step = moving.get(cell, 0)
        if step:
            if type(value) is not Lin or value.a != after_one[cell] or value.b != step:
                return "a cell does not move by the same step each cycle"
        elif type(value) is Lin or not same(value, after_one[cell]):
            return "a cell does not move by the same step each cycle"
    # Each cut keeps the cycles before some cycle, so the cuts can come in any order.  These
    # two cost a call for each cycle, so they come last, after the cheap cuts.
    last = asked.last_running(times.confirm(bound.last))
    if last < 0:
        return "the stop predicate ends the next cycle"
    period = sum(step.count for step in steps)
    return Prediction(last + 1, period, start, moving, ends)


class _Asked:
    """The stop predicate, and the values it is shown that move with the cycle."""

    def __init__(
        self, stops: Callable[[Mapping[tuple[Any, ...], Any]], bool], cells: frozenset[Any]
    ) -> None:
        self.stops = stops
        self.cells = cells
        self.moving: dict[tuple[tuple[Any, int, int], ...], dict[tuple[Any, ...], Any]] = {}

    def keeps_going(self, state: dict[tuple[Any, ...], Any]) -> bool:
        """False if the predicate stops the run on this scan of cycle 0."""
        shown = {cell: state[cell] for cell in self.cells}
        if not any(type(value) is Lin for value in shown.values()):
            return not self.stops(shown)
        # The predicate is asked for each cycle once the other cuts are known.
        key = tuple(
            (cell, value.a, value.b) if type(value) is Lin else (cell, value, 0)
            for cell, value in shown.items()
        )
        self.moving.setdefault(key, shown)
        return True

    def last_running(self, last: int) -> int:
        """The last cycle up to *last* on which the predicate stops no scan."""
        for k in range(last + 1):
            for shown in self.moving.values():
                if self.stops({cell: _value_at(value, k) for cell, value in shown.items()}):
                    return k - 1
        return last


def _run(
    steps: list[Step],
    held: dict[int, tuple[tuple[tuple[Any, ...], Any], ...]],
    state: dict[tuple[Any, ...], Any],
    times: list[dict[str, Any]],
    bound: _Bound,
    asked: _Asked,
    ends: list[dict[tuple[Any, ...], Any]] | None,
) -> bool:
    """Run one cycle on *state*.  False if cycle 0 itself leaves the loop."""
    position = 0
    for step in steps:
        iteration = step.iteration
        constants = held[id(step.constants)]
        for _scan in range(step.count):
            for cell, value in constants:
                if not _holds(bound, state[cell], value):
                    return False
            try:
                passed = iteration.run(state, times[position], bound)
            except Unsupported:
                raise
            except Exception:
                return False
            if not passed or bound.last < 0:
                return False
            if step.asks and not asked.keeps_going(state):
                return False
            position += 1
        if ends is not None:
            ends.append(dict(state))
    return True


def _holds(bound: _Bound, value: Any, expected: Any) -> bool:
    """Whether a constant holds on cycle 0, keeping the cycles on which it still does."""
    if type(value) is not Lin:
        return same(value, expected)
    if type(expected) is not int or value.a != expected:
        return False
    bound.last = min(bound.last, 0)
    return True
