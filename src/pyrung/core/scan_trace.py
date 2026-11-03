"""A guarded scan loop over the generated kernel.

A long run spends most of its scans repeating the scan before it, with only a
timer's accumulator and a clock moving.  This module traces one real scan over
the kernel's *generated source*, and emits a tiny loop that repeats that scan:
arithmetic on the values that move, and a guard on every decision that depends
on one.  Nothing is jumped: every scan in the loop runs.  A guard that fails
exits the loop before that scan commits, and an ordinary compiled scan runs it.

Why one loop iteration equals one kernel scan
---------------------------------------------

The kernel is a deterministic function of what it reads: ``tags``, ``blocks``
and ``memory`` (``prev`` is declined, and ``dt`` is fixed for a run).  The tracer
interprets its source statement by statement on the state at trace time, and
every value it reads is one of two things:

* A **constant**: a recorded (key, value) pair.  A trace only runs from a state
  in which every recorded pair still holds, compared by type and value.
* A **moving** value: read from the loop's locals on every iteration.  These are
  the time-derived system tags (:data:`MOVING_SYSTEM_TAGS`), plus every cell the
  scan changes, found by a fixed point: trace, and any cell whose end-of-scan
  value is not exactly the value it read becomes moving; repeat until none is.

So at the fixed point the scan leaves every constant as it found it.  Each value
derived from a moving one is computed in the loop by the same Python operation
the kernel performs, on the same operands.  Each decision taken on one (a branch,
a short circuit, a subscript) becomes a guard requiring the traced outcome.
Therefore, by induction over iterations: if the constants hold on entry and the
guards pass, the kernel would have taken exactly the traced path, so it computes
the loop's values and writes them, and the constants hold again for the next
iteration.  Nothing commits until every guard of the iteration has passed, and any
exception raised by a loop operation exits the loop the same way, so the real
scan decides what happens on that iteration.

The rest of a scan around the kernel functions is shown idle on entry and kept
idle by the loop.  ``step_replay`` also runs the scan hooks, input overrides and
system tag refresh.  Pending patches, forces and ``randint`` draws decline
(:meth:`ScanLoop._blocked` and :meth:`ScanLoop._whole_run_blocker`).  The hooks
must be quiet on entry, and a scan that would change anything they read
(:data:`~pyrung.core.system_points.HOOK_TAG_NAMES`) declines.  Clock and RTC values
are computed in the loop by the functions the real scan uses.

What makes it decline
---------------------

Everything the tracer does not positively know.  Unknown statements, expressions
and calls raise :class:`Decline`; they never guess.  A decline, a failed guard and
an exception all end in the same place: a real compiled scan, which remains the
floor.  In particular:

* a system tag outside the closed classification below, a scan-derived one, or a
  program with forces or ``randint``: plain compiled stepping for the whole run;
* a moving operation that raised while tracing, a moving iterable, a value that
  is not a scalar, a call that is not a generated helper or an allow-listed pure
  builtin (``run_function`` bodies among them);
* a stop predicate form outside :func:`_guardable`.

Self check
----------

The argument above leans on the tracer agreeing with Python about the generated
source.  So the first iteration of each new trace runs on a copy of the state,
and its result is compared with the real compiled scan run from the same state:
every tag, block cell, memory entry, block write record, the scan id and the
timestamp, types included.  A mismatch stops the loop for the rest of the run, and
under ``--fold-check`` (:data:`STRICT_SELF_CHECK`) raises :class:`SelfCheckMismatch`.
"""

from __future__ import annotations

import ast
import logging
import math
import operator
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

from pyrung.circuitpy.codegen._helpers import _HELPER_DEFS
from pyrung.core.bounds import TagConstraint, _check_one
from pyrung.core.compiled_plc import _KernelRuntimeContext, _TrackedList
from pyrung.core.condition import (
    _VALUE_LITERAL,
    _VALUE_TAG,
    AllCondition,
    AnyCondition,
    BitCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
    FallingEdgeCondition,
    IntTruthyCondition,
    NormallyClosedCondition,
    RisingEdgeCondition,
)
from pyrung.core.system_points import (
    _CLOCK_HALF_PERIODS,
    _DERIVED_TAG_NAMES,
    HOOK_MEMORY_KEYS,
    HOOK_TAG_NAMES,
    RTC_FIELD_TAG_NAMES,
    SYSTEM_TAGS_BY_NAME,
    clock_high,
    system,
)

if TYPE_CHECKING:
    from pyrung.core.compiled_plc import CompiledPLC
    from pyrung.core.kernel import CompiledKernel

logger = logging.getLogger(__name__)

# --- The closed classification of system tags ---------------------------------
#
# Every system tag is in exactly one of these three (a test enforces it, and a
# program referencing an unclassified one declines).  Calling a moving tag
# constant would land wrong, so nothing is constant by default: a new tag is
# unclassified until someone decides.

# Derived from the timestamp.  The loop recomputes them on every iteration.
MOVING_SYSTEM_TAGS = frozenset(_CLOCK_HALF_PERIODS) | RTC_FIELD_TAG_NAMES

# Derived from the scan id on every scan.  A program reading one steps.
DECLINING_SYSTEM_TAGS = frozenset(
    {
        system.sys.scan_counter.name,
        system.sys.scan_clock_toggle.name,
    }
)

# Held in tags and rewritten only by a kernel write (which the tracer sees), by a
# scan hook (which must be quiet), or derived from memory the hooks own.
# ``first_scan`` is False from scan 1 on, and the loop refuses scan 0.
CONSTANT_SYSTEM_TAGS = frozenset(
    tag.name
    for tag in (
        system.sys.always_on,
        system.sys.first_scan,
        system.sys.mode_switch_run,
        system.sys.mode_run,
        system.sys.cmd_mode_stop,
        system.sys.cmd_watchdog_reset,
        system.sys.fixed_scan_mode,
        system.sys.battery_present,
        system.sys.scan_time_current_ms,
        system.sys.scan_time_min_ms,
        system.sys.scan_time_max_ms,
        system.sys.scan_time_fixed_setup_ms,
        system.sys.interrupt_scan_time_ms,
        system.sys.dt,
        system.rtc.new_year4,
        system.rtc.new_month,
        system.rtc.new_day,
        system.rtc.new_hour,
        system.rtc.new_minute,
        system.rtc.new_second,
        system.rtc.apply_date,
        system.rtc.apply_date_error,
        system.rtc.apply_time,
        system.rtc.apply_time_error,
        system.fault.plc_error,
        system.fault.division_error,
        system.fault.out_of_range,
        system.fault.address_error,
        system.fault.math_operation_error,
        system.fault.code,
        system.firmware.main_ver_low,
        system.firmware.main_ver_high,
        system.firmware.sub_ver_low,
        system.firmware.sub_ver_high,
        system.storage.sd.eject_cmd,
        system.storage.sd.delete_all_cmd,
        system.storage.sd.ready,
        system.storage.sd.write_status,
        system.storage.sd.error,
        system.storage.sd.error_code,
    )
)

_CLASSIFIED_SYSTEM_TAGS = MOVING_SYSTEM_TAGS | DECLINING_SYSTEM_TAGS | CONSTANT_SYSTEM_TAGS

# --- Tuning -------------------------------------------------------------------

# Most traces a run may build, however long.  A trace costs far more than a scan.
MAX_TRACES_PER_RUN: int = 64
# Passes of the moving-set fixed point before giving up.
MAX_FIXED_POINT_PASSES = 12
# A firing shorter than this did not pay for its attempt, and the next attempt waits,
# twice as long each time in a row.  Measured on small programs, where a real scan is
# cheapest: firing for 2 scans at a time costs a third more than plain stepping, and
# for 4 at a time saves a quarter.
UNPRODUCTIVE_BELOW = 3
MAX_ATTEMPT_WAIT = 4096
# A run shorter than this cannot pay back a trace, and a run earns one more trace for
# each time it is this long.  Measured on the tumbler, a trace costs 15-25 ms, the work
# of some 400 real scans, and each scan the loop runs saves about 50 us.
MIN_RUN_SCANS = 1000

# --- Test modes (tests/fold_check.py and tests/core/test_scan_trace.py) --------

# True: a self check mismatch raises rather than quietly declining.
STRICT_SELF_CHECK = False
# Scans each firing of the loop may run, cycled, so that the states between its
# exits can be compared with stepping.  None: as many as the run allows.
BURST_SIZES: tuple[int, ...] | None = None

# Accumulated over all runs in this process: loop and real scans, traces, reasons.
TOTALS: Counter[str] = Counter()
# Told of each self check mismatch, kept or not: a count cannot say which it was.
SELF_CHECK_OBSERVERS: list[Callable[[str], None]] = []


class Decline(Exception):
    """The tracer met something it does not positively understand."""


class SelfCheckMismatch(AssertionError):
    """A trace's first iteration disagreed with the real scan it stands for."""


class _Missing:
    """A key absent from its dict.  Distinct from every value, ``None`` included."""

    def __repr__(self) -> str:
        return "<missing>"


MISSING: Any = _Missing()
_SCALAR_TYPES = (bool, int, float, str, type(None))


def same(a: Any, b: Any) -> bool:
    """Whether two state values are the same value of the same type.

    ``==`` says ``1 == True == 1.0`` and ``0.0 == -0.0``, and that NaN differs from
    itself.  A stored value keeps its type, and -0.0 divides differently from 0.0.
    """
    if a is b:
        return True
    if type(a) is not type(b):
        return False
    if type(a) is float:
        if a != a:
            return b != b
        return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)
    return a == b


# --- Source -------------------------------------------------------------------


@dataclass(frozen=True)
class _Function:
    node: ast.FunctionDef
    params: tuple[str, ...]
    global_names: frozenset[str]
    local_names: frozenset[str]


# Generated helpers the kernel may call.  ``run_function`` bodies are the user's
# code, compiled in as ``_fn_*``: they are not on this list and so decline.
_HELPER_PREFIXES = (
    "_sub_",
    "_resolve_index_",
    "_read_index_",
    "_window_addrs_",
    "_cond_index_",
)


def _traceable_helper(name: str) -> bool:
    return name in _HELPER_DEFS or name.startswith(_HELPER_PREFIXES)


class KernelSource:
    """A kernel's generated source, parsed once per compiled kernel.

    What the tracer may rely on and nothing more: the functions it may step into,
    and the module-level constants nothing ever rebinds.
    """

    def __init__(self, kernel: CompiledKernel) -> None:
        tree = ast.parse(kernel.source)
        self.functions: dict[str, _Function] = {}
        rebound: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                function = _describe_function(node)
                rebound |= function.global_names
                self.functions[node.name] = function
        self.constants: dict[str, Any] = {}
        for node in tree.body:
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or target.id in rebound:
                continue
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                continue
            if isinstance(value, (*_SCALAR_TYPES, tuple, frozenset)):
                self.constants[target.id] = value
        self.step_functions = tuple(
            self.functions[name]
            for name in ("_kernel_step_pre", "_kernel_step")
            if name in self.functions
        )
        self.scalar_writes, self.opaque_tag_writes = _scalar_writes(tree)
        # A kernel global loaded from a tag at the top of a scan is its working copy.
        self.working_copies: dict[str, str] = {
            statement.targets[0].id: cast("str", statement.value.slice.value)
            for function in self.step_functions
            for statement in function.node.body
            if isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Subscript)
            and isinstance(statement.value.value, ast.Name)
            and statement.value.value.id == "tags"
            and isinstance(statement.value.slice, ast.Constant)
        }


_NOT_LITERAL = object()


def _scalar_writes(tree: ast.Module) -> tuple[dict[str, list[Any]], bool]:
    """Each scalar's writes in the source: the value stored, where it is a literal.

    Also whether something writes ``tags`` other than through a scalar's own local, which
    says nothing about which tag it writes.
    """
    bodies = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    assigned: dict[int, ast.expr] = {}
    local_of: dict[str, str] = {}
    for body in bodies:
        for node in ast.walk(body):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    assigned[id(target)] = node.value
                    tag = _tag_loaded_by(node.value)
                    if isinstance(target, ast.Name) and tag is not None:
                        local_of[target.id] = tag
    writes: dict[str, list[Any]] = {}
    opaque = False
    for body in bodies:
        for node in ast.walk(body):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                tag = local_of.get(node.id)
                value = assigned.get(id(node))
                if tag is not None and (value is None or _tag_loaded_by(value) != tag):
                    writes.setdefault(tag, []).append(_literal_stored(value))
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
                value = assigned.get(id(node))
                if not _is_tags(node.value):
                    continue
                written_back = (
                    isinstance(node.slice, ast.Constant)
                    and isinstance(value, ast.Name)
                    and local_of.get(value.id) == node.slice.value
                )
                opaque = opaque or not written_back
    return writes, opaque


def _is_tags(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "tags"


def _tag_loaded_by(value: ast.expr) -> str | None:
    if (
        isinstance(value, ast.Subscript)
        and _is_tags(value.value)
        and isinstance(value.slice, ast.Constant)
        and isinstance(value.slice.value, str)
    ):
        return value.slice.value
    return None


def _literal_stored(value: ast.expr | None) -> Any:
    if isinstance(value, ast.Constant):
        return value.value
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id.startswith("_store_")
        and len(value.args) == 1
        and isinstance(value.args[0], ast.Constant)
        and type(value.args[0].value) is int
        # Clamped or wrapped alike by every integer type: it stores itself.
        and 0 <= value.args[0].value <= 32767
    ):
        return value.args[0].value
    return _NOT_LITERAL


def may_store_a_default(kernel: CompiledKernel, absent: Mapping[str, Any]) -> bool:
    """Whether a scan can store its default into one of the *absent* tags.

    Stepping adds a tag to the state on its first write.  A kernel scan does not say what
    it wrote, and holds an absent tag at its default, so the first write can be noticed
    only by the value leaving the default.  A write that stores the default cannot.
    """
    if not absent:
        return False
    source = _source_of(kernel)
    if source.opaque_tag_writes:
        return True
    for name, default in absent.items():
        for value in source.scalar_writes.get(name, ()):
            if type(value) is not type(default) or value == default:
                return True
    return False


def _describe_function(node: ast.FunctionDef) -> _Function:
    arguments = node.args
    simple = not (
        arguments.vararg or arguments.kwarg or arguments.kwonlyargs or arguments.posonlyargs
    )
    params = tuple(arg.arg for arg in arguments.args) if simple else ()
    if not simple or arguments.defaults:
        # The call site raises Decline: its argument count cannot match.
        params = (*params, "<not traceable>")
    declared: set[str] = set()
    assigned: set[str] = set(params)
    for child in ast.walk(node):
        if isinstance(child, ast.Global):
            declared.update(child.names)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            assigned.add(child.id)
    return _Function(node, params, frozenset(declared), frozenset(assigned - declared))


# --- Tracer values ------------------------------------------------------------


class _Value:
    """What a kernel expression holds: its concrete value on the traced scan, and
    the loop local computing it when it depends on a moving value (else ``None``).

    ``is_bool`` marks a local known to hold exactly True or False on every
    iteration.  Once a guard pins its truth its value is known, so it stops moving.
    """

    __slots__ = ("concrete", "local", "is_bool")

    def __init__(self, concrete: Any, local: str | None = None, *, is_bool: bool = False) -> None:
        self.concrete = concrete
        self.local = local
        self.is_bool = is_bool


@dataclass(frozen=True)
class _Ref:
    """One of the kernel's state containers, as a value the source passes around."""

    kind: str  # "tags", "memory", "blocks", "block", "prev"
    name: str = ""


class _KernelRaised(Exception):
    """The kernel, run on constants, would raise this here."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(repr(original))
        self.original = original


class _Returned(Exception):
    def __init__(self, value: _Value) -> None:
        super().__init__()
        self.value = value


class _LoopBreak(Exception):
    pass


class _LoopContinue(Exception):
    pass


_BINARY_OPERATORS: dict[type, tuple[str, Callable[[Any, Any], Any]]] = {
    ast.Add: ("+", operator.add),
    ast.Sub: ("-", operator.sub),
    ast.Mult: ("*", operator.mul),
    ast.Div: ("/", operator.truediv),
    ast.FloorDiv: ("//", operator.floordiv),
    ast.Mod: ("%", operator.mod),
    ast.Pow: ("**", operator.pow),
    ast.BitAnd: ("&", operator.and_),
    ast.BitOr: ("|", operator.or_),
    ast.BitXor: ("^", operator.xor),
    ast.LShift: ("<<", operator.lshift),
    ast.RShift: (">>", operator.rshift),
}

# (spelling, function, cannot raise on any scalar)
_COMPARISONS: dict[type, tuple[str, Callable[[Any, Any], Any], bool]] = {
    ast.Eq: ("==", operator.eq, True),
    ast.NotEq: ("!=", operator.ne, True),
    ast.Lt: ("<", operator.lt, False),
    ast.LtE: ("<=", operator.le, False),
    ast.Gt: (">", operator.gt, False),
    ast.GtE: (">=", operator.ge, False),
    ast.Is: ("is", operator.is_, True),
    ast.IsNot: ("is not", operator.is_not, True),
    ast.In: ("in", lambda a, b: a in b, False),
    ast.NotIn: ("not in", lambda a, b: a not in b, False),
}


@dataclass(frozen=True)
class _Pure:
    """A builtin with no side effect and no input but its arguments."""

    spelling: str
    function: Callable[..., Any]
    returns_bool: bool = False
    total: bool = False  # cannot raise on a scalar


_PURE_BUILTINS: dict[str, _Pure] = {
    "bool": _Pure("bool", bool, returns_bool=True, total=True),
    "int": _Pure("int", int),
    "float": _Pure("float", float),
    "abs": _Pure("abs", abs),
    "min": _Pure("min", min),
    "max": _Pure("max", max),
    "round": _Pure("round", round),
    "sorted": _Pure("sorted", sorted),
    "len": _Pure("len", len),
    "ord": _Pure("ord", ord),
    "chr": _Pure("chr", chr),
    "isinstance": _Pure("isinstance", isinstance, returns_bool=True, total=True),
    "type": _Pure("type", type, total=True),
    "frozenset": _Pure("frozenset", frozenset),
}
_PURE_MATH: dict[str, _Pure] = {
    "isfinite": _Pure("math.isfinite", math.isfinite, returns_bool=True),
}
_BUILTIN_VALUES: dict[str, Any] = {
    "int": int,
    "float": float,
    "bool": bool,
    "str": str,
}
_KERNEL_EXCEPTIONS: dict[str, type[BaseException]] = {
    "ArithmeticError": ArithmeticError,
    "IndexError": IndexError,
    "KeyError": KeyError,
    "OverflowError": OverflowError,
    "TypeError": TypeError,
    "ValueError": ValueError,
    "ZeroDivisionError": ZeroDivisionError,
}

# --- Loop operations ----------------------------------------------------------


@dataclass(frozen=True)
class _Op:
    target: str
    expression: str
    uses: tuple[str, ...]
    total: bool  # cannot raise, so an unused one can be dropped


@dataclass(frozen=True)
class _Guard:
    leave_if: str
    uses: tuple[str, ...]


# --- Stop predicates ----------------------------------------------------------

_STOP_COMPARES: dict[type, type] = {
    CompareEq: ast.Eq,
    CompareNe: ast.NotEq,
    CompareLt: ast.Lt,
    CompareLe: ast.LtE,
    CompareGt: ast.Gt,
    CompareGe: ast.GtE,
}


def _guardable(condition: Any) -> bool:
    """Whether a stop condition has a form the tracer can judge.

    Exact types: a subclass may evaluate differently.  A derived system tag has
    no stored value in the state a predicate reads, so it is not one of them.
    """
    kind = type(condition)
    if kind in (AllCondition, AnyCondition):
        return all(_guardable(child) for child in condition.conditions)
    if kind in (BitCondition, NormallyClosedCondition, RisingEdgeCondition, FallingEdgeCondition):
        return _plain_tag(condition._resolved_tag.name)
    if kind is IntTruthyCondition:
        return _plain_tag(condition.tag.name)
    if kind in _STOP_COMPARES:
        if not _plain_tag(condition.tag.name):
            return False
        if condition._value_kind == _VALUE_TAG:
            return _plain_tag(condition.value.name)
        return condition._value_kind == _VALUE_LITERAL and isinstance(
            condition.value, _SCALAR_TYPES
        )
    return False


def _plain_tag(name: str) -> bool:
    return name not in _DERIVED_TAG_NAMES


@dataclass(frozen=True)
class StopForm:
    """A stop predicate as the tracer sees it: a condition, and its edges' memory."""

    condition: Any
    edges: tuple[Any, ...]
    last_seen: Callable[[], Mapping[str, bool]]


def stop_form_of(predicate: Any) -> StopForm | None:
    """The guardable form of a compiled stop predicate, or None."""
    condition = getattr(predicate, "guard_condition", None)
    if condition is None or not _guardable(condition):
        return None
    return StopForm(condition, tuple(predicate.guard_edges), predicate.guard_last_seen)


# --- The tracer ---------------------------------------------------------------


@dataclass
class _Frame:
    function: _Function
    locals: dict[str, _Value] = field(default_factory=dict)


def _count_store(
    stores: dict[tuple[Any, ...], list[int]], key: tuple[Any, ...], value: _Value
) -> None:
    counts = stores.setdefault(key, [0, 0])
    counts[0] += 1
    if value.local is not None:
        counts[1] += 1


class _Tracer:
    """One concolic pass over a scan: the real path, with the moving values marked.

    Interprets the generated source only as far as it recognizes it.
    """

    def __init__(
        self,
        source: KernelSource,
        replay: CompiledPLC,
        seeds: set[tuple[Any, ...]],
    ) -> None:
        self._source = source
        self._replay = replay
        self._kernel = replay._kernel
        self.seeds = seeds
        self.cells: dict[tuple[Any, ...], _Value] = {}
        self.constants: dict[tuple[Any, ...], Any] = {}
        self.entries: dict[tuple[Any, ...], _Value] = {}
        self.entry_locals: dict[tuple[Any, ...], str] = {}
        self.written: set[tuple[Any, ...]] = set()
        self.ops: list[_Op | _Guard] = []
        self.loop_constants: list[Any] = []
        self.not_live: list[tuple[str, str, int]] = []
        self.edge_bits: dict[str, bool] = {}
        # Attempted stores this scan, by cell: how many, and how many of a moving value.
        # What a rung's column says over a stretch depends on it, and nothing else knows.
        self.stores: dict[tuple[Any, ...], list[int]] = {}
        self.working_copy_stores: dict[tuple[Any, ...], list[int]] = {}
        self._globals: dict[str, _Value] = {}
        self._known_truth: dict[str, bool] = {}
        self._own_lists: dict[int, list[Any]] = {}
        self._temp_count = 0
        self._statements: dict[type, Callable[[Any, _Frame], None]] = {
            ast.Assign: self._assign,
            ast.AugAssign: self._augmented_assign,
            ast.If: self._if,
            ast.For: self._for,
            ast.Try: self._try,
            ast.Expr: self._expression_statement,
            ast.Return: self._return,
            ast.Raise: self._raise,
            ast.Pass: self._nothing,
            ast.Global: self._nothing,
            ast.Break: self._break,
            ast.Continue: self._continue,
        }
        self._expressions: dict[type, Callable[[Any, _Frame], _Value]] = {
            ast.Constant: self._constant,
            ast.Name: self._name,
            ast.Subscript: self._subscript,
            ast.Compare: self._compare,
            ast.BinOp: self._binary,
            ast.UnaryOp: self._unary,
            ast.BoolOp: self._bool_op,
            ast.IfExp: self._if_expression,
            ast.List: self._sequence,
            ast.Tuple: self._sequence,
            ast.Call: self._call,
            ast.Attribute: self._attribute,
            ast.ListComp: self._list_comprehension,
        }

    # -- Running ----------------------------------------------------------------

    def run(self, dt: float) -> None:
        state_arguments = (
            _Value(_Ref("tags")),
            _Value(_Ref("blocks")),
            _Value(_Ref("memory")),
            _Value(_Ref("prev")),
            _Value(dt),
        )
        try:
            for function in self._source.step_functions:
                self._enter(function, state_arguments)
        except _KernelRaised as raised:
            raise Decline(f"scan raises {type(raised.original).__name__}") from raised
        for key, cell in self.cells.items():
            if key in self.entries and self._is_moving(key) and cell is not self.entries[key]:
                raise Decline("scan writes a moving tag")
            if cell.local is not None and cell.is_bool and not self._is_moving(key):
                self._truth(cell)

    def _enter(self, function: _Function, arguments: tuple[_Value, ...]) -> _Value:
        if len(arguments) != len(function.params):
            raise Decline(f"call of {function.node.name} with other arguments")
        frame = _Frame(function, dict(zip(function.params, arguments, strict=True)))
        try:
            self._block(function.node.body, frame)
        except _Returned as returned:
            return returned.value
        return _Value(None)

    def _block(self, statements: list[ast.stmt], frame: _Frame) -> None:
        for statement in statements:
            handler = self._statements.get(type(statement))
            if handler is None:
                raise Decline(f"statement {type(statement).__name__}")
            handler(statement, frame)

    def _eval(self, node: ast.expr, frame: _Frame) -> _Value:
        handler = self._expressions.get(type(node))
        if handler is None:
            raise Decline(f"expression {type(node).__name__}")
        return handler(node, frame)

    # -- State ------------------------------------------------------------------

    def _current(self, key: tuple[Any, ...]) -> Any:
        return _current_value(self._kernel, key)

    def release(self) -> None:
        """Keep only what writing another loop needs.  A coast keeps many traces."""
        self.cells = {key: self.cells[key] for key in self.entries}
        self.constants = {}
        self.seeds = set()
        self.written = set()
        self.not_live = []
        self.edge_bits = {}
        self._globals = {}
        self._known_truth = {}
        self._own_lists = {}
        self._statements = {}
        self._expressions = {}
        self._source = cast("Any", None)
        self._replay = cast("Any", None)
        self._kernel = cast("Any", None)

    def _is_moving(self, key: tuple[Any, ...]) -> bool:
        return key[0] == "tags" and key[1] in MOVING_SYSTEM_TAGS

    def read(self, key: tuple[Any, ...]) -> _Value:
        cell = self.cells.get(key)
        if cell is not None:
            return cell
        if key[0] == "tags" and key[1] in SYSTEM_TAGS_BY_NAME:
            # The run already declined these; this covers a read the list missed.
            if key[1] in DECLINING_SYSTEM_TAGS or key[1] not in _CLASSIFIED_SYSTEM_TAGS:
                raise Decline(f"scan reads {key[1]}")
        current = self._current(key)
        if current is not MISSING and not isinstance(current, _SCALAR_TYPES):
            raise Decline("state holds a value that is not a scalar")
        if key in self.seeds and current is not MISSING:
            local = f"L{len(self.entries)}"
            cell = _Value(current, local, is_bool=self._is_moving_clock(key))
            self.entries[key] = cell
            self.entry_locals[key] = local
        else:
            self.constants[key] = current
            cell = _Value(current)
        self.cells[key] = cell
        return cell

    def _is_moving_clock(self, key: tuple[Any, ...]) -> bool:
        return key[0] == "tags" and key[1] in _CLOCK_HALF_PERIODS

    def write(self, key: tuple[Any, ...], value: _Value) -> None:
        if not isinstance(value.concrete, _SCALAR_TYPES):
            raise Decline("scan stores a value that is not a scalar")
        if key in self.seeds:
            self.read(key)
            if key not in self.entries:
                # A cell that moved once and this state lacks reads as a constant absence,
                # and the scan adding it repeats nothing.
                raise Decline("scan adds a moving cell")
        else:
            self.constants.setdefault(key, self._current(key))
        self.cells[key] = value
        self.written.add(key)
        _count_store(self.stores, key, value)

    # -- Loop operations --------------------------------------------------------

    def _atom(self, value: _Value) -> str:
        """Spell a value in the loop: its local, a literal, or a bound constant."""
        if value.local is not None:
            return value.local
        concrete = value.concrete
        kind = type(concrete)
        if concrete is None or kind is bool:
            return repr(concrete)
        if kind is int or (kind is float and math.isfinite(concrete)):
            # Parenthesized: -1 ** x is not (-1) ** x.
            return f"({concrete!r})"
        if kind is str:
            return repr(concrete)
        if isinstance(concrete, _Ref):
            raise Decline("state container in a loop expression")
        if isinstance(concrete, (list, tuple)):
            concrete = _plain(value)  # declines if a moving value is inside
        self.loop_constants.append(concrete)
        return f"K{len(self.loop_constants) - 1}"

    def _emit(
        self,
        expression: str,
        concrete: Any,
        operands: tuple[_Value, ...],
        *,
        total: bool = False,
        is_bool: bool = False,
    ) -> _Value:
        self._temp_count += 1
        target = f"t{self._temp_count}"
        uses = tuple(v.local for v in operands if v.local is not None)
        self.ops.append(_Op(target, expression, uses, total))
        return _Value(concrete, target, is_bool=is_bool)

    def _guard(self, leave_if: str, operands: tuple[_Value, ...]) -> None:
        uses = tuple(v.local for v in operands if v.local is not None)
        self.ops.append(_Guard(leave_if, uses))

    def _truth(self, value: _Value) -> bool:
        """Decide on *value*: its truth now, and a guard to keep it."""
        local = value.local
        truth = bool(value.concrete)
        if local is None:
            return truth
        if local not in self._known_truth:
            self._guard(f"not {local}" if truth else local, (value,))
            self._known_truth[local] = truth
        if value.is_bool:
            value.concrete = truth
            value.local = None
        return truth

    def _pinned(self, value: _Value) -> Any:
        """A value a container access needs concretely, with a guard that it stays."""
        if value.local is None:
            return value.concrete
        concrete = value.concrete
        if not isinstance(concrete, _SCALAR_TYPES):
            raise Decline("moving value is not a scalar")
        constant = _Value(type(concrete))
        self._guard(
            f"type({value.local}) is not {self._atom(constant)} "
            f"or {value.local} != {self._atom(_Value(concrete))}",
            (value,),
        )
        return concrete

    def _pure(self, pure: _Pure, arguments: list[_Value]) -> _Value:
        plain = [_plain(argument) for argument in arguments]
        moving = any(argument.local is not None for argument in arguments)
        try:
            result = pure.function(*plain)
        except Exception as exc:
            if moving:
                raise Decline("a moving value raised") from exc
            raise _KernelRaised(exc) from exc
        if not moving:
            return _Value(result)
        if pure.spelling == "bool" and arguments[0].local in self._known_truth:
            return _Value(result)
        spelled = ", ".join(self._atom(argument) for argument in arguments)
        return self._emit(
            f"{pure.spelling}({spelled})",
            result,
            tuple(arguments),
            total=pure.total,
            is_bool=pure.returns_bool,
        )

    # -- Statements -------------------------------------------------------------

    def _nothing(self, _statement: ast.stmt, _frame: _Frame) -> None:
        return None

    def _assign(self, statement: ast.Assign, frame: _Frame) -> None:
        value = self._eval(statement.value, frame)
        for target in statement.targets:
            self._store(target, value, frame)

    def _augmented_assign(self, statement: ast.AugAssign, frame: _Frame) -> None:
        current = self._eval(_as_load(statement.target), frame)
        operand = self._eval(statement.value, frame)
        self._store(statement.target, self._apply_binary(current, statement.op, operand), frame)

    def _store(self, target: ast.expr, value: _Value, frame: _Frame) -> None:
        if isinstance(target, ast.Name):
            if target.id in frame.function.global_names:
                self._globals[target.id] = value
                tag = self._source.working_copies.get(target.id)
                # The load at the top of the scan is the cell itself, not an attempt.
                if tag is not None and value is not self.cells.get(("tags", tag)):
                    _count_store(self.working_copy_stores, ("tags", tag), value)
            else:
                frame.locals[target.id] = value
        elif isinstance(target, ast.Subscript):
            self._store_subscript(target, value, frame)
        elif isinstance(target, (ast.Tuple, ast.List)):
            items = value.concrete
            if value.local is not None or not isinstance(items, (list, tuple)):
                raise Decline("unpacking a value that is not a known sequence")
            if len(items) != len(target.elts):
                raise Decline("unpacking a sequence of another length")
            for element, item in zip(target.elts, items, strict=True):
                self._store(element, _wrapped(item), frame)
        else:
            raise Decline(f"assignment to {type(target).__name__}")

    def _store_subscript(self, target: ast.Subscript, value: _Value, frame: _Frame) -> None:
        if isinstance(target.slice, ast.Slice):
            raise Decline("slice store")
        container = self._eval(target.value, frame)
        index = self._pinned(self._eval(target.slice, frame))
        reference = container.concrete
        if isinstance(reference, list) and id(reference) in self._own_lists:
            # A list this scan built, so nothing outside the scan can see it.
            try:
                reference[index] = value
            except (IndexError, TypeError) as exc:
                raise _KernelRaised(exc) from exc
            return
        if not isinstance(reference, _Ref):
            raise Decline("store into a local container")
        self.write(self._cell_key(reference, index), value)

    def _cell_key(self, reference: _Ref, index: Any) -> tuple[Any, ...]:
        if reference.kind == "tags":
            return ("tags", index)
        if reference.kind == "memory":
            return ("memory", index)
        if reference.kind == "block":
            if type(index) is not int or index < 0:
                raise Decline("block index that is not a plain offset")
            if index >= len(self._kernel.blocks[reference.name]):
                raise _KernelRaised(IndexError(index))
            return ("block", reference.name, index)
        raise Decline(f"access to {reference.kind}")

    def _if(self, statement: ast.If, frame: _Frame) -> None:
        taken = self._truth(self._eval(statement.test, frame))
        self._block(statement.body if taken else statement.orelse, frame)

    def _for(self, statement: ast.For, frame: _Frame) -> None:
        if statement.orelse:
            raise Decline("for-else")
        iterable = self._eval(statement.iter, frame)
        if iterable.local is not None:
            raise Decline("moving iterable")
        if not isinstance(iterable.concrete, (list, tuple, range)):
            raise Decline("iteration over an unknown value")
        for item in iterable.concrete:
            self._store(statement.target, _wrapped(item), frame)
            try:
                self._block(statement.body, frame)
            except _LoopBreak:
                break
            except _LoopContinue:
                continue

    def _try(self, statement: ast.Try, frame: _Frame) -> None:
        if statement.finalbody:
            raise Decline("try-finally")
        try:
            self._block(statement.body, frame)
        except _KernelRaised as raised:
            for handler in statement.handlers:
                if handler.name is not None:
                    raise Decline("named exception handler") from raised
                if isinstance(raised.original, _exception_classes(handler.type)):
                    self._block(handler.body, frame)
                    return
            raise
        self._block(statement.orelse, frame)

    def _expression_statement(self, statement: ast.Expr, frame: _Frame) -> None:
        self._eval(statement.value, frame)

    def _return(self, statement: ast.Return, frame: _Frame) -> None:
        value = _Value(None) if statement.value is None else self._eval(statement.value, frame)
        raise _Returned(value)

    def _raise(self, statement: ast.Raise, _frame: _Frame) -> None:
        exception = statement.exc
        if isinstance(exception, ast.Call):
            exception = exception.func
        if not isinstance(exception, ast.Name) or exception.id not in _KERNEL_EXCEPTIONS:
            raise Decline("raise of an unknown exception")
        # The message is not evaluated: nothing reads it, and the kernel's raise
        # reaches a handler or ends the trace before any later statement.
        raise _KernelRaised(_KERNEL_EXCEPTIONS[exception.id]("kernel"))

    def _break(self, _statement: ast.Break, _frame: _Frame) -> None:
        raise _LoopBreak

    def _continue(self, _statement: ast.Continue, _frame: _Frame) -> None:
        raise _LoopContinue

    # -- Expressions ------------------------------------------------------------

    def _constant(self, node: ast.Constant, _frame: _Frame) -> _Value:
        return _Value(node.value)

    def _name(self, node: ast.Name, frame: _Frame) -> _Value:
        name = node.id
        function = frame.function
        if name in function.local_names and name not in function.global_names:
            value = frame.locals.get(name)
            if value is None:
                raise Decline(f"local {name} read before assignment")
            return value
        value = self._globals.get(name)
        if value is not None:
            return value
        if name in self._source.constants:
            return _Value(self._source.constants[name])
        if name in _BUILTIN_VALUES and name not in self._source.functions:
            return _Value(_BUILTIN_VALUES[name])
        raise Decline(f"name {name}")

    def _subscript(self, node: ast.Subscript, frame: _Frame) -> _Value:
        if isinstance(node.slice, ast.Slice):
            raise Decline("slice read")
        container = self._eval(node.value, frame)
        index = self._pinned(self._eval(node.slice, frame))
        reference = container.concrete
        if isinstance(reference, _Ref):
            if reference.kind == "blocks":
                return _Value(_Ref("block", index))
            value = self.read(self._cell_key(reference, index))
            if value.concrete is MISSING:
                raise _KernelRaised(KeyError(index))
            return value
        if container.local is not None:
            raise Decline("subscript of a moving value")
        if not isinstance(reference, (list, tuple, range, str)):
            raise Decline("subscript of an unknown value")
        try:
            return _wrapped(reference[index])
        except (IndexError, TypeError) as exc:
            raise _KernelRaised(exc) from exc

    def _compare(self, node: ast.Compare, frame: _Frame) -> _Value:
        left = self._eval(node.left, frame)
        last = len(node.ops) - 1
        for position, (comparison, comparator) in enumerate(
            zip(node.ops, node.comparators, strict=True)
        ):
            right = self._eval(comparator, frame)
            result = self._apply_compare(left, type(comparison), right)
            if position == last:
                return result
            # A chain stops at its first false link, without evaluating the rest.
            if not self._truth(result):
                return result
            left = right
        raise Decline("comparison without operators")

    def _apply_compare(self, left: _Value, kind: type, right: _Value) -> _Value:
        spec = _COMPARISONS.get(kind)
        if spec is None:
            raise Decline(f"comparison {kind.__name__}")
        spelling, function, total = spec
        try:
            concrete = function(_plain(left), _plain(right))
        except Exception as exc:
            if left.local is not None or right.local is not None:
                raise Decline("a moving value raised") from exc
            raise _KernelRaised(exc) from exc
        if left.local is None and right.local is None:
            return _Value(concrete)
        return self._emit(
            f"{self._atom(left)} {spelling} {self._atom(right)}",
            concrete,
            (left, right),
            total=total,
            is_bool=True,
        )

    def _binary(self, node: ast.BinOp, frame: _Frame) -> _Value:
        left = self._eval(node.left, frame)
        right = self._eval(node.right, frame)
        return self._apply_binary(left, node.op, right)

    def _apply_binary(self, left: _Value, op: ast.operator, right: _Value) -> _Value:
        spec = _BINARY_OPERATORS.get(type(op))
        if spec is None:
            raise Decline(f"operator {type(op).__name__}")
        spelling, function = spec
        if not isinstance(left.concrete, _SCALAR_TYPES) or not isinstance(
            right.concrete, _SCALAR_TYPES
        ):
            raise Decline("arithmetic on a container")
        try:
            concrete = function(left.concrete, right.concrete)
        except Exception as exc:
            if left.local is not None or right.local is not None:
                raise Decline("a moving value raised") from exc
            raise _KernelRaised(exc) from exc
        if left.local is None and right.local is None:
            return _Value(concrete)
        return self._emit(
            f"{self._atom(left)} {spelling} {self._atom(right)}", concrete, (left, right)
        )

    def _unary(self, node: ast.UnaryOp, frame: _Frame) -> _Value:
        operand = self._eval(node.operand, frame)
        if not isinstance(operand.concrete, _SCALAR_TYPES):
            raise Decline("unary operator on a container")
        if isinstance(node.op, ast.Not):
            if operand.local is None:
                return _Value(not operand.concrete)
            if operand.local in self._known_truth:
                return _Value(not self._known_truth[operand.local])
            return self._emit(
                f"not {operand.local}",
                not operand.concrete,
                (operand,),
                total=True,
                is_bool=True,
            )
        if isinstance(node.op, ast.USub):
            spelling, function = "-", operator.neg
        elif isinstance(node.op, ast.UAdd):
            spelling, function = "+", operator.pos
        elif isinstance(node.op, ast.Invert):
            spelling, function = "~", operator.invert
        else:
            raise Decline(f"operator {type(node.op).__name__}")
        try:
            concrete = function(operand.concrete)
        except Exception as exc:
            if operand.local is not None:
                raise Decline("a moving value raised") from exc
            raise _KernelRaised(exc) from exc
        if operand.local is None:
            return _Value(concrete)
        return self._emit(f"{spelling}{operand.local}", concrete, (operand,))

    def _bool_op(self, node: ast.BoolOp, frame: _Frame) -> _Value:
        stop_on = not isinstance(node.op, ast.And)
        value = _Value(None)
        for item in node.values:
            value = self._eval(item, frame)
            if self._truth(value) == stop_on:
                return value
        return value

    def _if_expression(self, node: ast.IfExp, frame: _Frame) -> _Value:
        taken = self._truth(self._eval(node.test, frame))
        return self._eval(node.body if taken else node.orelse, frame)

    def _sequence(self, node: ast.List | ast.Tuple, frame: _Frame) -> _Value:
        items = [self._eval(item, frame) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return _Value(tuple(items))
        self._own_lists[id(items)] = items
        return _Value(items)

    def _attribute(self, node: ast.Attribute, _frame: _Frame) -> _Value:
        raise Decline(f"attribute {node.attr}")

    def _list_comprehension(self, node: ast.ListComp, frame: _Frame) -> _Value:
        if len(node.generators) != 1:
            raise Decline("nested comprehension")
        generator = node.generators[0]
        if generator.ifs or generator.is_async:
            raise Decline("filtered comprehension")
        iterable = self._eval(generator.iter, frame)
        if iterable.local is not None or not isinstance(iterable.concrete, (list, tuple, range)):
            raise Decline("comprehension over an unknown value")
        # A comprehension's target has a scope of its own: it must not outlive the
        # comprehension or disturb a local, or a global, of the same name.
        names = {
            child.id
            for child in ast.walk(generator.target)
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
        }
        if names & frame.function.global_names:
            raise Decline("comprehension target named as a global")
        outer = {name: frame.locals[name] for name in names if name in frame.locals}
        results = []
        for item in iterable.concrete:
            self._store(generator.target, _wrapped(item), frame)
            results.append(self._eval(node.elt, frame))
        for name in names:
            frame.locals.pop(name, None)
        frame.locals.update(outer)
        self._own_lists[id(results)] = results
        return _Value(results)

    def _call(self, node: ast.Call, frame: _Frame) -> _Value:
        if node.keywords or any(isinstance(arg, ast.Starred) for arg in node.args):
            raise Decline("call with keyword or starred arguments")
        callee = node.func
        if isinstance(callee, ast.Attribute):
            return self._method_call(callee, node, frame)
        if isinstance(callee, ast.Lambda):
            return self._lambda_call(callee, node, frame)
        if not isinstance(callee, ast.Name):
            raise Decline("call of an expression")
        name = callee.id
        function = frame.function
        if name in function.local_names or name in function.global_names:
            raise Decline(f"call of variable {name}")
        arguments = [self._eval(argument, frame) for argument in node.args]
        target = self._source.functions.get(name)
        if target is not None:
            if not _traceable_helper(name):
                raise Decline(f"call {name}")
            return self._enter(target, tuple(arguments))
        if name in ("range", "zip", "enumerate"):
            return self._iterator(name, arguments)
        pure = _PURE_BUILTINS.get(name)
        if pure is None:
            raise Decline(f"call {name}")
        return self._pure(pure, arguments)

    def _lambda_call(self, callee: ast.Lambda, node: ast.Call, frame: _Frame) -> _Value:
        """A lambda called where it is written, as a median of an even count is generated.

        Its body is one expression and nothing in an expression the tracer follows assigns,
        so the frame it sees is the caller's with the parameters bound.
        """
        signature = callee.args
        if (
            signature.posonlyargs
            or signature.kwonlyargs
            or signature.vararg
            or signature.kwarg
            or signature.defaults
            or len(signature.args) != len(node.args)
        ):
            raise Decline("lambda with more than plain parameters")
        params = [parameter.arg for parameter in signature.args]
        function = frame.function
        if function.global_names.intersection(params):
            raise Decline("lambda parameter named as a global")
        arguments = [self._eval(argument, frame) for argument in node.args]
        inner = _Frame(
            replace(function, local_names=function.local_names.union(params)),
            {**frame.locals, **dict(zip(params, arguments, strict=True))},
        )
        return self._eval(callee.body, inner)

    def _iterator(self, name: str, arguments: list[_Value]) -> _Value:
        if any(argument.local is not None for argument in arguments):
            raise Decline("moving iterable")
        if name == "range":
            return _Value(range(*[_plain(argument) for argument in arguments]))
        for argument in arguments:
            if not isinstance(argument.concrete, (list, tuple, range)):
                raise Decline(f"{name} over an unknown value")
        columns = [argument.concrete for argument in arguments]
        if name == "enumerate":
            return _Value([(_Value(i), _wrapped(item)) for i, item in enumerate(*columns)])
        return _Value(
            [tuple(_wrapped(item) for item in row) for row in zip(*columns, strict=False)]
        )

    def _method_call(self, callee: ast.Attribute, node: ast.Call, frame: _Frame) -> _Value:
        if isinstance(callee.value, ast.Name) and callee.value.id == "math":
            pure = _PURE_MATH.get(callee.attr)
            if pure is None:
                raise Decline(f"call math.{callee.attr}")
            return self._pure(pure, [self._eval(argument, frame) for argument in node.args])
        owner = self._eval(callee.value, frame)
        arguments = [self._eval(argument, frame) for argument in node.args]
        reference = owner.concrete
        if (
            isinstance(reference, _Ref)
            and reference.kind == "memory"
            and callee.attr == "get"
            and 1 <= len(arguments) <= 2
        ):
            value = self.read(("memory", self._pinned(arguments[0])))
            if value.concrete is MISSING:
                return arguments[1] if len(arguments) == 2 else _Value(None)
            return value
        if isinstance(reference, list) and callee.attr == "append" and len(arguments) == 1:
            reference.append(arguments[0])
            return _Value(None)
        raise Decline(f"method {callee.attr}")

    # -- End of the scan: what the run asks of it --------------------------------

    def judge_stop(self, form: StopForm) -> None:
        """Guard the stop predicate false on the state this scan leaves.

        A predicate judges the committed state: a tag from the state, and a block
        cell only once a scan has written it, else the tag's default.  True here
        declines: the real scan runs, and the real predicate decides.

        The edge memory holds still.  Each edge's bit stays as last seen, so no
        edge fires, and the bits the predicate would record are the ones it has.
        """
        last_seen = dict(form.last_seen())
        for edge in form.edges:
            key = edge._memory_key
            bit = self._stop_tag_truth(edge._resolved_tag.name, False)
            if bit != last_seen.get(key):
                raise Decline("stop predicate edge moves")
        self.edge_bits = last_seen
        if self._condition_truth(form.condition):
            raise Decline("stop predicate holds")

    def _condition_truth(self, condition: Any) -> bool:
        kind = type(condition)
        if kind is AllCondition:
            return all(self._condition_truth(child) for child in condition.conditions)
        if kind is AnyCondition:
            return any(self._condition_truth(child) for child in condition.conditions)
        if kind is BitCondition:
            return self._stop_tag_truth(condition._resolved_tag.name, False)
        if kind is NormallyClosedCondition:
            return not self._stop_tag_truth(condition._resolved_tag.name, False)
        if kind in (RisingEdgeCondition, FallingEdgeCondition):
            return False  # see judge_stop
        if kind is IntTruthyCondition:
            value = self._stop_tag(condition.tag.name, condition.tag.default)
            as_int = self._pure(_PURE_BUILTINS["int"], [value])
            return self._truth(self._apply_compare(as_int, ast.NotEq, _Value(0)))
        if kind in _STOP_COMPARES:
            left = self._stop_tag(condition.tag.name, condition.tag.default)
            other = condition.value
            right = (
                self._stop_tag(other.name, other.default)
                if condition._value_kind == _VALUE_TAG
                else _Value(other)
            )
            return self._truth(self._apply_compare(left, _STOP_COMPARES[kind], right))
        raise Decline(f"stop condition {kind.__name__}")

    def _stop_tag_truth(self, name: str, default: Any) -> bool:
        return self._truth(self._pure(_PURE_BUILTINS["bool"], [self._stop_tag(name, default)]))

    def _stop_tag(self, name: str, default: Any) -> _Value:
        position = self._replay._block_pos.get(name)
        if position is None:
            value = self.read(("tags", name))
            if value.concrete is not MISSING:
                return value
            if name in SYSTEM_TAGS_BY_NAME:
                raise Decline("stop predicate reads an unbound system tag")
            return _Value(default)
        symbol, index = position
        key = ("block", symbol, index)
        array = self._kernel.blocks[symbol]
        live = (
            key in self.written
            or name in self._replay._live_block_tags
            or index in getattr(array, "written_indices", ())
        )
        if live:
            return self.read(key)
        self.not_live.append((name, symbol, index))
        return _Value(default)

    def judge_bounds(self, constrained: Mapping[str, TagConstraint]) -> None:
        """Guard each moving constrained tag inside its bounds.

        The guard passes only for a value certainly in bounds; anything else
        leaves, and the real scan decides, watched as a real scan is.  A constant
        one holds a value the run's watch has already seen in bounds.
        """
        for name, constraint in constrained.items():
            position = self._replay._block_pos.get(name)
            key = ("tags", name) if position is None else ("block", position[0], position[1])
            cell = self.cells.get(key)
            if cell is None or key not in self.seeds or cell is self.entries.get(key):
                continue
            if cell.local is None:
                if _check_one(name, cell.concrete, constraint) is not None:
                    raise Decline("scan leaves a value out of bounds")
                continue
            atom = cell.local
            low, high = constraint.min, constraint.max
            if low is not None:
                self._guard(f"not {self._atom(_Value(low))} <= {atom}", (cell,))
            if high is not None:
                self._guard(f"not {atom} <= {self._atom(_Value(high))}", (cell,))
            if constraint.choices_keys is not None:
                choices = self._atom(_Value(constraint.choices_keys))
                self._guard(f"{atom} not in {choices}", (cell,))


def _as_load(target: ast.expr) -> ast.expr:
    if isinstance(target, ast.Name):
        return ast.Name(target.id, ast.Load())
    if isinstance(target, ast.Subscript):
        return ast.Subscript(target.value, target.slice, ast.Load())
    raise Decline(f"augmented assignment to {type(target).__name__}")


def _wrapped(item: Any) -> _Value:
    return item if isinstance(item, _Value) else _Value(item)


def _plain(value: _Value) -> Any:
    """The concrete value of an argument.  A container holding a moving value
    cannot be spelled as one concrete operand of the loop, so it declines."""
    concrete = value.concrete
    if isinstance(concrete, _Ref):
        raise Decline("state container as an argument")
    if isinstance(concrete, (list, tuple)):
        items = [_plain(_wrapped(item)) for item in concrete]
        if any(isinstance(item, _Value) and item.local is not None for item in concrete):
            raise Decline("container holding a moving value")
        return items if isinstance(concrete, list) else tuple(items)
    return concrete


def _exception_classes(node: ast.expr | None) -> tuple[type[BaseException], ...]:
    if node is None:
        raise Decline("bare except")
    names = node.elts if isinstance(node, ast.Tuple) else [node]
    classes = []
    for name in names:
        if not isinstance(name, ast.Name) or name.id not in _KERNEL_EXCEPTIONS:
            raise Decline("handler of an unknown exception")
        classes.append(_KERNEL_EXCEPTIONS[name.id])
    return tuple(classes)


def _current_value(kernel: Any, key: tuple[Any, ...]) -> Any:
    domain = key[0]
    if domain == "tags":
        return kernel.tags.get(key[1], MISSING)
    if domain == "memory":
        return kernel.memory.get(key[1], MISSING)
    return kernel.blocks[key[1]][key[2]]


# --- A trace and its loop -----------------------------------------------------


@dataclass(eq=False)
class ScanTrace:
    """The result of tracing one scan: when it applies, and the loop that repeats it."""

    constants: tuple[tuple[tuple[Any, ...], Any], ...]
    loop: Callable[..., tuple[int, float]]
    block_writes: tuple[tuple[str, frozenset[int]], ...]
    not_live: tuple[tuple[str, str, int], ...]
    edge_bits: dict[str, bool]
    seeds: frozenset[tuple[Any, ...]]
    source: str
    op_count: int
    guard_count: int
    # The finished tracer, released, for a coast to write its own loops from.
    plan: _Tracer | None = None

    def holds(self, replay: CompiledPLC, form: StopForm | None) -> bool:
        """Whether every constant of this trace is as it was when it was traced."""
        kernel = replay._kernel
        for key, expected in self.constants:
            if not same(_current_value(kernel, key), expected):
                return False
        for name, symbol, index in self.not_live:
            if name in replay._live_block_tags:
                return False
            if index in getattr(kernel.blocks[symbol], "written_indices", ()):
                return False
        return form is None or dict(form.last_seen()) == self.edge_bits

    def changed_constants(self, replay: CompiledPLC) -> set[tuple[Any, ...]]:
        kernel = replay._kernel
        return {
            key
            for key, expected in self.constants
            if not same(_current_value(kernel, key), expected)
        }


def _render_loop(tracer: _Tracer) -> ScanTrace:
    """The trace a finished tracer describes, with its plain loop."""
    loop, source, body = _loop_function(tracer)
    block_writes: dict[str, set[int]] = {}
    for key in tracer.written:
        if key[0] == "block":
            block_writes.setdefault(key[1], set()).add(key[2])
    return ScanTrace(
        constants=tuple(tracer.constants.items()),
        loop=loop,
        block_writes=tuple((symbol, frozenset(cells)) for symbol, cells in block_writes.items()),
        not_live=tuple(tracer.not_live),
        edge_bits=tracer.edge_bits,
        seeds=frozenset(tracer.seeds),
        source=source,
        op_count=sum(1 for op in body if isinstance(op, _Op)),
        guard_count=sum(1 for op in body if isinstance(op, _Guard)),
    )


def _loop_function(
    tracer: _Tracer,
    *,
    guards: tuple[_Guard, ...] = (),
    seen: tuple[tuple[str, tuple[Any, ...]], ...] = (),
) -> tuple[Callable[..., tuple[int, float]], str, list[_Op | _Guard]]:
    """Write the loop a finished tracer describes.

    An iteration computes into temporaries and assigns the moving locals last, in
    one statement, so a guard or an exception leaves them at the last whole scan.
    Only what changed goes back to the kernel, and only if a scan ran.

    *guards* are extra, checked after the scan's own and before its commit.  *seen*
    lists (name, cell): after each commit those values go in the loop's ``seen``
    argument and it asks its ``stop`` argument whether to leave.
    """
    arrays: dict[str, str] = {}
    loads: list[str] = []
    refresh: list[str] = []
    commit_targets: list[str] = []
    commit_values: list[str] = []
    live: set[str] = set()
    stores: list[str] = []
    needs_rtc = False
    for key, entry in tracer.entries.items():
        local = tracer.entry_locals[key]
        if key[0] == "tags" and key[1] in _CLOCK_HALF_PERIODS:
            refresh.append(f"{local} = clock_high(ts, {_CLOCK_HALF_PERIODS[key[1]]!r})")
            continue
        if key[0] == "tags" and key[1] in RTC_FIELD_TAG_NAMES:
            refresh.append(f"{local} = rtc_now[{key[1]!r}]")
            needs_rtc = True
            continue
        if key[0] == "block":
            array = arrays.setdefault(key[1], f"B{len(arrays)}")
            place = f"{array}[{key[2]!r}]"
        else:
            place = f"{key[0]}[{key[1]!r}]"
        loads.append(f"{local} = {place}")
        cell = tracer.cells[key]
        if cell is not entry:
            commit_targets.append(local)
            commit_values.append(tracer._atom(cell))
            stores.append(f"{place} = {local}")
            if cell.local is not None:
                live.add(cell.local)

    body = _live_operations([*tracer.ops, *guards], live)
    lines = [
        "def _scan_loop(tags, blocks, memory, ts, dt, limit, rtc_fields, seen=None, stop=None):",
        "    used = 0",
    ]
    if needs_rtc:
        lines += ["    rtc_from = math.inf", "    rtc_until = -math.inf"]
    lines.append("    try:")
    # In the try: a state that lost a key since the trace is a scan for the kernel.
    lines += [f"        {array} = blocks[{symbol!r}]" for symbol, array in arrays.items()]
    lines += [f"        {load}" for load in loads]
    lines.append("        while used < limit:")
    if needs_rtc:
        lines += [
            "            if not rtc_from <= ts < rtc_until:",
            "                rtc_now, rtc_from, rtc_until = rtc_fields(ts)",
        ]
    lines += [f"            {line}" for line in refresh]
    for op in body:
        if isinstance(op, _Guard):
            lines.append(f"            if {op.leave_if}: break")
        else:
            lines.append(f"            {op.target} = {op.expression}")
    if commit_targets:
        lines.append(f"            {', '.join(commit_targets)}, = {', '.join(commit_values)},")
    lines += [
        "            used += 1",
        "            ts += dt",
    ]
    if seen:
        lines += [f"            seen[{name!r}] = {tracer.entry_locals[key]}" for name, key in seen]
        lines.append("            if stop(): break")
    lines += [
        "    except Exception:",
        "        pass",  # As a failed guard: the real scan runs this iteration.
    ]
    if stores:
        lines.append("    if used:")
        lines += [f"        {store}" for store in stores]
    lines.append("    return used, ts")
    source = "\n".join(lines)
    namespace: dict[str, Any] = {"clock_high": clock_high, "math": math}
    namespace.update({f"K{i}": value for i, value in enumerate(tracer.loop_constants)})
    exec(compile(source, "<scan loop>", "exec"), namespace)  # noqa: S102
    return namespace["_scan_loop"], source, body


def _live_operations(ops: list[_Op | _Guard], live: set[str]) -> list[_Op | _Guard]:
    """The operations an iteration has to run, in order.

    An unused operation goes only if it cannot raise: a raise is how the loop
    learns that the kernel would have left the traced path.
    """
    kept: list[_Op | _Guard] = []
    for op in reversed(ops):
        if isinstance(op, _Guard):
            live.update(op.uses)
            kept.append(op)
        elif op.target in live or not op.total:
            live.update(op.uses)
            kept.append(op)
    kept.reverse()
    return kept


_HOOK_KEYS = frozenset(
    {("tags", name) for name in HOOK_TAG_NAMES} | {("memory", key) for key in HOOK_MEMORY_KEYS}
)


def build_trace(
    source: KernelSource,
    replay: CompiledPLC,
    seeds: set[tuple[Any, ...]],
    form: StopForm | None,
    constrained: Mapping[str, TagConstraint],
    *,
    keep_plan: bool = False,
) -> ScanTrace:
    """Trace the scan about to run, to the fixed point of what moves.

    Raises :class:`Decline`.  Grows *seeds*: what moved once may well move again,
    and the next trace should know.  *keep_plan* lets a coast write its own loops
    of this scan (:func:`coast_loop`).
    """
    for _pass in range(MAX_FIXED_POINT_PASSES):
        tracer = _Tracer(source, replay, seeds)
        tracer.run(replay._dt)
        moved = {
            key
            for key, cell in tracer.cells.items()
            if key not in seeds
            and (cell.local is not None or not same(tracer.constants[key], cell.concrete))
        }
        if not moved:
            if form is not None:
                tracer.judge_stop(form)
            tracer.judge_bounds(constrained)
            trace = _render_loop(tracer)
            if keep_plan:
                tracer.release()
                trace.plan = tracer
            return trace
        if moved & _HOOK_KEYS:
            raise Decline("scan changes what the scan hooks read")
        seeds |= moved
    raise Decline("no fixed point")


# --- A coast's loop of a trace --------------------------------------------------


@dataclass(frozen=True)
class CoastMoves:
    """What the scans of a coast loop do to the tags, by name.

    In a coast loop a tag changes only by a checked constant integer delta, by an
    exact flip the path itself makes, or as a float ramp.  So a tag's values over
    a stretch can be told from where it ended.
    """

    moved: tuple[tuple[str, int], ...]  # (name, delta each scan)
    free: tuple[str, ...]  # float ramps
    flips: tuple[tuple[str, Any, Any], ...]  # (name, from, to): a loop of these runs one scan
    held: tuple[str, ...]  # computed from a moving value, and keeping theirs


def _tag_of_cell(key: tuple[Any, ...], block_names: Mapping[str, Mapping[int, str]]) -> str | None:
    if key[0] == "tags":
        return key[1]
    if key[0] == "block":
        return block_names.get(key[1], {}).get(key[2])
    return None


def moving_tags(
    plan: _Tracer, block_names: Mapping[str, Mapping[int, str]]
) -> dict[tuple[Any, ...], str]:
    """The tags a trace's scans may change, by cell.  Memory is not state a reader sees."""
    named = {}
    for key, entry in plan.entries.items():
        name = _tag_of_cell(key, block_names)
        if name is not None and plan.cells[key] is not entry:
            named[key] = name
    return named


def attempted_stores(
    plan: _Tracer, block_names: Mapping[str, Mapping[int, str]]
) -> dict[str, tuple[int, int]]:
    """Per tag: the stores a trace's scan attempts, and how many are of a moving value.

    A tag with a working copy counts attempts on the copy, not the flush at scan end.
    """
    stores: dict[str, tuple[int, int]] = {}
    for key, counts in plan.stores.items():
        name = _tag_of_cell(key, block_names)
        if name is not None:
            stores[name] = (counts[0], counts[1])
    for key, counts in plan.working_copy_stores.items():
        stores[key[1]] = (counts[0], counts[1])
    return stores


def coast_moves(plan: _Tracer, block_names: Mapping[str, Mapping[int, str]]) -> CoastMoves:
    """What each scan of this trace does to the tags.

    The same source moves a tag differently from different values: a saturating
    timer adds 10 up to its limit and then nothing, so two traces are the same path
    only if this is the same too.
    """
    return _coast_guards(plan, block_names, frozenset(), frozenset())[1]


def coast_loop(
    plan: _Tracer,
    block_names: Mapping[str, Mapping[int, str]],
    watched: frozenset[str],
    seen: frozenset[str],
) -> tuple[Callable[..., tuple[int, float]], CoastMoves, str]:
    """This trace's loop, kept to :class:`CoastMoves`.

    It leaves before the scan that would change a *watched* tag.  After each scan
    it hands the moving tags in *seen* to its ``seen`` argument and asks ``stop``.
    These are only further reasons to leave, so a scan it does run is one the plain
    loop would also run.
    """
    guards, moves, seen_cells = _coast_guards(plan, block_names, watched, seen)
    loop, source, _body = _loop_function(plan, guards=tuple(guards), seen=tuple(seen_cells))
    return loop, moves, source


def _coast_guards(
    plan: _Tracer,
    block_names: Mapping[str, Mapping[int, str]],
    watched: frozenset[str],
    seen: frozenset[str],
) -> tuple[list[_Guard], CoastMoves, list[tuple[str, tuple[Any, ...]]]]:
    guards: list[_Guard] = []
    moved: list[tuple[str, int]] = []
    free: list[str] = []
    flips: list[tuple[str, Any, Any]] = []
    held: list[str] = []
    seen_cells: list[tuple[str, tuple[Any, ...]]] = []
    for key, name in moving_tags(plan, block_names).items():
        cell = plan.cells[key]
        old = plan.entry_locals[key]
        new = plan._atom(cell)
        uses = (old,) if cell.local is None else (old, cell.local)
        before = plan.entries[key].concrete
        after = cell.concrete
        if name in seen:
            seen_cells.append((name, key))
        free_to_move = name not in watched and cell.local is not None
        if free_to_move and type(after) is int and type(before) is int:
            delta = after - before
            guards.append(_Guard(f"{new} - {old} != {delta!r}", uses))
            if delta:
                moved.append((name, delta))
            else:
                held.append(name)
        elif free_to_move and type(after) is float and type(before) is float:
            free.append(name)
        elif name in watched or same(after, before) or after == before:
            # Equal across types too: 0 to False on an Int driven by out() is no transition.
            guards.append(_Guard(f"{old} != {new}", uses))
            if cell.local is not None and same(after, before):
                held.append(name)
        else:
            guards.append(_Guard(f"{old} != {plan._atom(_Value(before))}", (old,)))
            if cell.local is not None:
                guards.append(_Guard(f"{new} != {plan._atom(_Value(after))}", (cell.local,)))
            flips.append((name, before, after))
    return guards, CoastMoves(tuple(moved), tuple(free), tuple(flips), tuple(held)), seen_cells


# --- Driving the loop through a run --------------------------------------------


def _source_of(kernel: CompiledKernel) -> KernelSource:
    """The parsed source, kept on the kernel: a kernel serves many runs."""
    cached = kernel.__dict__.get("_scan_trace_source")
    if cached is None:
        cached = KernelSource(kernel)
        object.__setattr__(kernel, "_scan_trace_source", cached)
    return cached


@dataclass
class _PendingCheck:
    """A new trace's first iteration, on copies, awaiting the real scan's answer."""

    trace: ScanTrace
    tags: dict[str, Any]
    blocks: dict[str, _TrackedList]
    memory: dict[str, Any]
    timestamp: float
    scan_id: int


class ScanLoop:
    """One run's guarded loop.  Ask :meth:`advance` before every real scan."""

    _keep_plans = False

    def __init__(
        self,
        replay: CompiledPLC,
        kernel: CompiledKernel,
        budget: int,
        predicate: Callable[..., bool] | None,
        constrained: Mapping[str, TagConstraint],
    ) -> None:
        self._replay = replay
        self._artifact = kernel
        self._constrained = constrained
        self._stop_form = None if predicate is None else stop_form_of(predicate)
        self._seeds: set[tuple[Any, ...]] = {
            ("tags", name) for name, _half_period in replay._clock_system_tags
        } | {("tags", name) for name in replay._rtc_field_tags}
        self._traces: list[ScanTrace] = []
        self._built = 0
        self._may_build = min(MAX_TRACES_PER_RUN, 1 + budget // MIN_RUN_SCANS)
        self._pending: _PendingCheck | None = None
        self._just_checked: _PendingCheck | None = None
        self._attempt_wait = 0
        self._backoff = 0
        self._burst = 0
        self.counts: Counter[str] = Counter()
        self._off = self._whole_run_blocker(kernel, budget, predicate)
        if self._off is not None:
            self.counts[f"run declined: {self._off}"] += 1

    def _whole_run_blocker(
        self, kernel: CompiledKernel, budget: int, predicate: Callable[..., bool] | None
    ) -> str | None:
        if budget < MIN_RUN_SCANS:
            return "short run"
        if predicate is not None and self._stop_form is None:
            return "stop predicate form"
        return self._program_blocker(kernel)

    def _program_blocker(self, kernel: CompiledKernel) -> str | None:
        replay = self._replay
        if kernel.blockless:
            return "blockless kernel"
        if replay._forces:
            return "force"
        if replay._randint_sites:
            return "randint"
        for name in replay._referenced_system_tags:
            if name in DECLINING_SYSTEM_TAGS:
                return "scan-derived system tag"
            if name not in _CLASSIFIED_SYSTEM_TAGS:
                return "unclassified system tag"
        return None

    # -- The driver's calls -----------------------------------------------------

    def advance(self, limit: int) -> int:
        """Run up to *limit* scans in the loop; 0 means the next scan must be real."""
        if self._off is not None:
            return 0
        if self._attempt_wait:
            self._attempt_wait -= 1
            return 0
        if BURST_SIZES:
            limit = min(limit, BURST_SIZES[self._burst % len(BURST_SIZES)])
            self._burst += 1
        blocked = self._blocked()
        if blocked is not None:
            self._note_decline(blocked)
            return 0
        replay = self._replay
        for position, trace in enumerate(reversed(self._traces)):
            if trace.holds(replay, self._stop_form):
                used = self._fire(trace, limit)
                if used:
                    # Most recently fired last, so it is asked first next time.
                    self._traces.append(self._traces.pop(len(self._traces) - 1 - position))
                    self._after_firing(used, limit)
                    return used
                self.counts["misfires"] += 1
        self._trace_this_scan()
        return 0

    def real_scan_ran(self) -> None:
        """Note a real scan, and answer a pending self check.  Call it straight after
        the scan: materializing the state moves written block cells to the tags."""
        self.counts["real scans"] += 1
        pending = self._pending
        self._pending = None
        self._just_checked = pending
        if pending is None:
            return
        difference = self._difference_from_real(pending)
        if difference is not None:
            self._mismatch(difference, pending)
            return
        self._traces.append(pending.trace)

    def stop_held(self) -> None:
        """The real stop predicate held on the real scan just run."""
        checked = self._just_checked
        if checked is not None:
            self._mismatch("the stop predicate holds, but its guards passed", checked)

    def real_scan_failed(self, reason: str) -> None:
        """The real scan raised or left bounds.  A pending trace had run past that."""
        pending = self._pending
        self._pending = None
        if pending is not None:
            self._mismatch(f"its guards passed where the real scan {reason}", pending)

    def finish(self) -> None:
        """Add this run to :data:`TOTALS`."""
        TOTALS.update(self.counts)

    # -- Entry ------------------------------------------------------------------

    def _blocked(self) -> str | None:
        """What stops the next scan being loop material, or None.

        Leaves the system tags refreshed for the next scan, as the scan would.
        """
        replay = self._replay
        kernel = replay._kernel
        if replay._pending_patches:
            return "pending patch"
        if replay._forces:
            return "force"
        if kernel.scan_id == 0:
            return "first scan"
        if not replay._running:
            return "not running"
        if kernel.memory.get("_dt") != replay._dt:
            return "dt memory unset"
        runtime = replay._system_runtime
        if not runtime.scan_start_is_quiet(
            kernel.tags, kernel.memory
        ) or not runtime.scan_end_is_quiet(kernel.tags, replay._dt):
            return "hooks not quiet"
        replay._materialize_system_tags(
            _KernelRuntimeContext(
                tags=kernel.tags,
                memory=kernel.memory,
                scan_id=kernel.scan_id,
                timestamp=kernel.timestamp,
                block_pos=replay._block_pos,
                blocks=kernel.blocks,
            )
        )
        return None

    def _rtc_fields(self, timestamp: float) -> tuple[Mapping[str, Any], float, float]:
        replay = self._replay
        kernel = replay._kernel
        return replay._rtc_fields_at(
            _KernelRuntimeContext(
                tags=kernel.tags,
                memory=kernel.memory,
                scan_id=kernel.scan_id,
                timestamp=timestamp,
            )
        )

    # -- Firing -----------------------------------------------------------------

    def _fire(self, trace: ScanTrace, limit: int) -> int:
        kernel = self._replay._kernel
        used, timestamp = trace.loop(
            kernel.tags,
            kernel.blocks,
            kernel.memory,
            kernel.timestamp,
            self._replay._dt,
            limit,
            self._rtc_fields,
        )
        if used:
            kernel.scan_id += used
            kernel.timestamp = timestamp
            _record_block_writes(trace, kernel.blocks)
            self.counts["loop scans"] += used
        return used

    def _after_firing(self, used: int, limit: int) -> None:
        if used >= min(limit, UNPRODUCTIVE_BELOW):
            self._backoff = 0
            self._attempt_wait = 0
        else:
            self._back_off()
        if used < limit:
            # A guard failed: this very state needs a real scan, whatever else.
            self.counts["exits"] += 1
            self._attempt_wait = max(self._attempt_wait, 1)

    def _back_off(self) -> None:
        self._backoff = min(max(1, self._backoff * 2), MAX_ATTEMPT_WAIT)
        self._attempt_wait = self._backoff

    def _note_decline(self, reason: str) -> None:
        self.counts[f"decline: {reason}"] += 1
        self._back_off()

    # -- Tracing ----------------------------------------------------------------

    def _trace_this_scan(self) -> None:
        if self._built >= self._may_build:
            self._note_decline("trace cap")
            return
        self._built += 1
        if self._traces:
            self._seeds |= self._traces[-1].changed_constants(self._replay) - _HOOK_KEYS
        started = time.perf_counter()
        try:
            trace = build_trace(
                _source_of(self._artifact),
                self._replay,
                self._seeds,
                self._stop_form,
                self._constrained,
                keep_plan=self._keep_plans,
            )
        except Decline as declined:
            self._note_decline(str(declined))
            return
        except Exception as exc:
            if STRICT_SELF_CHECK:
                raise
            self._note_decline(f"tracer error: {type(exc).__name__}")
            return
        finally:
            self.counts["trace microseconds"] += int((time.perf_counter() - started) * 1e6)
        self.counts["traces"] += 1
        self._begin_self_check(trace)

    def _begin_self_check(self, trace: ScanTrace) -> None:
        kernel = self._replay._kernel
        tags = dict(kernel.tags)
        memory = dict(kernel.memory)
        blocks = {symbol: _copy_tracked(array) for symbol, array in kernel.blocks.items()}
        used, timestamp = trace.loop(
            tags,
            blocks,
            memory,
            kernel.timestamp,
            self._replay._dt,
            1,
            self._rtc_fields,
        )
        if not used:
            # The trace's own state fails a guard, or one of its operations raises
            # in the loop though not in the tracer: nothing to check, nothing to keep.
            self._note_decline("trace left on its own scan")
            return
        _record_block_writes(trace, blocks)
        self._pending = _PendingCheck(trace, tags, blocks, memory, timestamp, kernel.scan_id)

    def _difference_from_real(self, pending: _PendingCheck) -> str | None:
        kernel = self._replay._kernel
        if pending.scan_id + 1 != kernel.scan_id:
            return f"scan id {pending.scan_id + 1} against {kernel.scan_id}"
        if not same(pending.timestamp, kernel.timestamp):
            return f"timestamp {pending.timestamp!r} against {kernel.timestamp!r}"
        tags = _first_difference(pending.tags, kernel.tags)
        if tags is not None:
            return f"tag {tags}"
        memory = _first_difference(pending.memory, kernel.memory)
        if memory is not None:
            return f"memory {memory}"
        for symbol, array in cast("dict[str, _TrackedList]", kernel.blocks).items():
            copy = pending.blocks[symbol]
            for index, (ours, real) in enumerate(zip(copy, array, strict=True)):
                if not same(ours, real):
                    return f"block {symbol}[{index}]: loop {ours!r}, real {real!r}"
            if copy.written_indices != array.written_indices:
                return f"block {symbol} write record"
        return None

    def _mismatch(self, difference: str, pending: _PendingCheck) -> None:
        message = f"scan loop self check: {difference}"
        for observer in SELF_CHECK_OBSERVERS:
            observer(message)
        if STRICT_SELF_CHECK:
            raise SelfCheckMismatch(f"{message}\nloop:\n{pending.trace.source}")
        logger.warning("%s; stepping the rest of this run", message)
        self.counts["self check mismatches"] += 1
        self._off = "self check mismatch"


def _copy_tracked(array: Any) -> _TrackedList:
    copy = _TrackedList(array)
    copy.written_indices = set(array.written_indices)
    return copy


def _record_block_writes(trace: ScanTrace, blocks: Mapping[str, Any]) -> None:
    """A scan rewriting a cell with its own value still marks it written: from then
    on the cell is live in the committed state.  The loop skips the rewrite."""
    for symbol, cells in trace.block_writes:
        blocks[symbol].written_indices.update(cells)


def _first_difference(ours: Mapping[str, Any], real: Mapping[str, Any]) -> str | None:
    for key, value in real.items():
        if key not in ours:
            return f"{key}: absent from the loop, real {value!r}"
        if not same(ours[key], value):
            return f"{key}: loop {ours[key]!r}, real {value!r}"
    for key in ours:
        if key not in real:
            return f"{key}: loop {ours[key]!r}, absent from the real scan"
    return None
