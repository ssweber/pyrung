"""PLC - Generator-driven PLC execution engine.

The runner orchestrates scan cycle execution with inversion of control.
The consumer drives execution via step(), allowing input injection,
inspection, and pause at any point.

Uses ScanContext to batch all tag/memory updates within a scan cycle,
reducing object allocation from O(instructions) to O(1) per scan.
"""

from __future__ import annotations

import builtins
import dis
import inspect
import logging
import math
import time
import warnings
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import Token
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import count

logger = logging.getLogger(__name__)
from types import CodeType, MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, TypeGuard, TypeVar, cast

from pyrsistent import PMap, pmap

from pyrung.core.analysis.causal.models import Transition
from pyrung.core.bounds import BoundsViolation, TagConstraint, build_constraint_index, check_bounds
from pyrung.core.compiled_plc import CompiledPLC, absent_scalars
from pyrung.core.condition_trace import ConditionTraceEngine
from pyrung.core.context import ConditionView, RungId, ScanContext
from pyrung.core.debug_trace import RungTrace, RungTraceEvent, TraceEvent
from pyrung.core.debugger import PLCDebugger
from pyrung.core.executor import (
    ConditionViewCapture,
    RungRun,
    execute_observed_rung,
    execute_program,
)
from pyrung.core.expression import RandIntExpr, randint_sites
from pyrung.core.history import History
from pyrung.core.input_overrides import (
    InputOverrideManager,
    TagsKnownOrReached,
    TagsRungsReach,
)
from pyrung.core.kernel import CompiledKernel
from pyrung.core.live_binding import reset_active_runner, set_active_runner
from pyrung.core.rung_firings import (
    _UNKNOWN_VALUE,
    SETTLE_SCAN_LIMIT,
    RungFiringTimelines,
    SearchQuestionRefused,
    UnrecordedScansError,
)
from pyrung.core.scan_log import LifecycleEvent, LifecycleKind, ScanLog, ScanLogSnapshot
from pyrung.core.site_memory import bind_logic_list, migrate_legacy_edge_memory
from pyrung.core.state import SystemState
from pyrung.core.system_points import (
    _BATTERY_PRESENT_KEY,
    _MODE_RUN_KEY,
    _SCAN_TIME_MIN_NAME,
    READ_ONLY_SYSTEM_TAG_NAMES,
    SYSTEM_TAGS_BY_NAME,
    SystemPointRuntime,
)
from pyrung.core.time_mode import TimeMode
from pyrung.core.trace_formatter import TraceFormatter
from pyrung.core.validation._common import _collect_write_sites, walk_instructions
from pyrung.core.validation.readonly_write import _any_write_targets

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from pyrung.core.analysis.causal import CausalChain
    from pyrung.core.coast_run import CoastMemo
    from pyrung.core.condition import Condition
    from pyrung.core.rung import Rung
    from pyrung.core.tag import Tag, TagType

_SENTINEL = object()  # distinguishes "not passed" from None/False

_CHECKPOINT_INTERVAL_DEFAULT = 200
# One causal working set, independent of checkpoint spacing.  Checkpoints may
# intentionally be sparse (or skipped by a long compiled run); they are replay anchors,
# not a license to materialize the entire gap into memory.
_REPLAY_SLAB_SCANS = 1600
_REPLAY_SLAB_MAX_ANCHORS = 1
# Exact causal walks commonly revisit a small set of historical scans while
# explaining several changed tags.  Keep that immutable interpreted evidence
# by execution epoch; one slot thrashes when a counterfactual investigation
# alternates among the same source/action/landing scans.
_REPLAY_CAPTURE_CACHE_SCANS = 16

_EPOCH_REFERENCE_VALUES = count(1)

# Byte budget for the recent-state cache (default for ``history_budget``).
_HISTORY_BUDGET_BYTES_DEFAULT = 100 * 1024 * 1024  # 100 MB

# Floor: never evict below this many entries regardless of byte budget.
# Monitor ``previous_value`` reads assume N-1 is always
# present; the recent-state cache floor must not regress under budget pressure.
_RECENT_STATE_CACHE_MIN_ENTRIES = 20

_T = TypeVar("_T")


def _parse_retention(value: str | int | None, dt_seconds: float) -> int | None:
    """Convert a retention parameter to a scan count.

    Accepts ``None`` (unlimited), ``int`` (literal scan count), or a
    duration string parseable by ``parse_duration`` (e.g. ``"1h"``).
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    from pyrung.core.physical import parse_duration

    ms = parse_duration(value)
    dt_ms = dt_seconds * 1000.0
    return max(1, int(ms / dt_ms))


# Conservative per-entry byte estimate for PMap HAMT nodes.
_PER_PMAP_ENTRY_BYTES = 200

# Fixed overhead for PRecord shell (scan_id, timestamp, two PMap roots).
_STATE_BASE_BYTES = 200


def _estimate_state_bytes(state: SystemState) -> int:
    """Conservative ceiling estimate of a ``SystemState``'s memory footprint.

    Uses PMap entry count times a fixed per-entry constant.  States are
    structurally shared, so summing independent sizes would massively
    overcount; this estimator is deliberately coarse.
    """
    entries = len(state.tags) + len(state.memory)
    return _STATE_BASE_BYTES + entries * _PER_PMAP_ENTRY_BYTES


def _validate_assume(logic: list[Any], assume: dict[str, Any]) -> None:
    """Raise ``ValueError`` if *assume* targets a readonly tag."""
    from pyrung.core.analysis.query import find_tag_object

    for name in assume:
        tag_obj = find_tag_object(logic, name)
        if tag_obj is not None and tag_obj.readonly:
            raise ValueError(f"cannot assume value for readonly tag {name!r}")


@dataclass(frozen=True)
class ScanStep:
    """Debug scan step emitted at rung boundaries."""

    rung_index: int
    rung: Rung
    ctx: ScanContext
    kind: Literal["rung", "branch", "subroutine", "loop_body", "instruction"]
    subroutine_name: str | None
    depth: int
    call_stack: tuple[str, ...]
    source_file: str | None
    source_line: int | None
    end_line: int | None
    enabled_state: Literal["enabled", "disabled_local", "disabled_parent"] | None
    trace: TraceEvent | None
    instruction_kind: str | None
    #: Place in the top-level rung's tree (``()`` for the rung, ``(k,)`` for its
    #: k-th branch); ``None`` in a called subroutine or a loop body.
    branch_path: tuple[int, ...] | None = None


@dataclass
class _MonitorRegistration:
    id: int
    tag_name: str
    callback: Callable[[Any, Any], None]
    enabled: bool = True
    removed: bool = False


@dataclass
class _BreakpointRegistration:
    id: int
    predicate: Callable[[SystemState], bool]
    action: Literal["pause", "snapshot", "do"]
    label: str | None = None
    callback: Callable[[SystemState], None] | None = None
    enabled: bool = True
    removed: bool = False


# Longest run_for() counted in advance, at a few tens of ns a scan.  Past this it steps.
_RUN_FOR_MAX_COUNTED_SCANS = 500_000_000


class _ConditionPredicate:
    """A ``run_until()`` / ``when()`` condition with no edges: stateless."""

    __slots__ = ("_condition", "_context")

    guard_edges: tuple[Any, ...] = ()

    def __init__(self, condition: Any, context: Callable[[SystemState], Any]) -> None:
        self._condition = condition
        self._context = context

    def __call__(self, state: SystemState) -> bool:
        return self._condition.evaluate(self._context(state))

    @property
    def guard_condition(self) -> Any:
        return self._condition

    @staticmethod
    def guard_last_seen() -> Mapping[str, bool]:
        return {}


class _EdgePredicate:
    """A ``run_until()`` / ``when()`` condition holding ``rise()`` / ``fall()``.

    Its edges compare against the bit as this predicate last judged it, armed
    from the runner's current state.  A breakpoint registers its own armed
    copy (two registrations from one ``when()`` never share memory) and
    re-arms it when re-enabled, so the scans it skipped while disabled do not
    count as an edge.
    """

    __slots__ = ("_condition", "_edges", "_context", "_current", "_last_seen")

    def __init__(
        self,
        condition: Any,
        edges: Sequence[Any],
        context: Callable[[SystemState], Any],
        current: Callable[[], SystemState],
    ) -> None:
        self._condition = condition
        self._edges = tuple(edges)
        self._context = context
        self._current = current
        self._last_seen: dict[str, bool] = {}
        self.rearm()

    def _bits(self, ctx: Any) -> dict[str, bool]:
        return {
            edge._memory_key: bool(ctx.get_tag(edge._resolved_tag.name, False))
            for edge in self._edges
        }

    def rearm(self) -> None:
        """Remember every edge's bit as it is in the runner's current state."""
        self._last_seen = self._bits(self._context(self._current()))

    def armed(self) -> _EdgePredicate:
        """An independent copy, armed now."""
        return _EdgePredicate(self._condition, self._edges, self._context, self._current)

    def __call__(self, state: SystemState) -> bool:
        ctx = self._context(state)
        for key, bit in self._last_seen.items():
            ctx.set_memory(key, bit)
        hit = bool(self._condition.evaluate(ctx))
        self._last_seen.update(self._bits(ctx))
        return hit

    @property
    def guard_condition(self) -> Any:
        return self._condition

    @property
    def guard_edges(self) -> tuple[Any, ...]:
        return self._edges

    def guard_last_seen(self) -> Mapping[str, bool]:
        return self._last_seen


class _RunnerHandle:
    """Mutable registration handle used by monitor/breakpoint APIs."""

    __slots__ = ("_id", "_remove", "_enable", "_disable")

    def __init__(
        self,
        *,
        handle_id: int,
        remove: Callable[[int], None],
        enable: Callable[[int], None],
        disable: Callable[[int], None],
    ) -> None:
        self._id = handle_id
        self._remove = remove
        self._enable = enable
        self._disable = disable

    @property
    def id(self) -> int:
        return self._id

    def remove(self) -> None:
        self._remove(self._id)

    def enable(self) -> None:
        self._enable(self._id)

    def disable(self) -> None:
        self._disable(self._id)


class _BreakpointBuilder:
    """Fluent builder returned by ``runner.when(...)``."""

    __slots__ = ("_runner", "_predicate")

    def __init__(self, runner: PLC, predicate: Callable[[SystemState], bool]) -> None:
        self._runner = runner
        self._predicate = predicate

    def pause(self) -> _RunnerHandle:
        return self._runner._register_breakpoint(
            predicate=self._predicate,
            action="pause",
            label=None,
        )

    def snapshot(self, label: str) -> _RunnerHandle:
        return self._runner._register_breakpoint(
            predicate=self._predicate,
            action="snapshot",
            label=label,
        )

    def do(self, callback: Callable[[SystemState], None]) -> _RunnerHandle:
        """Run ``callback(state)`` after each committed scan where the condition holds.

        The callback receives the post-scan :class:`SystemState` and may drive the
        runner via ``plc.patch`` / ``plc.force``.  Unlike :meth:`pause`, the run
        continues.  This is the hook for reactive inputs — e.g. a liveness
        oscillator ``when(tag != v).do(lambda s: plc.patch({tag: v}))`` — which the
        runner then applies every scan.
        Use ``patch`` (one-shot) rather than ``force`` for a reactive re-assert —
        a force would pin the tag so the program could never drift it.
        """
        return self._runner._register_breakpoint(
            predicate=self._predicate,
            action="do",
            label=None,
            callback=callback,
        )


def _iter_tags_and_edge_names(
    roots: Iterable[Any],
    *,
    block_uses: list[Any] | None = None,
    beyond_rungs: list[Tag] | None = None,
) -> tuple[tuple[Tag, ...], frozenset[str]]:
    """Single-pass graph walk collecting both Tag objects and edge tag names.

    The tags returned are what the rungs say.  A range holds its block, whose cache is every
    slot anything has asked for, and a call holds its program, which holds what has run and
    been asked of it, so the walk takes neither on its way.  Of a block's slots it takes the
    ones a map_to put there; of a program, its rungs and subroutines.

    *block_uses*, when given, also collects each range and pointer into a block.
    *beyond_rungs*, when given, also collects the tags found by going on into those blocks
    and programs, once the rungs are done: some other tag of the same name yields to a rung's.
    """
    from pyrung.core.condition import FallingEdgeCondition, RisingEdgeCondition
    from pyrung.core.memory_block import (
        Block,
        BlockRange,
        IndirectBlockRange,
        IndirectExprRef,
        IndirectRef,
    )
    from pyrung.core.program import Program
    from pyrung.core.tag import Tag as TagClass

    found_by_name: dict[str, TagClass] = {}
    edge_names: set[str] = set()
    visited: set[int] = set()
    queue: list[Any] = list(roots)
    held_back: dict[int, Any] = {}
    on_the_rungs = True
    on_rungs_by_name: dict[str, TagClass] = {}

    while True:
        while queue:
            current = queue.pop()
            if current is None:
                continue
            if isinstance(current, TagClass):
                found_by_name[current.name] = current
                continue
            if isinstance(current, (RisingEdgeCondition, FallingEdgeCondition)):
                edge_names.add(current._resolved_tag.name)
                # Still need to walk children for Tag references
                current_id = id(current)
                if current_id in visited:
                    continue
                visited.add(current_id)
                if hasattr(current, "__dict__"):
                    queue.extend(vars(current).values())
                continue
            if isinstance(current, (str, bytes, bytearray, int, float, bool)):
                continue

            current_id = id(current)
            if on_the_rungs and isinstance(current, Block | Program):
                if current_id not in held_back:
                    held_back[current_id] = current
                    if isinstance(current, Block):
                        queue.extend(current._mapped_tags.values())
                    else:
                        queue.extend(current.rungs)
                        queue.extend(current.subroutines.values())
                continue
            if current_id in visited:
                continue
            visited.add(current_id)

            if (
                on_the_rungs
                and block_uses is not None
                and isinstance(
                    current, BlockRange | IndirectRef | IndirectExprRef | IndirectBlockRange
                )
            ):
                block_uses.append(current)

            if isinstance(current, Mapping):
                queue.extend(current.keys())
                queue.extend(current.values())
                continue
            if isinstance(current, tuple | list | set | frozenset):
                queue.extend(current)
                continue

            if hasattr(current, "__dict__"):
                queue.extend(vars(current).values())
                continue
            if hasattr(current, "__slots__"):
                for slot in current.__slots__:
                    if slot in {"__weakref__", "__dict__"}:
                        continue
                    if hasattr(current, slot):
                        queue.append(getattr(current, slot))

        if beyond_rungs is None or not on_the_rungs:
            break
        on_the_rungs = False
        on_rungs_by_name = found_by_name
        found_by_name = {}
        queue = list(held_back.values())

    if beyond_rungs is None:
        return tuple(found_by_name.values()), frozenset(edge_names)
    beyond_rungs.extend(tag for name, tag in found_by_name.items() if name not in on_rungs_by_name)
    return tuple(on_rungs_by_name.values()), frozenset(edge_names)


def _apply_lifecycle_to_replay(replay: Any, event: LifecycleEvent) -> None:
    """Apply a captured lifecycle event to a replay PLC.

    ``reboot`` never appears in a live log snapshot because
    ``PLC.reboot()`` resets the scan log — surfacing it here flags a
    regression in that invariant.
    """
    if event.kind == "stop":
        replay.stop()
    elif event.kind == "battery_present":
        replay.battery_present = bool(event.value)
    elif event.kind == "clear_forces":
        replay.clear_forces()
    elif event.kind == "reboot":
        raise AssertionError(
            "unexpected reboot lifecycle event in replay log "
            "(reboot() should reset the log; see PLC.reboot())"
        )
    else:  # pragma: no cover - exhaustive
        raise AssertionError(f"unknown lifecycle kind: {event.kind!r}")


def _program_writes_read_only_system_tags(program: Any) -> bool:
    from pyrung.core.program import Program

    if not isinstance(program, Program):
        return False
    for site in _collect_write_sites(program, target_extractor=_any_write_targets):
        if site.target_name in READ_ONLY_SYSTEM_TAG_NAMES:
            return True
    return False


def _global_names(code: CodeType) -> tuple[set[str], set[str]]:
    """The global names *code* and the code nested in it read, and those it binds."""
    read: set[str] = set()
    bound: set[str] = set()
    for instruction in dis.get_instructions(code):
        if instruction.opname in ("LOAD_GLOBAL", "LOAD_NAME"):
            read.add(instruction.argval)
        elif instruction.opname in ("STORE_GLOBAL", "DELETE_GLOBAL"):
            bound.add(instruction.argval)
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            inner_read, inner_bound = _global_names(constant)
            read |= inner_read
            bound |= inner_bound
    return read, bound


#: Builtins whose answer depends only on what they are handed, in any module.  Anything
#: else (``globals``, ``eval``, ``vars``, ``getattr``, ``open``, ``print``...) can reach
#: the function's module or the world, or is simply not worth the proof.
_SELF_CONTAINED_BUILTINS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bin",
        "bool",
        "bytes",
        "chr",
        "complex",
        "dict",
        "divmod",
        "enumerate",
        "filter",
        "float",
        "frozenset",
        "hex",
        "int",
        "isinstance",
        "iter",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "oct",
        "ord",
        "pow",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    }
)

#: Attributes that hand a function a frame, and through it the globals of its module.
#: ``format`` and ``format_map`` are here for the same reason: they walk attributes named
#: in a text that can be built while the function runs, so no constant can clear them.
_ATTRIBUTES_PAST_ITS_SCOPE = frozenset(
    {
        "format",
        "format_map",
        "ag_frame",
        "cr_frame",
        "f_back",
        "f_builtins",
        "f_globals",
        "f_locals",
        "gi_frame",
        "tb_frame",
    }
)


def _is_self_contained_builtin(name: str) -> bool:
    if name in _SELF_CONTAINED_BUILTINS:
        return True
    # Not a BaseException outside Exception: a batch that raises one of those is not undone.
    builtin = getattr(builtins, name, None)
    return isinstance(builtin, type) and issubclass(builtin, Exception)


def _reaches_past_its_scope(code: CodeType) -> bool:
    """Whether *code* imports, sets an attribute, or asks an object for a dunder or a frame.

    An import or an attribute of ``math`` shares state with every other call, and a dunder
    (``__globals__``, ``__defaults__``, ``__traceback__``), a frame or ``str.format``
    (``"{0.__globals__}"``) hands over the function's own module, by a route no global
    name shows.
    """
    for instruction in dis.get_instructions(code):
        if instruction.opname in ("IMPORT_NAME", "STORE_ATTR", "DELETE_ATTR"):
            return True
    for name in code.co_names:
        if name.startswith("__") or name in _ATTRIBUTES_PAST_ITS_SCOPE:
            return True
    return any(
        _reaches_past_its_scope(constant)
        for constant in code.co_consts
        if isinstance(constant, CodeType)
    )


def _is_immutable_constant(value: Any) -> bool:
    if type(value) in (tuple, frozenset):
        return all(_is_immutable_constant(item) for item in value)
    return value is None or type(value) in (bool, int, float, complex, str, bytes)


def _may_be_called_again(fn: Any) -> bool:
    """Whether a kernel may run *fn*, given that an undo means calling it again.

    A kernel calls the function itself, so its names, defaults and cells are the ones
    stepping sees.  What a kernel can't promise is one call per scan: a batch that raises
    or leaves its bounds is undone and stepped, which calls the function again for scans
    it has already run.  So a function that keeps anything from one call to the next, or
    reaches past its own frame to something that can (a closure cell, a global, a mutable
    default, an import, a dunder or a frame), runs stepped, once per scan.
    """
    if not inspect.isfunction(fn) or fn.__closure__ or fn.__builtins__ is not vars(builtins):
        return False
    defaults = (*(fn.__defaults__ or ()), *(fn.__kwdefaults__ or {}).values())
    if not all(_is_immutable_constant(default) for default in defaults):
        return False
    if _reaches_past_its_scope(fn.__code__):
        return False
    read, bound = _global_names(fn.__code__)
    if bound:
        return False
    function_globals = fn.__globals__
    for name in read:
        if name == "math" and function_globals.get(name) is math:
            continue  # Nothing in it depends on a past call.
        if name in function_globals or not _is_self_contained_builtin(name):
            return False
    return True


def _hands_over_nothing_it_could_keep(value: Any) -> bool:
    """Whether an ``ins`` value reaches the function as a fresh value, or one nothing can change.

    A list a function appends to, or any object it can call, is state like a mutable default.
    """
    from pyrung.core.expression import Expression
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef
    from pyrung.core.tag import ImmediateRef, Tag

    return isinstance(
        value, Tag | IndirectRef | IndirectExprRef | Expression | ImmediateRef
    ) or _is_immutable_constant(value)


def calls_that_depend_on_more_than_their_inputs(program: Any) -> list[tuple[Any, str]]:
    """Each ``run_function`` whose answer may not follow from its inputs, and why.

    The one gate on calling a function anywhere but its own scan: a kernel's undo calls it
    again, and the verifier calls it to explore, for inputs no scan may ever give it.
    """
    from pyrung.core.instruction.control import (
        EnabledFunctionCallInstruction,
        FunctionCallInstruction,
    )
    from pyrung.core.program import Program

    if not isinstance(program, Program):
        return []
    found: list[tuple[Any, str]] = []
    for instruction in walk_instructions(program):
        if not isinstance(instruction, FunctionCallInstruction | EnabledFunctionCallInstruction):
            continue
        if not _may_be_called_again(instruction._fn):
            found.append(
                (
                    instruction,
                    "it is not a plain def of its arguments: it is a callable object, or uses "
                    "a closure, a global, a helper, a mutable default or an import",
                )
            )
            continue
        for key, value in instruction._ins.items():
            if not _hands_over_nothing_it_could_keep(value):
                found.append((instruction, f"ins[{key!r}] is an object it could change or keep"))
                break
    return found


def _program_calls_what_a_kernel_may_not(program: Any) -> bool:
    return bool(calls_that_depend_on_more_than_their_inputs(program))


def _looks_like_compiled_replay_gap(exc: Exception) -> bool:
    if isinstance(exc, NotImplementedError | NameError | SyntaxError):
        return True  # A kernel that doesn't build is a kernel not used: the run steps.
    if not isinstance(exc, ValueError | TypeError):
        return False
    message = str(exc)
    return any(
        needle in message
        for needle in (
            "requires generate_circuitpy",
            "Unsupported",
        )
    )


class _DebugNamespace:
    """Namespace exposing debugger-facing methods on ``plc.debug``."""

    __slots__ = ("_plc",)

    def __init__(self, plc: PLC) -> None:
        self._plc = plc

    # -- DebugRunner protocol (used by PLCDebugger) --

    def prepare_scan(self) -> tuple[ScanContext, float]:
        return self._plc._prepare_scan()

    def commit_scan(self, ctx: ScanContext, dt: float) -> None:
        self._plc._commit_scan(ctx, dt)

    def iter_top_level_rungs(self) -> Iterable[Rung]:
        return self._plc._logic

    def evaluate_condition_value(
        self,
        condition: Any,
        ctx: ScanContext | ConditionView,
    ) -> tuple[bool, list[dict[str, Any]]]:
        return self._plc._evaluate_condition_value(condition, ctx)

    def condition_term_text(self, condition: Any, details: list[dict[str, Any]]) -> str:
        return self._plc._condition_term_text(condition, details)

    def condition_annotation(self, *, status: str, expression: str, summary: str) -> str:
        return self._plc._condition_annotation(
            status=status, expression=expression, summary=summary
        )

    def condition_expression(self, condition: Any) -> str:
        return self._plc._condition_expression(condition)

    # -- Public debug surface --

    def scan_steps(self) -> Generator[tuple[int, Rung, ScanContext], None, None]:
        """Execute one scan cycle yielding after each rung."""
        return self._plc._scan_steps()

    def scan_steps_debug(self) -> Generator[ScanStep, None, None]:
        """Execute one scan cycle yielding fine-grained debug steps."""
        return self._plc._scan_steps_debug()

    def abandon_scan(self) -> None:
        """Forget a partially stepped scan without committing it.

        Call this when dropping a ``scan_steps()`` / ``scan_steps_debug()``
        generator before it is exhausted.  The scan already took the pending
        patches; they are queued again (ahead of any newer patch for the same
        tag) so the next scan applies them instead of losing them.
        """
        self._plc._requeue_drained_patches()

    def rung_trace(self, rung_id: int) -> RungTrace:
        """Return rung-level debug trace for the most recently committed scan.

        Only the most recent debug scan's traces are retained — historical
        scan inspection is not supported.
        """
        return self._plc._inspect(rung_id)

    def rung_firings(self, scan_id: int | None = None) -> PMap:
        """Return rung firings for the given scan (default: playhead)."""
        return self._plc.rung_firings(scan_id)

    def last_event(self) -> tuple[int, int, RungTraceEvent] | None:
        """Return the latest debug-trace event."""
        return self._plc._inspect_event()

    @property
    def system_runtime(self) -> SystemPointRuntime:
        """System point runtime component."""
        return self._plc._system_runtime


def _count_visible_changes(steps: list[Any], tag_defaults: dict[str, Any]) -> int:
    total = 0
    prev_action: dict[str, Any] = {}
    for s in steps:
        if not prev_action:
            total += sum(1 for k, v in s.action.items() if v != tag_defaults.get(k))
        else:
            total += sum(1 for k, v in s.action.items() if prev_action.get(k) != v)
        prev_action = s.action
    return total


def _render_avoid_expr(expr: Any) -> str:
    """Compact human name for one avoid condition's compiled expression."""
    form = getattr(expr, "form", None)
    if form is not None:  # an Atom (tag / comparison)
        tag = expr.tag
        if form == "xic":
            return str(tag)
        if form == "xio":
            return f"~{tag}"
        symbols = {"eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
        return f"{tag} {symbols.get(form, form)} {expr.operand!r}"
    terms = getattr(expr, "terms", None)
    if terms is not None:
        joiner = " | " if type(expr).__name__.lower().startswith("or") else " & "
        return "(" + joiner.join(_render_avoid_expr(t) for t in terms) + ")"
    return type(expr).__name__


def _avoid_expr_has_edge(expr: Any) -> bool:
    """Whether a compiled property needs transition rather than snapshot state."""
    if getattr(expr, "form", None) in {"rise", "fall"}:
        return True
    return any(_avoid_expr_has_edge(child) for child in getattr(expr, "terms", ()) or ())


def _avoid_condition(spec: Any, expr: Any) -> Any | None:
    """Return the executable condition of one condition-like avoid.

    Opaque callables have no expression and therefore none.
    """

    if expr is None:
        return None

    from pyrung.core.condition import _as_condition, _normalize_and_condition

    return _normalize_and_condition(
        spec,
        coerce=_as_condition,
        empty_error="avoid condition cannot be empty",
        group_empty_error="avoid condition group cannot be empty",
    )


def _compile_avoid(spec: Any) -> Any:
    """Compile ``avoid=`` into a :class:`_AvoidPredicate` (a **union**).

    Each condition is avoided independently: ``avoid=(A, B)`` / ``avoid=[A, B]``
    exclude a path that depends on *either*, while ``avoid=And(A, B)`` is a single
    member that excludes only the combined state.  Every member keeps its own
    printable name and condition so a decline can point at what it violated and
    a PILOT coast can say what it reads. Opaque callables retain
    only their predicate; rise/fall transition conditions are rejected because
    avoidance is defined over snapshots.
    """
    if spec is None:
        return None
    from pyrung.core.analysis.pilot.types import _AvoidMember, _AvoidPredicate
    from pyrung.core.analysis.prove import _compile_property

    conds = list(spec) if isinstance(spec, (tuple, list)) else [spec]
    members: list[Any] = []
    for cond in conds:
        pred, tags, expr = _compile_property(cond)
        if _avoid_expr_has_edge(expr):
            raise ValueError(
                "avoid= does not support rise()/fall() transition conditions; "
                "use a snapshot state condition or callable"
            )
        name = _render_avoid_expr(expr) if expr is not None else ", ".join(tags or ()) or "avoid"
        members.append(
            _AvoidMember(
                name=name,
                pred=pred,
                tags=frozenset(tags or ()),
                condition=_avoid_condition(cond, expr),
            )
        )
    return _AvoidPredicate(tuple(members))


_EpochCacheName = Literal[
    "recent_states",
    "replay_slabs",
    "replay_trace",
    "replay_captures",
]
_EpochCacheEvent = Literal[
    "tip_advanced",
    "history_trimmed",
    "runtime_scope_reset",
    "recording_reset",
    "replay_anchor_replaced",
    "replay_evidence_discarded",
]


class EpochCaches:
    """Invalidation registry for history-derived epoch caches.

    The matrix deliberately preserves the lifecycle asymmetry.  Trimming
    prunes surviving recent states separately.  A runtime-scope reset and a
    recording reset are distinct events: a fork's time-mode setup starts a log
    with no runtime reset.  Reboot and STOP-to-RUN compose both, because each
    returns scan ids to 0 and a slab from before would answer for a new scan.
    """

    INVALIDATION_MATRIX: Mapping[_EpochCacheEvent, tuple[_EpochCacheName, ...]] = MappingProxyType(
        {
            "tip_advanced": ("replay_trace", "replay_captures"),
            "history_trimmed": ("replay_slabs",),
            "runtime_scope_reset": (
                "recent_states",
                "replay_trace",
                "replay_captures",
            ),
            "recording_reset": ("replay_slabs",),
            "replay_anchor_replaced": ("recent_states",),
            "replay_evidence_discarded": ("replay_trace", "replay_captures"),
        }
    )

    def __init__(self, plc: PLC) -> None:
        self._plc = plc

    def _invalidate(self, event: _EpochCacheEvent) -> None:
        for cache_name in self.INVALIDATION_MATRIX[event]:
            self._clear(cache_name)

    def _clear(self, cache_name: _EpochCacheName) -> None:
        if cache_name == "recent_states":
            self._plc._recent_state_cache = OrderedDict()
            self._plc._recent_state_cache_bytes = 0
        elif cache_name == "replay_slabs":
            self._plc._replay_slabs = {}
        elif cache_name == "replay_trace":
            self._plc._cached_replay_trace = None
        elif cache_name == "replay_captures":
            self._plc._cached_replay_captures = OrderedDict()

    def on_tip_advanced(self) -> None:
        self._invalidate("tip_advanced")

    def on_history_trimmed(self) -> None:
        self._invalidate("history_trimmed")

    def on_runtime_scope_reset(self) -> None:
        self._invalidate("runtime_scope_reset")

    def on_recording_reset(self) -> None:
        self._invalidate("recording_reset")

    def on_replay_anchor_replaced(self) -> None:
        self._invalidate("replay_anchor_replaced")

    def on_replay_evidence_discarded(self) -> None:
        self._invalidate("replay_evidence_discarded")


@dataclass(frozen=True, order=True)
class EpochRef:
    """Stable identity of one physical execution interval in a lineage."""

    value: int


def _new_epoch_ref() -> EpochRef:
    return EpochRef(next(_EPOCH_REFERENCE_VALUES))


class AvoidCheckError(Exception):
    """An ``avoid=`` check raised while a how() was judging a state.

    A check that raised did not say the state is clear, so how() does not go through it,
    and it proved nothing about the target.  So there is no plan to return, and the check's
    own exception is the ``__cause__``.
    """

    def __init__(self, check: str, error: Exception) -> None:
        super().__init__(
            f"avoid check {check!r} raised {type(error).__name__}: {error}. "
            "how() cannot tell whether that state is avoided, so it stopped without a plan. "
            "A callable reads a tag a state may not have yet with snapshot.get(name, default). "
            "A condition compares what it finds, so check each tag it names is in the program "
            "and the two sides can be compared."
        )
        self.check = check


class SearchStopped(Exception):
    """A how() world stopped itself, and its next scan would restart it.

    A restart is a new recording with scan ids from 0.  A plan is scans of one
    recording, so how() ends here rather than step over it.
    """

    def __init__(self, scan_id: int) -> None:
        super().__init__(f"the program stopped itself at scan {scan_id}")
        self.scan_id = scan_id


@dataclass(frozen=True)
class Epoch:
    """One immutable executed interval and the evidence needed to replay it."""

    reference: EpochRef
    first_scan: int
    last_scan: int
    initial_scan_id: int
    initial_state: SystemState
    tip_state: SystemState
    scan_log: ScanLog
    checkpoints: Mapping[int, SystemState]
    state_cache: tuple[tuple[int, tuple[SystemState, int]], ...]
    rung_firing_timelines: RungFiringTimelines[int]
    committed_tag_timelines: RungFiringTimelines[str]
    node_firing_timelines: RungFiringTimelines[RungId]
    # ``(first, last)`` scan ranges that ran without records, clipped to the
    # interval.  Shared with the epoch's query runner: settling empties it, once
    # for every fork that holds this epoch.
    unrecorded_scans: list[tuple[int, int]]
    synthesis: Any | None
    plant_changes: tuple[tuple[int, list[Any]], ...]
    program: Any | None
    logic: tuple[Any, ...]
    time_mode: TimeMode
    dt: float
    history_retention_scans: int | None
    cache_retention_scans: int | None
    recent_state_cache_budget: int | float
    checkpoint_interval: int
    record_all_tags: bool
    firing_retained_tags: frozenset[str] | None
    known_tags_by_name: Mapping[str, Tag]
    edge_tag_names: frozenset[str]
    constrained_tags: Mapping[str, TagConstraint]
    causal_initial_tags: Mapping[str, Any]
    rtc_base: datetime
    rtc_base_sim_time: float
    keep_forces_on_run: bool

    @classmethod
    def seal(
        cls,
        plc: PLC,
        *,
        reference: EpochRef,
        first_scan: int,
        last_scan: int,
    ) -> Epoch:
        """Detach the executed prefix ``first_scan..last_scan`` from *plc*."""
        if last_scan < first_scan:
            raise ValueError("an epoch must own at least one executed scan")
        tip_state = plc._state_at(last_scan)
        from pyrung.core.synthesis import Synthesis

        synthesis = (
            None
            if plc._synthesis is None
            else Synthesis(
                holds=list(plc._synthesis.holds),
                plant=list(plc._synthesis.plant),
            )
        )
        return cls(
            reference=reference,
            first_scan=first_scan,
            last_scan=last_scan,
            initial_scan_id=plc._initial_scan_id,
            initial_state=plc._initial_state,
            tip_state=tip_state,
            scan_log=plc._scan_log.clone(up_to=last_scan),
            checkpoints=MappingProxyType(
                {
                    scan_id: state
                    for scan_id, state in plc._checkpoints.items()
                    if scan_id <= last_scan
                }
            ),
            state_cache=tuple(
                (scan_id, entry)
                for scan_id, entry in plc._recent_state_cache.items()
                if scan_id <= last_scan
            ),
            rung_firing_timelines=plc._rung_firing_timelines.snapshot(up_to=last_scan),
            committed_tag_timelines=plc._committed_tag_timelines.snapshot(up_to=last_scan),
            node_firing_timelines=plc._node_firing_timelines.snapshot(up_to=last_scan),
            unrecorded_scans=list(
                (max(first, first_scan), min(last, last_scan))
                for first, last in plc._unrecorded_scans
                if first <= last_scan and last >= first_scan
            ),
            synthesis=synthesis,
            plant_changes=tuple(plc._plant_changes),
            program=plc._program,
            logic=tuple(plc._logic),
            time_mode=plc._time_mode,
            dt=plc._dt,
            history_retention_scans=plc._history_retention_scans,
            cache_retention_scans=plc._cache_retention_scans,
            recent_state_cache_budget=plc._recent_state_cache_budget,
            checkpoint_interval=plc._checkpoint_interval,
            record_all_tags=plc._record_all_tags,
            firing_retained_tags=plc._consumed_tags_for_capture(),
            known_tags_by_name=MappingProxyType(dict(plc._known_tags_by_name)),
            edge_tag_names=frozenset(plc._edge_tag_names),
            constrained_tags=MappingProxyType(dict(plc._constrained_tags)),
            causal_initial_tags=MappingProxyType(dict(plc._causal_initial_tags)),
            rtc_base=plc._rtc_base,
            rtc_base_sim_time=plc._rtc_base_sim_time,
            keep_forces_on_run=plc._keep_forces_on_run,
        )


class EpochQuery:
    """Narrow causal/replay query adapter for one :class:`Epoch`.

    Sealed epochs lazily construct a disposable PLC from explicit epoch
    evidence.  That runner is an implementation detail: lineage ownership is
    always this adapter, never the disposable runner.
    """

    def __init__(self, epoch: Epoch, *, live_plc: PLC | None = None) -> None:
        self.epoch = epoch
        self._live_plc = live_plc
        self._query_plc: PLC | None = None
        self.cause_memo: dict[Any, Any] = {}

    def _runner(self) -> PLC:
        if self._live_plc is not None:
            return self._live_plc
        if self._query_plc is not None:
            return self._query_plc

        epoch = self.epoch
        runner = PLC(
            logic=epoch.program if epoch.program is not None else list(epoch.logic),
            initial_state=epoch.initial_state,
            history=epoch.history_retention_scans,
            cache=epoch.cache_retention_scans,
            history_budget=epoch.recent_state_cache_budget,
            checkpoint_interval=epoch.checkpoint_interval,
            record_all_tags=epoch.record_all_tags,
            keep_forces_on_run=epoch.keep_forces_on_run,
            _tag_index=(
                dict(epoch.known_tags_by_name),
                epoch.edge_tag_names,
                dict(epoch.constrained_tags),
            ),
        )
        runner._time_mode = epoch.time_mode
        runner._dt = epoch.dt
        runner._state = epoch.tip_state
        runner._initial_scan_id = epoch.initial_scan_id
        runner._initial_state = epoch.initial_state
        runner._playhead = epoch.last_scan
        runner._scan_log = epoch.scan_log.clone(up_to=epoch.last_scan)
        runner._checkpoints = dict(epoch.checkpoints)
        runner._plant_changes = list(epoch.plant_changes)
        runner._recent_state_cache = OrderedDict(epoch.state_cache)
        runner._recent_state_cache_bytes = sum(
            estimate for _state, estimate in runner._recent_state_cache.values()
        )
        runner._rung_firing_timelines = epoch.rung_firing_timelines
        runner._committed_tag_timelines = epoch.committed_tag_timelines
        runner._node_firing_timelines = epoch.node_firing_timelines
        runner._unrecorded_scans = epoch.unrecorded_scans
        runner._causal_initial_tags = dict(epoch.causal_initial_tags)
        runner._causal_lineage = CausalLineage(runner)
        runner._set_rtc_internal(epoch.rtc_base, epoch.rtc_base_sim_time)
        if epoch.synthesis is not None:
            from pyrung.core.synthesis import Synthesis

            runner._synthesis = Synthesis(
                holds=list(epoch.synthesis.holds),
                plant=list(epoch.synthesis.plant),
            )
        self._query_plc = runner
        return runner

    @property
    def rung_firing_timelines(self) -> RungFiringTimelines[int]:
        return self.epoch.rung_firing_timelines

    @property
    def committed_tag_timelines(self) -> RungFiringTimelines[str]:
        return self.epoch.committed_tag_timelines

    @property
    def node_firing_timelines(self) -> RungFiringTimelines[RungId]:
        return self.epoch.node_firing_timelines

    @property
    def firing_retained_tags(self) -> frozenset[str] | None:
        """Exact recording-time firing retention policy for this epoch."""
        return self.epoch.firing_retained_tags

    def state_at(self, scan_id: int) -> SystemState:
        return self._runner()._state_at(scan_id)

    def state_in_cache(self, scan_id: int) -> bool:
        return self._runner()._state_in_cache(scan_id)

    def history_range(self, start_scan_id: int, end_scan_id: int) -> list[SystemState]:
        return self._runner()._local_history_range(start_scan_id, end_scan_id)

    def latest_firing(self, rung_ids: frozenset[RungId], scan_id: int) -> int | None:
        return self._runner()._local_node_latest_firing_at_or_before(rung_ids, scan_id)

    def latest_value_transition(
        self,
        rung_ids: frozenset[RungId],
        tag_name: str,
        value: Any,
        scan_id: int,
    ) -> int | None:
        return self._runner()._local_node_latest_value_transition_at_or_before(
            rung_ids,
            tag_name,
            value,
            scan_id,
        )

    def resolve_node_rung(self, rung_id: RungId, scan_id: int) -> Rung | None:
        return self._runner()._local_resolve_node_rung(rung_id, scan_id)

    def replay_capture_at(self, scan_id: int) -> ConditionViewCapture | None:
        return self._runner()._replay_capture_at(scan_id)

    def replay_capture_uncached_at(self, scan_id: int) -> ConditionViewCapture | None:
        """Reconstruct one disposable capture without retaining it in the raw LRU."""
        return self._runner()._build_replay_capture_at(scan_id)

    def rung_write_projection_at(self, scan_id: int) -> Any:
        """Replay the exact tag-only read/write projection for this epoch."""

        return self._runner()._replay_rung_write_projection_at(scan_id)

    def pilot_rung_write_projection_at(self, scan_id: int) -> Any:
        """Replay Pilot's exact tag-and-memory access projection for this epoch."""

        return self._runner()._replay_pilot_rung_write_projection_at(scan_id)

    def rtc_at(self, state: SystemState) -> datetime:
        runner = self._runner()
        return runner._replay_rtc_at_state(state, runner._scan_log)

    def cause(
        self,
        lineage: CausalLineage,
        tag: Tag | str,
        *,
        scan: int,
        since: int | None = None,
    ) -> CausalChain | None:
        """Explain *scan* through the caller's complete epoch lineage."""
        return lineage.cause(tag, scan=scan, since=since)


class CausalLineage:
    """A retained epoch chain addressed as one continuous timeline.

    A child starts executing after its ``initial_scan_id``; the boundary
    itself belongs to the frozen parent.  Epoch-aware queries use this object
    so ownership, interval clipping, and newest-result clamping share that one
    rule.
    """

    def __init__(
        self,
        plc: PLC,
        sealed_epoch_queries: tuple[tuple[Epoch, EpochQuery], ...] = (),
    ) -> None:
        self._plc = plc
        self._sealed_epochs = tuple(epoch for epoch, _query in sealed_epoch_queries)
        self._sealed_queries = tuple(query for _epoch, query in sealed_epoch_queries)
        self._detached_epoch_queries = list(sealed_epoch_queries)
        self._live_query: EpochQuery | None = None
        self._current_epoch_cache: Epoch | None = None
        self._current_epoch_key: tuple[int, int, int] | None = None
        self._current_epoch_ref = _new_epoch_ref()

    @property
    def sealed_epochs(self) -> tuple[Epoch, ...]:
        return self._sealed_epochs

    @property
    def oldest_scan_id(self) -> int:
        if self._sealed_epochs:
            return self._sealed_epochs[0].first_scan
        return self._plc._initial_scan_id

    def current_epoch(self) -> Epoch | None:
        """Return the live runner's current executed interval as an Epoch view."""
        first_scan = (
            self._plc._initial_scan_id + 1 if self._sealed_epochs else self._plc._initial_scan_id
        )
        if first_scan > self._plc._state.scan_id:
            return None
        key = (first_scan, self._plc._state.scan_id, id(self._plc._state))
        cached = self._current_epoch_cache
        if cached is not None and self._current_epoch_key == key:
            return cached
        current = Epoch.seal(
            self._plc,
            reference=self._current_epoch_ref,
            first_scan=first_scan,
            last_scan=self._plc._state.scan_id,
        )
        self._current_epoch_cache = current
        self._current_epoch_key = key
        return current

    def invalidate_current_epoch(self) -> None:
        """Discard the live Epoch view after an in-place overlay change."""
        self._current_epoch_cache = None
        self._current_epoch_key = None
        self._live_query = None

    def _epochs(self) -> tuple[Epoch, ...]:
        current = self.current_epoch()
        return self._sealed_epochs + ((current,) if current is not None else ())

    def _query_for(self, epoch: Epoch) -> EpochQuery:
        for sealed, query in zip(self._sealed_epochs, self._sealed_queries, strict=True):
            if sealed is epoch:
                return query
        if self._live_query is None:
            self._live_query = EpochQuery(epoch, live_plc=self._plc)
        else:
            self._live_query.epoch = epoch
        return self._live_query

    def _detached_query_for(self, epoch: Epoch) -> EpochQuery:
        """Return the stable detached query identity paired with *epoch*."""
        for known_epoch, query in self._detached_epoch_queries:
            if known_epoch is epoch:
                return query
        query = EpochQuery(epoch)
        self._detached_epoch_queries.append((epoch, query))
        return query

    def cause(
        self,
        tag: Tag | str,
        *,
        scan: int,
        since: int | None = None,
    ) -> CausalChain | None:
        """Run a recorded query against this lineage's complete history."""
        return self._plc.cause(tag, scan=scan, since=since)

    def owner_at(self, scan_id: int) -> EpochQuery | None:
        """Return the epoch that executed *scan_id*, if it is retained."""
        if not isinstance(scan_id, int):
            return None
        epoch = next(
            (epoch for epoch, _first, _last in self.epochs_covering(scan_id, scan_id)),
            None,
        )
        return self._query_for(epoch) if epoch is not None else None

    def epochs_covering(self, lo: int, hi: int) -> Iterator[tuple[Epoch, int, int]]:
        """Yield epochs overlapping inclusive ``[lo, hi]``, already clipped."""
        if hi < lo:
            return
        for epoch in self._epochs():
            clipped_first = max(lo, epoch.first_scan)
            clipped_last = min(hi, epoch.last_scan)
            if clipped_first <= clipped_last:
                yield epoch, clipped_first, clipped_last

    def newest(
        self,
        query: Callable[[Epoch, int], _T | None],
        before: int,
        *,
        scan_of: Callable[[_T], int],
    ) -> _T | None:
        """Ask each epoch its clamped question and return the newest result.

        ``before`` is inclusive.  The query receives the latest scan owned by
        that epoch which is not after ``before``.  Results outside the epoch's
        clipped interval are ignored, preventing an epoch-local index from
        leaking an ancestor or frozen future.
        """
        intervals = tuple(self.epochs_covering(self.oldest_scan_id, before))
        for epoch, first_scan, last_scan in reversed(intervals):
            candidate = query(epoch, last_scan)
            if candidate is not None and first_scan <= scan_of(candidate) <= last_scan:
                return candidate
        return None

    def seal_through(self, scan_id: int) -> tuple[tuple[Epoch, EpochQuery], ...]:
        """Return detached epoch/query pairs for the prefix through *scan_id*."""
        sealed: list[tuple[Epoch, EpochQuery]] = []
        for epoch in self._epochs():
            if scan_id < epoch.first_scan:
                break
            if scan_id >= epoch.last_scan:
                sealed.append((epoch, self._detached_query_for(epoch)))
                continue
            owner = self._query_for(epoch)
            sliced = Epoch.seal(
                owner._runner(),
                reference=epoch.reference,
                first_scan=epoch.first_scan,
                last_scan=scan_id,
            )
            sealed.append((sliced, EpochQuery(sliced)))
            break
        return tuple(sealed)

    def trim_before(self, scan_id: int) -> None:
        """Discard sealed evidence before *scan_id*, slicing its owner."""
        retained: list[tuple[Epoch, EpochQuery]] = []
        for epoch, query in zip(self._sealed_epochs, self._sealed_queries, strict=True):
            if epoch.last_scan < scan_id:
                continue
            if epoch.first_scan < scan_id:
                epoch = Epoch.seal(
                    query._runner(),
                    reference=epoch.reference,
                    first_scan=scan_id,
                    last_scan=epoch.last_scan,
                )
                query = EpochQuery(epoch)
            retained.append((epoch, query))
        self._sealed_epochs = tuple(epoch for epoch, _query in retained)
        self._sealed_queries = tuple(query for _epoch, query in retained)
        self._detached_epoch_queries = retained


class _CausalRungFiringTimelines(RungFiringTimelines[Any]):
    """Compressed firing lookup across immutable overlay epochs."""

    def __init__(self, plc: PLC) -> None:
        self._plc = plc

    def observed_writers_of(self, tag_name: str) -> frozenset[Any]:
        writers: set[Any] = set()
        lineage = self._plc._causal_lineage
        for epoch, first_scan, last_scan in lineage.epochs_covering(
            lineage.oldest_scan_id,
            self._plc._state.scan_id,
        ):
            timelines = epoch.rung_firing_timelines
            writers.update(timelines.observed_writers_of_between(tag_name, first_scan, last_scan))
        return frozenset(writers)

    def tag_transition_candidate_scans_before(
        self,
        writer_indices: frozenset[Any],
        tag_name: str,
        before_scan_id: int,
    ) -> tuple[int, ...]:
        candidates: set[int] = set()
        lineage = self._plc._causal_lineage
        for epoch, first_scan, last_scan in lineage.epochs_covering(
            lineage.oldest_scan_id,
            before_scan_id - 1,
        ):
            candidates.update(
                scan_id
                for scan_id in epoch.rung_firing_timelines.tag_transition_candidate_scans_before(
                    writer_indices,
                    tag_name,
                    last_scan + 1,
                )
                if first_scan <= scan_id <= last_scan
            )
        return tuple(sorted(candidates, reverse=True))

    def last_tag_write_before(
        self,
        writer_indices: frozenset[Any],
        tag_name: str,
        before_scan_id: int,
    ) -> tuple[int, Any] | None:
        return self._plc._causal_lineage.newest(
            lambda epoch, at_or_before: epoch.rung_firing_timelines.last_tag_write_before(
                writer_indices,
                tag_name,
                at_or_before + 1,
            ),
            before_scan_id - 1,
            scan_of=lambda candidate: candidate[0],
        )

    def rung_writes_at(self, rung_index: Any, scan_id: int) -> PMap | None:
        owner = self._plc._causal_lineage.owner_at(scan_id)
        return (
            owner.rung_firing_timelines.rung_writes_at(rung_index, scan_id)
            if owner is not None
            else None
        )


class PLC:
    """Generator-driven PLC execution engine.

    Executes PLC logic as pure functions: Logic(state) -> new_state.
    The consumer controls execution via step(), enabling:
    - Input injection via patch()
    - Inspection of retained historical state via runner.history
    - Pause/resume at any scan boundary

    Attributes:
        current_state: The current SystemState snapshot.
        history: Query interface for retained SystemState snapshots.
        simulation_time: Current simulation clock (seconds).
        dt: Configured scan period in seconds.
        time_mode: Current time mode (FIXED_STEP or REALTIME).
    """

    def __init__(
        self,
        logic: list[Any] | Any = None,
        initial_state: SystemState | None = None,
        *,
        dt: float | None = None,
        realtime: bool = False,
        history: str | int | None = None,
        cache: str | int | None = None,
        history_budget: int | float | None = None,
        checkpoint_interval: int | None = None,
        record_all_tags: bool = False,
        keep_forces_on_run: bool = False,
        _tag_index: tuple[dict[str, Tag], frozenset[str], dict[str, TagConstraint]] | None = None,
    ) -> None:
        """Create a new PLC.

        Args:
            logic: Program, list of rungs, or None for empty logic.
            initial_state: Starting state. Defaults to SystemState().
            dt: Time delta per scan in seconds (default 0.010).
                Only used in fixed-step mode.
            realtime: Use wall-clock timing instead of fixed step.
                Mutually exclusive with dt.
            history: Retention window for the scan log, checkpoints,
                and firing timelines.  Duration string (``"1h"``,
                ``"30m"``), scan count (int), or ``None`` (unlimited).
            cache: Instant-lookup window for full ``SystemState``
                snapshots.  Same formats as *history*.  ``None`` uses
                byte-budget-only eviction.
            history_budget: Byte ceiling for the recent-state cache.
                Defaults to 100 MB.  Raises ``ValueError`` below 1 MB.
            checkpoint_interval: Number of scans between replay checkpoints.
                Defaults to ``_CHECKPOINT_INTERVAL_DEFAULT``.
            record_all_tags: Bypass the PDG-based rung-firing capture
                filter.  By default the firing log drops writes to tags
                that no rung reads, since the simulator's analysis APIs
                don't need them.  Set this to True when a diagnostic
                session needs the unfiltered firing history (e.g. when
                the PDG is suspected of misclassifying a consumer).
            keep_forces_on_run: Keep active forces across STOP -> RUN
                transitions (see :attr:`keep_forces_on_run`).
            _tag_index: Internal (``fork()``).  A ``(known_tags_by_name,
                edge_tag_names, constrained_tags)`` triple from a runner
                sharing the same program object.  The dicts are copied
                (both sides keep registering privately); the AST walk
                and constraint-index build that would recreate them —
                the dominant fork cost on large programs — are skipped.
                Contract: the caller must pass an ``initial_state`` that
                already contains a value for every known tag (``fork()``
                pre-seeds defaults), because the default-seeding scan is
                also skipped.
        """
        if realtime and dt is not None:
            raise ValueError("Cannot specify dt= with realtime=True")
        if dt is None:
            dt = 0.010
        if checkpoint_interval is None:
            checkpoint_interval = _CHECKPOINT_INTERVAL_DEFAULT
        if checkpoint_interval < 1:
            raise ValueError("checkpoint_interval must be >= 1")
        if history_budget is None:
            history_budget = _HISTORY_BUDGET_BYTES_DEFAULT
        if history_budget < 1_048_576:
            raise ValueError("history_budget must be >= 1 MB (1048576)")

        history_scans = _parse_retention(history, dt)
        cache_scans = _parse_retention(cache, dt)
        history_floor = checkpoint_interval * 2
        if history_scans is not None:
            history_scans = max(history_scans, history_floor)
        if cache_scans is not None:
            cache_scans = max(cache_scans, _RECENT_STATE_CACHE_MIN_ENTRIES)
            if history_scans is not None:
                cache_scans = min(cache_scans, history_scans)
        self._history_retention_scans: int | None = history_scans
        self._cache_retention_scans: int | None = cache_scans

        self._logic: list[Rung]
        self._program: Any = None
        # Handle different logic types
        # Import Program here to avoid circular import at module level
        from pyrung.core.program import Program

        if logic is None:
            self._logic = []
        elif isinstance(logic, Program):
            self._logic = logic.rungs
            self._program = logic
        elif isinstance(logic, list):
            self._logic = logic
        else:
            self._logic = [logic]
        self._site_table = self._bind_logic_sites()

        self._state = initial_state if initial_state is not None else SystemState()
        if _tag_index is None:
            # A state saved before edge memory was per contact (``_prev:X``).
            # Forks start from this runner's own (already migrated) states.
            self._state = migrate_legacy_edge_memory(self._state, self._site_table)
        # Mutable read mirror for ordinary interpreted scans.  The committed
        # state remains an immutable PMap; this avoids repeated HAMT bucket
        # walks while a scan reads its unchanged input image.  Capturing/debug
        # scans bypass it because they retain ConditionViews after commit.
        self._state_tags_read_cache: dict[str, Any] | None = None
        self._state_tags_read_cache_state: SystemState | None = None
        self._running = True
        self._battery_present = True
        self._keep_forces_on_run = bool(keep_forces_on_run)
        self._state = self._apply_runtime_memory_flags(
            self._state,
            mode_run=self._running,
            battery_present=self._battery_present,
        )
        # Byte-bounded recent-state cache feeding ``History.at()`` on
        # the hot path.  ``History`` itself is a stateless facade.
        # Keyed by scan_id → (SystemState, estimated_bytes).
        self._recent_state_cache_budget = history_budget
        self._recent_state_cache: OrderedDict[int, tuple[SystemState, int]] = OrderedDict()
        self._recent_state_cache_bytes = 0
        self._cache_state(self._state)
        # The "implicit anchor" for replay walks below the earliest
        # checkpoint.  Pinned at construction and refreshed only on
        # ``_reset_runtime_scope`` (reboot), so it remains valid even
        # after the recent-state window rotates past the initial scan.
        self._initial_scan_id: int = self._state.scan_id
        self._initial_state: SystemState = self._state
        self._history = History(self)
        self._current_rung_traces: dict[int, RungTrace] = {}
        self._current_rung_traces_scan_id: int | None = None
        # Independent compressed columns for rung firing, final attempted
        # values, and sparse within-scope variation.
        self._rung_firing_timelines: RungFiringTimelines[int] = RungFiringTimelines()
        # Authoritative committed tag boundaries. Unlike rung write payloads,
        # these are captured after the complete scan, so same-scan overwrites
        # are already resolved. The range encoder compresses arithmetic churn
        # while sparse tags contribute only when their committed value changes.
        self._committed_tag_timelines: RungFiringTimelines[str] = RungFiringTimelines()
        self._causal_initial_tags = dict(self._state.tags)
        self._causal_rung_firing_timelines = _CausalRungFiringTimelines(self)
        # Node-level firing log, keyed by ``RungId`` (subroutine, rung_index).
        # Holds subroutine-rung firings the main-rung log can't represent;
        # consumed by ``query.cold_rungs`` / ``hot_rungs`` so subroutine rungs
        # are visible to coverage.
        self._node_firing_timelines: RungFiringTimelines[RungId] = RungFiringTimelines()
        self._inflight_scan_id: int | None = None
        self._inflight_rung_events: dict[int, list[RungTraceEvent]] = {}
        self._latest_inflight_trace_event: tuple[int, int, RungTraceEvent] | None = None
        self._latest_committed_trace_event: tuple[int, int, RungTraceEvent] | None = None
        self._playhead = self._state.scan_id
        self._dt = dt
        self._last_step_time: float | None = None
        if realtime:
            self._time_mode = TimeMode.REALTIME
            self._last_step_time = time.perf_counter()
        else:
            self._time_mode = TimeMode.FIXED_STEP
        self._scan_log = ScanLog(time_mode=self._time_mode)
        self._checkpoint_interval = checkpoint_interval
        self._checkpoints: dict[int, SystemState] = {}
        self._forces_last_recorded: dict[str, bool | int | float | str] = {}
        self._this_scan_drained_patches: dict[str, bool | int | float | str] = {}
        # Replay plumbing. ``_dt_override_for_next_scan`` lets
        # ``replay_to`` inject the recorded dt for each replayed scan in
        # REALTIME mode; ``_replay_mode`` suppresses user monitors,
        # breakpoint labels, and the RTC setter during a replay walk.
        self._dt_override_for_next_scan: float | None = None
        # Every ``(first, last)`` range of scans that ran without causal records, in
        # the compiled kernel or its guarded loop.  A question that reaches one settles it.
        self._unrecorded_scans: list[tuple[int, int]] = []
        self._replay_mode: bool = False
        self._compiled_replay_kernel: CompiledKernel | None | bool = None
        # What coasts keep for later coasts: a search's, shared with its forks.
        self._coast_memo: CoastMemo | None = None
        self._search_refuses_restart = False
        self._last_replay_slab_stats: dict[str, int] = {}
        # The synthesis-bracketed compilation unit for soft-exec replay (holds +
        # user + plant), built lazily and cached.  ``None`` ⇒ no synthesis (use
        # the bare program).  Invalidated by the harness when the overlay changes.
        self._soft_exec_program_cache: Any | None = None
        # Replay slabs keyed by checkpoint anchor.  A causal backward walk reads
        # state at scans scattered across a few checkpoint intervals and revisits
        # them; keeping one slab per anchor (LRU-bounded) for the walk's duration
        # turns those revisits into dict hits instead of re-replaying the same
        # interval.  Slabs cache immutable historical states, so only
        # history-changing events (trim / reset / fork) invalidate them.
        self._replay_slabs: dict[int, dict[int, SystemState]] = {}
        # PDG-filtered rung-firing capture.  When the filter is active
        # (``record_all_tags=False``), ``capturing_rung`` drops writes to
        # tags that no rung reads — the firing log is consumed only by
        # ``cause``/``effect``/``query`` which never ask about an unread
        # tag.  Populated lazily alongside ``_pdg_cache`` the first time
        # a scan captures; rebuilt atomically whenever the PDG is
        # invalidated.
        self._record_all_tags: bool = record_all_tags
        self._pdg_consumed_tags: frozenset[str] | None = None
        # One-slot cache for ``replay_trace_at``.  Reconstructing rung
        # traces for a historical scan costs one fork + up to K plain
        # scans + one debug scan; caching a back-to-back repeat query
        # (common when the user hovers/expands a scan in the UI) saves
        # that work.  Cleared on tip advance (``_run_single_scan``) and
        # on any reset that invalidates the log (reboot, stop→run).
        self._cached_replay_trace: tuple[int, dict[int, RungTrace]] | None = None
        # Bounded epoch-local cache for on-demand interpreted replays
        # (``_replay_capture_at``) — the at-fire-time ConditionView per rung plus
        # each rung's data-read footprint for a historical scan, used by recorded
        # ``cause()`` to classify subroutine-writer contacts and to cross opaque
        # data-writers (Crossings Tier 2).  Same lifecycle as
        # ``_cached_replay_trace``.
        self._cached_replay_captures: OrderedDict[int, ConditionViewCapture] = OrderedDict()
        # Immutable executed epochs behind this live runner. The current epoch
        # remains live until a fork seals its executed prefix.
        self._causal_lineage = CausalLineage(self)
        self._epoch_caches = EpochCaches(self)
        self._rtc_base = self._normalize_rtc_datetime(datetime.now())
        self._rtc_base_sim_time = float(self._state.timestamp)
        self._system_runtime = SystemPointRuntime(
            time_mode_getter=lambda: self._time_mode,
            fixed_step_dt_getter=lambda: self._dt,
            rtc_now_getter=self._rtc_at_sim_time,
            rtc_setter=self._set_rtc_and_record,
        )
        self._input_overrides = InputOverrideManager(
            is_read_only=self._system_runtime.is_read_only,
            known_type=self._type_a_name_holds,
        )
        self._tags_rungs_reach = TagsRungsReach(self._logic, self._program)
        # Known only because a patch or a force named them by Tag: a name means no more than
        # it did, so a string's answer doesn't depend on which came first.
        self._names_no_rung_gave: set[str] = set()
        # Preserve direct access used in tests/live-tag helpers.
        self._pending_patches = self._input_overrides.pending_patches
        self._forces = self._input_overrides.forces_mutable
        self._condition_trace = ConditionTraceEngine(formatter=TraceFormatter())
        self._debugger = PLCDebugger(step_factory=ScanStep)
        self._debug_ns = _DebugNamespace(self)
        self._next_debug_handle_id = 1
        self._monitors_by_id: dict[int, _MonitorRegistration] = {}
        self._breakpoints_by_id: dict[int, _BreakpointRegistration] = {}
        self._pause_requested_this_scan = False
        self._active_tokens: list[Token[PLC | None]] = []
        self._harness: Any | None = None
        # Synthesis overlay (soft-exec only): bracketing rungs the runner scans
        # around the user program — ``holds`` pre-scan (input steering),
        # ``plant`` post-scan (feedback).  ``None`` ⇒ no overlay (the common
        # case; deploy/prove never set it).  See ``core/synthesis.py``.
        self._synthesis: Any | None = None
        # ``(scan_id, plant)``: the plant that ran the scans up to *scan_id*, each
        # kept when a coupling change replaced it there (:meth:`_note_plant_change`).
        self._plant_changes: list[tuple[int, list[Any]]] = []
        self._fork_seed_cache: tuple[SystemState, int, SystemState] | None = None
        self._bounds_violations: dict[str, BoundsViolation] = {}
        self._randint_sites_cache: tuple[int, tuple[RandIntExpr, ...]] | None = None
        if _tag_index is not None:
            # fork() pre-seeds defaults into initial_state, so the
            # membership scan below is skipped along with the AST walk
            # and constraint-index build.
            known_tags, edge_names, constrained_tags = _tag_index
            self._known_tags_by_name = dict(known_tags)
            self._edge_tag_names = edge_names
            self._constrained_tags = dict(constrained_tags)
            return
        self._known_tags_by_name = {}
        self._edge_tag_names = self._refresh_known_tags_and_edges()
        self._constrained_tags = build_constraint_index(
            self._tags_rungs_reach.declared_with(self._known_tags_by_name)
        )
        # Seed initial state with tag defaults (skip tags already in state).
        # One pmap iteration + C-level set difference; per-key pmap
        # ``in`` probes cost ~2 bucket walks per known tag.
        missing = self._known_tags_by_name.keys() - set(self._state.tags)
        if missing:
            self._state = self._state.with_tags(
                {name: self._known_tags_by_name[name].default for name in missing}
            )
            self._reset_cache(self._state)
            self._initial_state = self._state
        # Causal endpoint reconstruction uses the fully seeded scan-zero image,
        # not the pre-inventory SystemState created near the top of __init__.
        self._causal_initial_tags = dict(self._state.tags)

    @property
    def program(self) -> Any:
        """The Program object if the PLC was constructed from one, else None."""
        return self._program

    @property
    def state(self) -> SystemState:
        """Current state snapshot."""
        return self._state

    @property
    def current_state(self) -> SystemState:
        return self._state

    @property
    def tags(self) -> MappingProxyType[str, Tag]:
        """Read-only mapping of tag name → Tag object, for what the program has and reaches.

        A slot a range reaches becomes a tag when it is read from here.
        """
        return MappingProxyType(
            TagsKnownOrReached(self._known_tags_by_name, self._tags_rungs_reach)
        )

    @property
    def bounds_violations(self) -> dict[str, BoundsViolation]:
        """Constraint violations from the most recent scan, if any."""
        return self._bounds_violations

    @property
    def history(self) -> History:
        """Read-only history query surface."""
        return self._history

    def _causal_history_contains(self, scan_id: int) -> bool:
        """Whether this runner or its read-only ancestry retains *scan_id*.

        Public :class:`History` spans sealed epochs plus the live current
        interval, with each sealed record already clamped at its fork boundary.
        """
        return (
            isinstance(scan_id, int)
            and self._causal_lineage.oldest_scan_id <= scan_id <= self._state.scan_id
        )

    def _causal_oldest_scan_id(self) -> int:
        """Oldest scan retained anywhere in this runner's causal ancestry."""
        return self._causal_lineage.oldest_scan_id

    def _causal_epoch_intervals(self) -> tuple[tuple[Epoch, int, int], ...]:
        """Compatibility view of executed intervals in the causal lineage."""
        return tuple(
            self._causal_lineage.epochs_covering(
                self._causal_lineage.oldest_scan_id,
                self._state.scan_id,
            )
        )

    def _causal_owner_at(self, scan_id: int) -> EpochQuery | None:
        """Compatibility facade for the centralized lineage owner lookup."""
        return self._causal_lineage.owner_at(scan_id)

    def _causal_state_at(self, scan_id: int) -> SystemState:
        """Read a state from the overlay epoch that executed its scan."""
        owner = self._causal_lineage.owner_at(scan_id)
        if owner is None:
            raise KeyError(scan_id)
        return owner.state_at(scan_id)

    def _timestamp_at(self, scan_id: int) -> float:
        """A scan's timestamp, added up from the nearest state its epoch holds, not stepped to."""
        owner = self._causal_lineage.owner_at(scan_id)
        if owner is None:
            raise KeyError(scan_id)
        plc = owner._runner()
        if scan_id == plc._state.scan_id:
            return plc._state.timestamp
        held = plc._recent_state_cache.get(scan_id)
        if held is not None:
            return held[0].timestamp
        anchor = plc._replay_anchor_scan(scan_id)
        state = plc._checkpoints.get(anchor, plc._initial_state)
        timestamp = state.timestamp
        dts = plc._scan_log._dts
        base = plc._scan_log.base_scan
        for scan in range(state.scan_id + 1, scan_id + 1):
            timestamp += plc._dt if dts is None else dts[scan - base]
        return timestamp

    def _causal_state_in_cache(self, scan_id: int) -> bool:
        owner = self._causal_lineage.owner_at(scan_id)
        return owner.state_in_cache(scan_id) if owner is not None else False

    def _causal_raw_committed_transition_before(
        self,
        tag_name: str,
        before_scan_id: int,
    ) -> tuple[int, Any] | None:
        """Latest authoritative committed change across the epoch lineage."""
        return self._causal_lineage.newest(
            lambda epoch, at_or_before: epoch.committed_tag_timelines.last_tag_write_before(
                frozenset({tag_name}),
                tag_name,
                at_or_before + 1,
            ),
            before_scan_id - 1,
            scan_of=lambda candidate: candidate[0],
        )

    def _causal_committed_transition_at(
        self,
        tag_name: str,
        scan_id: int,
    ) -> Transition | None:
        """Exact post-precedence boundary transition, without state replay."""
        owner = self._causal_lineage.owner_at(scan_id)
        if owner is None:
            return None
        writes = owner.committed_tag_timelines.rung_writes_at(tag_name, scan_id)
        if writes is None or tag_name not in writes:
            return None
        return self._committed_transition(tag_name, scan_id, writes[tag_name])

    def _causal_last_committed_transition_before(
        self,
        tag_name: str,
        before_scan_id: int,
    ) -> Transition | None:
        """Latest exact committed transition before a scan, across epochs."""
        latest = self._causal_raw_committed_transition_before(tag_name, before_scan_id)
        if latest is None:
            return None
        return self._committed_transition(tag_name, latest[0], latest[1])

    def _committed_transition(self, tag_name: str, scan_id: int, after: Any) -> Transition:
        """The two sides of a change, a side no column can say read from its state.

        The column says what the last write it has wrote, which is the old value only if no
        scan since then ran without records: one of those may have moved the tag.
        """
        prior = self._causal_raw_committed_transition_before(tag_name, scan_id)
        before = prior[1] if prior is not None else self._causal_initial_tags.get(tag_name)
        if before is _UNKNOWN_VALUE or self._unrecorded_in(
            None if prior is None else prior[0] + 1, scan_id - 1
        ):
            before = self.history.at(scan_id - 1).tags.get(tag_name)
        if after is _UNKNOWN_VALUE:
            after = self.history.at(scan_id).tags.get(tag_name)
        return Transition(tag_name, scan_id, before, after)

    @property
    def playhead(self) -> int:
        """Current scan id used for inspection/time-travel queries."""
        return self._playhead

    def seek(self, scan_id: int) -> SystemState:
        """Move playhead to a retained scan and return that snapshot."""
        state = self.history.at(scan_id)
        self._playhead = state.scan_id
        return state

    def rewind(self, seconds: float) -> SystemState:
        """Move playhead backward in time by ``seconds`` and return snapshot."""
        if seconds < 0:
            raise ValueError("seconds must be >= 0")

        playhead_state = self.history.at(self._playhead)
        target_timestamp = playhead_state.timestamp - seconds

        target_state = self.history.at_or_before_timestamp(target_timestamp)
        if target_state is None:
            target_state = self.history.at(self.history.oldest_scan_id)

        self._playhead = target_state.scan_id
        return target_state

    def rung_firings(self, scan_id: int | None = None) -> PMap:
        """Return rung firings for the given scan (default: playhead).

        Returns ``PMap[int, PMap[str, Any]]`` mapping each rung index
        that fired (had any write, even if all were filtered by PDG)
        during the scan to the filtered ``{tag_name: value_written}``
        map.  Synthesized on demand from per-rung range-encoded
        timelines (:class:`RungFiringTimelines`); rungs with no
        timeline covering ``scan_id`` contribute nothing.

        Populated uniformly by both the non-debug (``step()`` /
        ``run()``) and debug (DAP ``pyrungStepScan`` / continue) scan
        paths via ``ScanContext.capturing_rung``.

        Values are attempted writes, including a later writer reasserting the
        already-pending value. Transition consumers compare committed state;
        causal execution consumers retain the exact writer occurrence.

        A column of values too various to keep, or that a loop's record cannot speak for,
        is read by stepping the scan again from the nearest checkpoint, up to
        ``UnrecordedScansError`` where that is too far.
        """
        target = self._playhead if scan_id is None else scan_id
        firings = self._recorded_rung_firings(target)
        if any(value is _UNKNOWN_VALUE for writes in firings.values() for value in writes.values()):
            owner = self._causal_lineage.owner_at(target)
            assert owner is not None
            return owner._runner()._rung_firings_stepped_again(
                target, self._search_limits(), self._refusal
            )
        return firings

    def _recorded_rung_firings(self, target: int) -> PMap:
        """What the timelines say of *target*, a value they gave up on as ``_UNKNOWN_VALUE``."""
        self._settle_compiled_ranges(target, target)
        owner = self._causal_lineage.owner_at(target)
        return owner.rung_firing_timelines.at(target) if owner is not None else pmap()

    def _refusal(self, first: int, last: int, scans: int) -> BaseException:
        """What a question gets when reading *first*..*last* would step too far."""
        refusal = UnrecordedScansError(first, last, scans)
        return SearchQuestionRefused(refusal) if self._search_refuses_restart else refusal

    def _rung_firings_stepped_again(
        self,
        target: int,
        check_limits: Callable[[], None] | None,
        refusal: Callable[[int, int, int], BaseException],
    ) -> PMap:
        steps = target - self._replay_anchor_scan(target - 1)
        if steps > SETTLE_SCAN_LIMIT:
            raise refusal(target, target, steps)
        anchor = self._nearest_checkpoint_at_or_before(target - 1)
        replay, log, anchor_scan_id, lifecycle_by_scan = self._build_replay_fork(anchor)
        for scan_id in range(anchor_scan_id + 1, target + 1):
            if check_limits is not None:
                check_limits()
            self._apply_log_entries_for_scan(replay, scan_id, log, lifecycle_by_scan)
            if scan_id == target:
                # One scan's values, which the run-up cannot have made too various to keep.
                replay._rung_firing_timelines = RungFiringTimelines()
            replay.step()
        return replay._rung_firing_timelines.at(target)

    def _node_firings_at(self, scan_id: int | None = None) -> PMap:
        """Return node-level subroutine/synthesis firings for the given scan.

        Returns ``PMap[RungId, PMap[str, Any]]`` keyed by
        ``RungId(namespace, rung_index)``.  User subroutine nodes and the
        synthetic ``plant`` / ``PILOT`` namespaces live here; main rungs remain
        exclusively in :meth:`rung_firings`.
        """
        target = self._playhead if scan_id is None else scan_id
        self._settle_compiled_ranges(target, target)
        owner = self._causal_lineage.owner_at(target)
        return owner.node_firing_timelines.at(target) if owner is not None else pmap()

    def _node_latest_firing_at_or_before(
        self,
        rung_ids: frozenset[RungId],
        scan_id: int,
    ) -> int | None:
        """Latest retained execution of any selected node at/before a scan."""
        return self._causal_lineage.newest(
            lambda epoch, at_or_before: self._causal_lineage._query_for(epoch).latest_firing(
                rung_ids, at_or_before
            ),
            scan_id,
            scan_of=int,
        )

    def _local_node_latest_firing_at_or_before(
        self,
        rung_ids: frozenset[RungId],
        scan_id: int,
    ) -> int | None:
        """Latest selected-node execution in this epoch's local indexes."""
        main = frozenset(rung.rung_index for rung in rung_ids if rung.subroutine is None)
        nested = frozenset(rung for rung in rung_ids if rung.subroutine is not None)
        candidates = [
            self._rung_firing_timelines.latest_firing_scan_at_or_before(main, scan_id),
            self._node_firing_timelines.latest_firing_scan_at_or_before(nested, scan_id),
        ]
        return max((candidate for candidate in candidates if candidate is not None), default=None)

    def _node_latest_value_transition_at_or_before(
        self,
        rung_ids: frozenset[RungId],
        tag_name: str,
        value: Any,
        scan_id: int,
    ) -> int | None:
        """Latest retained selected-node range that may establish a value."""
        return self._causal_lineage.newest(
            lambda epoch, at_or_before: self._causal_lineage._query_for(
                epoch
            ).latest_value_transition(rung_ids, tag_name, value, at_or_before),
            scan_id,
            scan_of=int,
        )

    def _local_node_latest_value_transition_at_or_before(
        self,
        rung_ids: frozenset[RungId],
        tag_name: str,
        value: Any,
        scan_id: int,
    ) -> int | None:
        """Latest selected-node value candidate in this epoch's indexes."""
        main = frozenset(rung.rung_index for rung in rung_ids if rung.subroutine is None)
        nested = frozenset(rung for rung in rung_ids if rung.subroutine is not None)
        retained = self._consumed_tags_for_capture()
        missing_is_unknown = retained is not None and tag_name not in retained
        candidates = [
            self._rung_firing_timelines.latest_value_transition_scan_at_or_before(
                main,
                tag_name,
                value,
                scan_id,
                missing_is_unknown=missing_is_unknown,
            ),
            self._node_firing_timelines.latest_value_transition_scan_at_or_before(
                nested,
                tag_name,
                value,
                scan_id,
                missing_is_unknown=missing_is_unknown,
            ),
        ]
        return max((candidate for candidate in candidates if candidate is not None), default=None)

    def _resolve_node_rung(self, rung_id: RungId, scan_id: int) -> Rung | None:
        """Resolve a recorded node against the world that executed *scan_id*."""
        owner = self._causal_lineage.owner_at(scan_id)
        return owner.resolve_node_rung(rung_id, scan_id) if owner is not None else None

    def _local_resolve_node_rung(self, rung_id: RungId, scan_id: int) -> Rung | None:
        """Resolve a node against this live epoch's executable overlay."""
        from pyrung.core.synthesis import (
            PILOT_RUNG_NAMESPACE,
            PLANT_RUNG_NAMESPACE,
        )

        if self._synthesis is not None:
            if rung_id.subroutine == PLANT_RUNG_NAMESPACE:
                rungs = self._synthesis.plant
                return rungs[rung_id.rung_index] if rung_id.rung_index < len(rungs) else None
            if rung_id.subroutine == PILOT_RUNG_NAMESPACE:
                rungs = self._synthesis.holds
                return rungs[rung_id.rung_index] if rung_id.rung_index < len(rungs) else None
        if rung_id.subroutine is None:
            rungs = self._program.rungs if self._program is not None else self._logic
            return rungs[rung_id.rung_index] if rung_id.rung_index < len(rungs) else None
        if self._program is None:
            return None
        rungs = self._program.subroutines.get(rung_id.subroutine)
        if rungs is None or rung_id.rung_index >= len(rungs):
            return None
        return rungs[rung_id.rung_index]

    def diff(self, scan_a: int, scan_b: int) -> dict[str, tuple[Any, Any]]:
        """Return changed tag values between two retained historical scans."""
        state_a = self.history.at(scan_a)
        state_b = self.history.at(scan_b)

        changed: dict[str, tuple[Any, Any]] = {}
        all_keys = sorted(set(state_a.tags.keys()) | set(state_b.tags.keys()))
        for key in all_keys:
            old_value = state_a.tags.get(key)
            new_value = state_b.tags.get(key)
            if old_value != new_value:
                changed[key] = (old_value, new_value)
        return changed

    def _ensure_pdg(self) -> Any:
        """Lazily build and cache the static program dependency graph.

        Also populates ``_pdg_consumed_tags`` — every tag any rung
        reads, combining condition-reads and data-reads via
        ``readers_of`` — unioned with every Bool-typed tag the PDG
        knows about.  The capture-layer filter keeps all Bools regardless
        of read/write role: they're low-cardinality and usually the
        tags users ask ``cause()`` about, so the direct-log path stays
        cheap for them.  Non-Bool churn (Timer.acc et al.) still gets
        filtered.  Any future site that invalidates ``_pdg_cache`` must
        clear ``_pdg_consumed_tags`` together — the two caches share a
        lifetime.
        """
        if not hasattr(self, "_pdg_cache") or self._pdg_cache is None:
            from pyrung.core.analysis.pdg import build_program_graph
            from pyrung.core.program import Program
            from pyrung.core.tag import TagType

            program = self._program
            if program is None:
                program = Program.__new__(Program)
                program.rungs = list(self._logic)
                program.subroutines = {}
            self._pdg_cache = build_program_graph(program)
            consumed = set(self._pdg_cache.readers_of.keys())
            # Union in every Bool-typed tag the PDG observed. A mixed rung
            # (e.g. ``out(BoolFlag) + Timer.acc``) keeps the Bool write while
            # the unused accumulator drops, preserving the Bool's exact
            # causal column indefinitely.
            for name, tag in self._pdg_cache.tags.items():
                if getattr(tag, "type", None) == TagType.BOOL:
                    consumed.add(name)
            self._pdg_consumed_tags = frozenset(consumed)
        return self._pdg_cache

    def _consumed_tags_for_capture(self) -> frozenset[str] | None:
        """Capture-worthy tag set for ``ScanContext.capturing_rung``
        to filter against, or ``None`` when the filter should be
        bypassed.

        The set is consumed-tags (per PDG ``readers_of``) unioned with
        every Bool-typed tag — see :meth:`_ensure_pdg`.  ``None``
        bypasses the filter entirely — used for the
        ``record_all_tags=True`` escape hatch, for logic-less PLCs (no
        rung = no consumer, filter would silently drop every write),
        and for programs with rungs the PDG cannot model (synthetic
        test rungs that only implement ``evaluate(ctx)``).  Otherwise
        the PDG is built lazily on first call; every subsequent
        invocation is a single attribute read.
        """
        if self._record_all_tags or not self._logic:
            return None
        if self._pdg_consumed_tags is None:
            from pyrung.core.rung import Rung as RungClass

            if not all(isinstance(rung, RungClass) for rung in self._logic):
                return None
            self._ensure_pdg()
        return self._pdg_consumed_tags

    def cause(
        self,
        tag: Tag | str,
        scan: int | None = None,
        *,
        to: Any = _SENTINEL,
        assume: dict[str, Any] | None = None,
        deep: bool = True,
        since: int | None = None,
    ) -> CausalChain | None:
        """Explain what caused a tag to transition.

        **Recorded** (default, ``to`` omitted): walks recorded history
        backward from the transition. Returns ``None`` if no transition
        was found. With ``deep=True``, each fired rung's steady enablers
        are recursively explained through their establishing transitions
        or an observed writer of their current value. Non-firing static
        writers are not swept into the result. Terminals are classified in
        :attr:`~pyrung.core.analysis.causal.CausalChain.roots`.

        **Projected** (``to=value``): projects forward from the current
        state, finding reachable paths that would drive the tag to *value*.
        Returns a ``CausalChain`` with ``mode='projected'`` (reachable) or
        ``mode='unreachable'`` (stranded, with ``blockers``).  Never
        returns ``None`` in projected mode.

        Args:
            tag: Tag object or tag name string.
            scan: Specific scan to examine (recorded mode only).
            to: Target value for projected mode.  When provided, the
                method returns the path that would drive *tag* to this
                value from the current state.
            assume: Tag-to-value overrides for projected mode.  Pins
                the given tags to specified values during analysis.
                Raises ``ValueError`` if used without ``to=`` or if
                any key is a ``readonly`` tag.
            deep: Recorded mode only. ``False`` restores the shallow
                trigger-only walk (no recursive enabler explanation).
            since: Recorded mode only. Inclusive earliest transition scan for
                an incident-local explanation. The state immediately before
                this scan is boundary context, not a cause to chase. Omit it
                to traverse the runner's complete inherited epoch lineage.

        Returns:
            A :class:`~pyrung.core.analysis.causal.CausalChain`, or ``None``
            (recorded mode only, when no transition was found).
        """
        if assume and to is _SENTINEL:
            raise ValueError("assume= requires projected mode (provide to=)")
        if since is not None and to is not _SENTINEL:
            raise ValueError("since= is only supported in recorded mode")
        if since is not None and not isinstance(since, int):
            raise TypeError("since must be an int or None")
        if since is not None and scan is not None and since > scan:
            return None

        if to is not _SENTINEL:
            if assume:
                _validate_assume(self._logic, assume)
            from pyrung.core.analysis.causal import projected_cause

            return projected_cause(
                logic=self._logic,
                history=self._history,
                tag=tag,
                to_value=to,
                pdg=self._ensure_pdg(),
                assume=assume,
                timelines=self._rung_firing_timelines,
                program=self._program,
            )

        self._settle_compiled_ranges(since, scan)
        from pyrung.core.analysis.causal import recorded_cause

        cause_history = (
            self._history._causal_window(since, scan) if since is not None else self._history
        )

        return recorded_cause(
            logic=self._logic,
            history=cast(History, cause_history),
            rung_firings_fn=self._recorded_rung_firings,
            tag=tag,
            scan_id=scan,
            pdg=self._ensure_pdg() if self._logic else None,
            timelines=self._causal_rung_firing_timelines,
            state_in_cache_fn=self._causal_state_in_cache,
            program=self._program,
            scan_log=self._scan_log,
            # A bounded incident accepts its entrance snapshot as established
            # context.  Seeding input attribution from the lineage's cold
            # origin would both violate that contract and rematerialize an
            # unrelated ancient slab before the local walk even begins.
            initial_tags=cause_history.at(cause_history.oldest_scan_id).tags,
            node_firings_fn=self._node_firings_at,
            node_previous_firing_fn=self._node_latest_value_transition_at_or_before,
            node_rung_fn=self._resolve_node_rung,
            node_views_fn=self._replay_node_views_at,
            node_runs_fn=self._replay_rung_runs_at,
            rung_write_projection_fn=self._replay_rung_write_projection_at,
            node_reads_fn=self._replay_node_reads_at,
            deep=deep,
        )

    def effect(
        self,
        tag: Tag | str,
        scan: int | None = None,
        *,
        from_: Any = _SENTINEL,
        to_value: Any = _SENTINEL,
        assume: dict[str, Any] | None = None,
        steady_state_k: int = 3,
        max_scans: int = 1000,
    ) -> CausalChain | None:
        """Trace the downstream effects of a tag transition.

        **Recorded** (default, ``from_`` omitted): walks recorded
        history forward from an actual transition.  Returns ``None`` if
        no transition was found.

        **Projected** (``from_=value``): what-if analysis — if the tag
        transitioned from *value* right now, what downstream effects would
        follow?  Returns ``mode='projected'`` (possibly empty steps for
        dead-end) or ``mode='unreachable'`` if the trigger can't fire.
        Never returns ``None`` in projected mode.

        Args:
            tag: Tag object or tag name string.
            scan: Specific scan of the transition (recorded mode only).
            from_: Current value for projected what-if analysis.  For
                Bool tags the TO value is inferred as ``not from_``.
            assume: Tag-to-value overrides for projected mode.  Pins
                the given tags to specified values during analysis.
                Raises ``ValueError`` if used without ``from_=`` or if
                any key is a ``readonly`` tag.
            steady_state_k: Stop after this many consecutive scans with no
                new effects (recorded mode only, default 3).
            max_scans: Hard cap on forward scans (recorded mode only,
                default 1000).

        Returns:
            A :class:`~pyrung.core.analysis.causal.CausalChain`, or ``None``
            (recorded mode only, when no transition was found).
        """
        if assume and from_ is _SENTINEL:
            raise ValueError("assume= requires projected mode (provide from_=)")

        if from_ is not _SENTINEL:
            if assume:
                _validate_assume(self._logic, assume)
            from pyrung.core.analysis.causal import projected_effect

            kwargs: dict[str, Any] = {}
            if to_value is not _SENTINEL:
                kwargs["to_value"] = to_value

            return projected_effect(
                logic=self._logic,
                history=self._history,
                tag=tag,
                from_value=from_,
                pdg=self._ensure_pdg(),
                assume=assume,
                program=self._program,
                **kwargs,
            )

        self._settle_compiled_ranges(scan, None)
        from pyrung.core.analysis.causal import recorded_effect

        return recorded_effect(
            logic=self._logic,
            history=self._history,
            rung_firings_fn=self._recorded_rung_firings,
            tag=tag,
            scan_id=scan,
            steady_state_k=steady_state_k,
            max_scans=max_scans,
            pdg=self._ensure_pdg() if self._logic else None,
            timelines=self._causal_rung_firing_timelines,
            program=self._program,
            node_runs_fn=self._replay_rung_runs_at,
        )

    def _settle_compiled_ranges(
        self, first: int | None = None, last: int | None = None, *, at_any_cost: bool = False
    ) -> None:
        """Give the scans in *first*..*last* that ran without records their records.

        A plain compiled run commits states and no index, which is its whole speed.  The
        first question that reaches such a scan pays for it, by stepping it again from the
        nearest checkpoint: the answer is then what a stepped run recorded, because it is
        one.  An epoch's ranges settle once, for every fork that holds it.
        """
        asked = self._unrecorded_in(first, last)
        if not asked:
            return
        # What it costs is the steps from the checkpoint, and a long stretch keeps few of them.
        scans = sum(end - owner._replay_anchor_scan(start - 1) for owner, (start, end) in asked)
        if scans > SETTLE_SCAN_LIMIT and not at_any_cost:
            raise self._refusal(asked[0][1][0], asked[-1][1][1], scans)
        for owner, (start, end) in asked:
            owner._settle_window(start, end, self._search_limits())

    def _unrecorded_in(
        self, first: int | None = None, last: int | None = None
    ) -> list[tuple[PLC, tuple[int, int]]]:
        """Each epoch's scans in *first*..*last* that ran without records, by the epoch's runner."""
        lineage = self._causal_lineage
        owners = [self] if self._unrecorded_scans else []
        for epoch in lineage.sealed_epochs:
            if epoch.unrecorded_scans:
                owners.append(lineage._query_for(epoch)._runner())
        return [
            (
                owner,
                (
                    start if first is None else max(start, first),
                    end if last is None else min(end, last),
                ),
            )
            for owner in owners
            for start, end in owner._unrecorded_scans
            if (last is None or start <= last) and (first is None or end >= first)
        ]

    def _replay_anchor_scan(self, scan_id: int) -> int:
        anchor = self._nearest_checkpoint_at_or_before(scan_id)
        return anchor if anchor is not None else self._initial_scan_id

    def _search_limits(self) -> Callable[[], None] | None:
        """What a how() asking this question polls while it waits for the answer."""
        return None if self._coast_memo is None else self._coast_memo.check_limits

    def _stepped_again(self, first: int, last: int, check_limits: Callable[[], None] | None) -> PLC:
        """A replay of scans up to *last* from the checkpoint before *first*, with its records."""
        anchor = self._nearest_checkpoint_at_or_before(first - 1)
        replay, log, anchor_scan_id, lifecycle_by_scan = self._build_replay_fork(anchor)
        for scan_id in range(anchor_scan_id + 1, last + 1):
            if check_limits is not None:
                check_limits()
            self._apply_log_entries_for_scan(replay, scan_id, log, lifecycle_by_scan)
            replay.step()
        return replay

    def _settle_window(
        self, first: int, last: int, check_limits: Callable[[], None] | None
    ) -> None:
        replay = self._stepped_again(first, last, check_limits)
        self._rung_firing_timelines.replace_range(replay._rung_firing_timelines, first, last)
        self._committed_tag_timelines.replace_range(replay._committed_tag_timelines, first, last)
        self._node_firing_timelines.replace_range(replay._node_firing_timelines, first, last)
        self._causal_lineage.invalidate_current_epoch()
        remaining: list[tuple[int, int]] = []
        for start, end in self._unrecorded_scans:
            if end < first or start > last:
                remaining.append((start, end))
                continue
            if start < first:
                remaining.append((start, first - 1))
            if end > last:
                remaining.append((last + 1, end))
        self._unrecorded_scans[:] = remaining

    def why(self, *tags: Tag | str) -> CausalChain:
        """Explain how tags reached their current values from a snapshot.

        No history required. Walks the program graph backward from each
        tag, using the current state as evidence. Terminates at external
        inputs.

        Multiple tags produce one unified tree — a single explanation
        consistent with all observations.

        Args:
            tags: One or more Tag objects or tag name strings.

        Returns:
            A :class:`~pyrung.core.analysis.causal.CausalChain` with
            ``mode='why'``.
        """
        if not tags:
            raise ValueError("why() requires at least one tag")

        from pyrung.core.analysis.causal import why_cause

        resolved = [self._normalize_tag_name(t, method="why") for t in tags]
        return why_cause(
            logic=self._logic,
            state=self._state,
            tags=resolved,
            pdg=self._ensure_pdg(),
            program=self._program,
        )

    def how(
        self,
        *conditions: Any,
        avoid: Any = None,
        max_scans: int = 4000,
        unlink: list[str] | None = None,
        on_event: Any = None,
        wall_clock_s: float | None = 300.0,
    ) -> Any:
        """Find an input-change sequence that reaches a target state.

        For a target with more than one route — a Bool (``Running``,
        ``Running == False``) or a word value (``State == 5``) — PILOT never
        reports ambiguous: it picks a deterministic default route, reaches the
        goal, and records where it went on ``Path.route``. Exclude a reported
        route with ``avoid=`` and PILOT will look for another.

        ``avoid X`` = do not take a path that depends on X.  It excludes routes,
        operator actions, and observed scan states that satisfy the predicate —
        momentary commands are treated as actions, not just settled states (so
        ``avoid=C_Complete`` will not *press* ``C_Complete`` even though it
        settles back to rest).  A path is excluded if it depends on the avoided
        condition at *any* of these three gates, and if every path is excluded
        the returned Path is unreachable with a reason that names the violated
        avoid condition(s).  A check that raises when it is asked raises
        ``AvoidCheckError`` here, since it has not said that state is clear.

        Args:
            conditions: Target condition(s).  Each is a Tag (``Running``) or a
                comparison (``State == 3``).  Pass more than one for an AND goal
                — ``how(Running, State == 3)`` reaches a single state where every
                target holds, or reports why they can't coexist.
            avoid: Condition(s) to keep the path (and the chosen route) clear of.
                One condition, or a tuple/list = **union of exclusions** (each
                condition avoided independently).  Express a composite prohibition
                explicitly — ``avoid=And(A, B)`` avoids only the combined state,
                not A or B alone.
            max_scans: Work budget for the search, in interpreted scans.  A scan run in
                the guarded loop costs a hundredth of one, charged when its seek returns:
                a seek in the loop is not stopped partway, however long.
            wall_clock_s: Seconds of real time before the search stops and says so, or
                ``None`` for no limit.  A search this ends may have reached on a faster
                machine.
            unlink: Harness-feedback tag names to free for fault injection.
                Named tags have their ``link=`` coupling removed so the harness
                stops driving them and PILOT may steer them directly — the way
                to reach a fault (e.g. a dead sensor) that the intact physical
                link would otherwise hold out of reach.

        Returns:
            A :class:`~pyrung.core.analysis.graph.Path`.
        """
        return self._how_with_pilot(
            *conditions,
            max_scans=max_scans,
            avoid=avoid,
            unlink=unlink,
            on_event=on_event,
            wall_clock_s=wall_clock_s,
        )

    def _how_with_pilot(
        self,
        *conditions: Any,
        max_scans: int = 4000,
        avoid: Any = None,
        unlink: list[str] | None = None,
        on_event: Any = None,
        wall_clock_s: float | None = 300.0,
    ) -> Any:
        """PILOT engine: backward-trace + forward-simulate."""
        from pyrung.core.analysis.pilot import pilot_how

        return pilot_how(
            self,
            *conditions,
            max_scans=max_scans,
            avoid_pred=_compile_avoid(avoid),
            unlink=unlink,
            on_event=on_event,
            wall_clock_s=wall_clock_s,
        )

    def recovers(self, tag: Tag | str, *, assume: dict[str, Any] | None = None) -> bool:
        """True if *tag* has a reachable clear path from the current state.

        Convenience predicate: ``cause(tag, to=resting).mode != 'unreachable'``.
        For the underlying chain (witness or blockers), call ``cause()`` directly.

        Tags marked ``external=True`` always return True — the recovery path
        exists outside the ladder by declaration.  When *assume* is provided
        the external shortcut is skipped so the analysis runs with the given
        overrides.

        Args:
            tag: Tag object or tag name string.
            assume: Tag-to-value overrides.  Pins the given tags to
                specified values during projected analysis.
        """
        from pyrung.core.analysis.query import find_tag_object
        from pyrung.core.tag import Tag as TagClass

        if isinstance(tag, TagClass):
            tag_obj = tag
            resting = tag.default
        else:
            tag_obj = find_tag_object(self._logic, tag)
            resting = self._resolve_resting_value(tag)

        # External tags recover by declaration — the external writer
        # handles it.  Skip when assume is provided so the caller can
        # exercise the actual recovery path.
        if not assume and tag_obj is not None and tag_obj.external:
            return True

        chain = self.cause(tag, to=resting, assume=assume)
        assert chain is not None  # projected mode never returns None
        return chain.mode != "unreachable"

    def _resolve_resting_value(self, tag_name: str) -> Any:
        """Resolve the resting (default) value for a tag name by searching logic."""
        from pyrung.core.analysis.query import find_tag_object

        tag_obj = find_tag_object(self._logic, tag_name)
        return tag_obj.default if tag_obj is not None else False

    @property
    def dataview(self) -> Any:
        """Chainable query over this program's tag dependency graph.

        Convenience shorthand for ``plc.program.dataview()`` — builds
        (and caches) the static program graph lazily on first access.
        """
        from pyrung.core.analysis.dataview import DataView

        return DataView.from_graph(self._ensure_pdg())

    @property
    def query(self) -> Any:
        """Survey namespace for whole-program dynamic analysis."""
        from pyrung.core.analysis.query import QueryNamespace

        return QueryNamespace(self)

    def _inspect(self, rung_id: int) -> RungTrace:
        """Return rung-level debug trace for the most recently committed scan.

        Only the most recent debug scan's traces are retained.

        Raises:
            KeyError: No debug trace for the current scan, or no trace for
                the requested rung.
        """
        if self._current_rung_traces_scan_id is None:
            raise KeyError(rung_id)

        try:
            return self._current_rung_traces[rung_id]
        except KeyError as exc:
            raise KeyError(rung_id) from exc

    def _inspect_event(self) -> tuple[int, int, RungTraceEvent] | None:
        """Return the latest debug-trace event for active/committed debug-path scans.

        Returns:
            A tuple of ``(scan_id, rung_id, event)``. In-flight debug-scan events
            are preferred when available. Otherwise, the latest retained committed
            debug-scan event is returned.

        Notes:
            - This API is populated by ``scan_steps_debug()`` only.
            - Scans produced through ``step()/run()/run_for()/run_until()`` do not
              contribute trace events here.
        """
        inflight = self._latest_inflight_trace_event
        if self._inflight_scan_id is not None and inflight is not None:
            return inflight

        committed = self._latest_committed_trace_event
        if committed is None:
            return None

        scan_id, rung_id, _event = committed
        if scan_id != self._current_rung_traces_scan_id:
            self._latest_committed_trace_event = None
            return None
        trace = self._current_rung_traces.get(rung_id)
        if trace is None:
            self._latest_committed_trace_event = None
            return None

        if not trace.events:
            self._latest_committed_trace_event = None
            return None

        latest_event = trace.events[-1]
        self._latest_committed_trace_event = (scan_id, rung_id, latest_event)
        return self._latest_committed_trace_event

    def fork(
        self,
        scan_id: int | None = None,
        *,
        history_budget: int | float | None = None,
        inherit_log: bool = True,
    ) -> PLC:
        """Create an independent runner from retained historical state.

        Args:
            scan_id: Snapshot to fork from. Defaults to current committed tip state.
            history_budget: Byte ceiling for the child's recent-state cache.
                Defaults to the parent's budget.  Pass ``math.inf`` to
                disable eviction entirely — every state the child produces
                stays cached, guaranteeing full-fidelity ``cause()`` chains
                without replay.  Useful for ephemeral forks that will be
                discarded shortly after causal analysis.
        """
        target_scan_id = self._state.scan_id if scan_id is None else scan_id
        if not self.history.contains(target_scan_id):
            raise KeyError(target_scan_id)
        historical_owner = self._causal_lineage.owner_at(target_scan_id)
        if historical_owner is None:
            raise KeyError(target_scan_id)
        historical_state = historical_owner.state_at(target_scan_id)
        effective_budget = (
            history_budget if history_budget is not None else self._recent_state_cache_budget
        )
        fork = PLC(
            logic=self._program if self._program is not None else list(self._logic),
            initial_state=self._seed_defaults_for_fork(historical_state),
            history=self._history_retention_scans,
            cache=None if effective_budget == math.inf else self._cache_retention_scans,
            history_budget=effective_budget,
            checkpoint_interval=self._checkpoint_interval,
            record_all_tags=self._record_all_tags,
            keep_forces_on_run=self._keep_forces_on_run,
            _tag_index=(
                self._known_tags_by_name,
                self._edge_tag_names,
                self._constrained_tags,
            ),
        )
        fork._tags_rungs_reach = self._tags_rungs_reach
        fork._names_no_rung_gave = set(self._names_no_rung_gave)
        fork._set_time_mode(self._time_mode, dt=self._dt)
        fork._coast_memo = self._coast_memo
        fork._search_refuses_restart = self._search_refuses_restart
        parent_rtc_at_fork_point = historical_owner.rtc_at(historical_state)
        fork._set_rtc_internal(parent_rtc_at_fork_point, fork.current_state.timestamp)
        if self._harness is not None:
            # Re-install the feedback harness on the fork (it rebuilds its plant
            # rungs against the fork's tags; accumulator state rides the inherited
            # committed state).
            self._harness.fork_onto(fork)
        if inherit_log:
            # Continue the parent's recording instead of starting a fresh log, so
            # a chain of execution forks accumulates the whole history —
            # the returned fork is a continuous recording from the anchor, not just
            # its final segment.
            fork._scan_log = self._scan_log.clone(up_to=target_scan_id)
            fork._causal_lineage = CausalLineage(
                fork,
                self._causal_lineage.seal_through(target_scan_id),
            )
            fork._causal_initial_tags = dict(self._causal_initial_tags)
        return fork

    def fork_from(self, scan_id: int) -> PLC:
        """Create an independent runner from a retained historical snapshot."""
        return self.fork(scan_id=scan_id)

    def _causal_history_range(
        self,
        start_scan_id: int,
        end_scan_id: int,
    ) -> list[SystemState]:
        """Historical states across this runner's read-only fork ancestry.

        An incident may begin in a sealed epoch and finish in the live runner.
        Stitch each immutable interval under its recorded synthesis overlay,
        already clamped at every fork boundary.
        """
        if end_scan_id <= start_scan_id:
            return []
        states: list[SystemState] = []
        for epoch, first_scan, last_scan in self._causal_lineage.epochs_covering(
            start_scan_id,
            end_scan_id - 1,
        ):
            states.extend(
                self._causal_lineage._query_for(epoch).history_range(first_scan, last_scan + 1)
            )
        return states

    def _local_history_range(
        self,
        start_scan_id: int,
        end_scan_id: int,
    ) -> list[SystemState]:
        """Local-epoch half-open range used beneath the inherited History view."""
        if end_scan_id <= start_scan_id:
            return []
        tip = self._state.scan_id
        lo = max(self._initial_scan_id, start_scan_id)
        hi = min(tip, end_scan_id - 1)
        if lo > hi:
            return []
        cache = self._recent_state_cache
        cache_lo = self._cache_oldest_scan_id()
        window_lo = cache_lo if cache_lo is not None else tip + 1
        if hi < window_lo:
            return self._replay_range(lo, hi)
        replayed = self._replay_range(lo, window_lo - 1) if lo < window_lo else []
        first = max(lo, window_lo)
        cached = {sid: state for sid, (state, _) in cache.items() if first <= sid <= hi}
        if len(cached) == hi - first + 1:
            return replayed + [cached[sid] for sid in range(first, hi + 1)]
        # A compiled run leaves its landing in the cache, not the scans before it.
        # Replay the gaps.
        missing = [sid for sid in range(first, hi + 1) if sid not in cached]
        gap_lo = missing[0]
        filled = self._replay_range(gap_lo, missing[-1])
        for offset, state in enumerate(filled):
            cached.setdefault(gap_lo + offset, state)
        return replayed + [cached[sid] for sid in range(first, hi + 1)]

    def _seed_defaults_for_fork(self, state: SystemState) -> SystemState:
        """Return *state* with defaults for any known tag it lacks.

        Upholds the ``_tag_index`` contract (the child skips its own
        default-seeding scan).  States usually already contain every
        known tag, so the result is *state* itself; a one-slot cache
        keyed on state identity and known-tag count makes repeated
        forks from the same snapshot — the walker forks the tip once
        per steer — skip even the membership scan.  Both cache keys
        are stable: states are immutable and the known-tags dict only
        grows.
        """
        cached = self._fork_seed_cache
        n_known = len(self._known_tags_by_name)
        if cached is not None and cached[0] is state and cached[1] == n_known:
            return cached[2]
        missing = self._known_tags_by_name.keys() - set(state.tags)
        seeded = (
            state.with_tags({name: self._known_tags_by_name[name].default for name in missing})
            if missing
            else state
        )
        self._fork_seed_cache = (state, n_known, seeded)
        return seeded

    def _state_at(self, scan_id: int) -> SystemState:
        """Return the ``SystemState`` for ``scan_id`` without recursing
        through ``History.at()``.

        Used by ``fork()`` and ``History.at()`` to avoid the
        ``replay_to → fork → history.at → replay_to`` loop.  Direct
        lookups (current tip, recent-state window, checkpoint dict,
        the pinned initial state) terminate immediately; the slab cache
        covers a bounded window behind a miss so that
        consecutive ``history.at()`` calls in causal backward walks
        resolve as dict lookups instead of independent replays.
        """
        if scan_id == self._state.scan_id:
            return self._state
        entry = self._recent_state_cache.get(scan_id)
        if entry is not None:
            return entry[0]
        if scan_id in self._checkpoints:
            return self._checkpoints[scan_id]
        if scan_id == self._initial_scan_id:
            return self._initial_state
        if self._initial_scan_id <= scan_id <= self._state.scan_id:
            for slab_key in reversed(self._replay_slabs):
                slab = self._replay_slabs[slab_key]
                if scan_id in slab:
                    self._replay_slabs.pop(slab_key)
                    self._replay_slabs[slab_key] = slab
                    return slab[scan_id]
            return self._replay_slab_fill(scan_id)
        raise KeyError(scan_id)

    def _replay_slab_fill(self, scan_id: int) -> SystemState:
        """Populate one bounded causal slab and return *scan_id*.

        A stretch's checkpoints can be far apart.  Positioning and materialization
        are separate: the run-up goes through the compiled kernel and leaves nothing
        behind, and only one fixed-size working set is retained.
        """
        desired_start = max(
            self._initial_scan_id + 1,
            scan_id - _REPLAY_SLAB_SCANS + 1,
        )
        # Anchor the slab's whole desired window, not the individual miss.
        # A causal walk routinely crosses replay checkpoints while moving
        # backward; choosing the checkpoint immediately before each miss would
        # truncate the nominal slab and make the one-slot cache ping-pong.
        anchor = self._nearest_checkpoint_at_or_before(desired_start - 1)
        anchor_scan = anchor if anchor is not None else self._initial_scan_id
        # Causal walks move backward from the requested point.  End the slab at
        # the miss and retain one contiguous preceding neighborhood, including
        # across intermediate replay checkpoints.
        slab_start = max(anchor_scan + 1, desired_start)
        slab_end = scan_id

        kernel = self._replay_kernel_from(anchor_scan)
        if slab_start > anchor_scan + 1 and kernel is not None:
            # History ownership has already selected the immutable execution
            # epoch that ran this scan. Its synthesis brackets are therefore
            # stable replay logic, just like the bare Program. A descendant never
            # reaches this branch with its own overlay for an inherited scan:
            # ``_causal_state_at`` delegates to the historical owner first.
            positioned = self._replay_to_compiled(slab_start - 1, kernel)
            states = self._replay_range_from_compiled(
                positioned,
                slab_start,
                slab_end,
                kernel,
            )
        else:
            states = self._replay_range(anchor_scan + 1, slab_end)
            if slab_start > anchor_scan + 1:
                states = [state for state in states if state.scan_id >= slab_start]
        slab = {state.scan_id: state for state in states}
        # Store under the anchor, marking it most-recently-used, and evict the
        # oldest anchors past the cap.
        slab_key = slab_start - 1
        self._replay_slabs.pop(slab_key, None)
        self._replay_slabs[slab_key] = slab
        while len(self._replay_slabs) > _REPLAY_SLAB_MAX_ANCHORS:
            self._replay_slabs.pop(next(iter(self._replay_slabs)))
        self._last_replay_slab_stats = {
            "runup_scans": max(0, slab_start - anchor_scan - 1),
            "materialized_states": len(slab),
        }
        if scan_id in slab:
            return slab[scan_id]
        return self.replay_to(scan_id).current_state

    def _cache_state(self, state: SystemState) -> None:
        """Add *state* to the recent-state cache, evicting if over budget."""
        est = _estimate_state_bytes(state)
        self._recent_state_cache[state.scan_id] = (state, est)
        self._recent_state_cache_bytes += est
        min_scan = (
            state.scan_id - self._cache_retention_scans
            if self._cache_retention_scans is not None
            else -1
        )
        while len(self._recent_state_cache) > _RECENT_STATE_CACHE_MIN_ENTRIES:
            oldest_sid = next(iter(self._recent_state_cache))
            over_budget = self._recent_state_cache_bytes > self._recent_state_cache_budget
            over_time = oldest_sid < min_scan
            if not (over_budget or over_time):
                break
            _, (_, evicted_est) = self._recent_state_cache.popitem(last=False)
            self._recent_state_cache_bytes -= evicted_est

    def _reset_cache(self, state: SystemState) -> None:
        """Clear cache and seed with a single *state*."""
        self._epoch_caches.on_replay_anchor_replaced()
        self._cache_state(state)

    def _edit_tip_tags(self, tags: Mapping[str, Any]) -> None:
        """Put *tags* in the state the next scan starts from, between scans.

        The scans before the tip ran without them and stay as they ran.  The tip's
        own entries follow it, so it reads the same before and after the next scan,
        and a causal transition's first ``before`` is the value set here.
        """
        state = self._state.with_tags(dict(tags))
        self._state = state
        scan_id = state.scan_id
        replaced = self._recent_state_cache.pop(scan_id, None)
        if replaced is not None:
            self._recent_state_cache_bytes -= replaced[1]
        self._cache_state(state)
        if scan_id in self._checkpoints:
            self._checkpoints[scan_id] = state
        at_initial = scan_id == self._initial_scan_id
        if at_initial:
            self._initial_state = state
        for name, value in tags.items():
            if at_initial or name not in self._causal_initial_tags:
                self._causal_initial_tags[name] = value

    def _note_plant_change(self) -> None:
        """Keep the plant that ran the scans so far: it is about to be replaced.

        Replay runs a scan under the plant that ran it.  A checkpoint here starts any
        later scan's replay on this side of the change, so no replay crosses it.
        """
        scan_id = self._state.scan_id
        if scan_id == self._initial_scan_id:
            return
        if self._plant_changes and self._plant_changes[-1][0] == scan_id:
            return
        plant = [] if self._synthesis is None else list(self._synthesis.plant)
        self._plant_changes.append((scan_id, plant))
        self._scan_log.record_force_changes(scan_id, self._forces_last_recorded)
        self._checkpoints[scan_id] = self._state

    def _earlier_plant_at(self, scan_id: int) -> list[Any] | None:
        """The plant the scans after *scan_id* ran under, when it is not the current one."""
        for changed_at, plant in self._plant_changes:
            if scan_id < changed_at:
                return plant
        return None

    def _plant_tag_defaults(self) -> dict[str, Any]:
        """Each tag the synthesis plant uses, at its default.

        A plant stores its timers' zeros from its first scan, into tags no rung has
        written.  A kernel holds an absent tag at its default and notices a write only
        by the value leaving it, so it could not say when such a tag joined the state.
        Present from the start of a recording, they read and commit the same in every
        engine.
        """
        synthesis = self._synthesis
        if synthesis is None:
            return {}
        tags, _edge_names = _iter_tags_and_edge_names(synthesis.plant)
        return {tag.name: tag.default for tag in tags if tag.name not in SYSTEM_TAGS_BY_NAME}

    def _state_in_cache(self, scan_id: int) -> bool:
        """True if *scan_id* is in the recent-state cache."""
        return scan_id in self._recent_state_cache

    def _cache_oldest_scan_id(self) -> int | None:
        """Scan_id of the oldest cached entry, or None if empty."""
        if not self._recent_state_cache:
            return None
        return next(iter(self._recent_state_cache))

    def _nearest_checkpoint_at_or_before(self, scan_id: int) -> int | None:
        """Largest retained checkpoint scan_id <= ``scan_id``, or None."""
        return max((c for c in self._checkpoints if c <= scan_id), default=None)

    def _nearest_checkpoint_at_or_after(self, scan_id: int) -> int | None:
        """Smallest retained checkpoint scan_id >= ``scan_id``, or None."""
        return min((c for c in self._checkpoints if c >= scan_id), default=None)

    def _trim_history_before(self, scan_id: int) -> None:
        """Advance the replay horizon to *scan_id*.

        Trims the scan log, rung-firing timelines, and checkpoints in
        lockstep and advances ``_initial_scan_id`` so that
        ``History.oldest_scan_id`` / ``contains()`` / ``scan_ids()``
        reflect the narrowed range.
        """
        if not self._causal_history_contains(scan_id):
            raise KeyError(scan_id)
        new_floor_state = self._causal_state_at(scan_id)
        self._epoch_caches.on_history_trimmed()
        self._causal_lineage.trim_before(scan_id)
        self._scan_log.trim_before(scan_id)
        self._rung_firing_timelines.trim_before(scan_id)
        self._committed_tag_timelines.trim_before(scan_id)
        self._node_firing_timelines.trim_before(scan_id)
        self._unrecorded_scans = [
            (max(first, scan_id), last) for first, last in self._unrecorded_scans if last >= scan_id
        ]
        for cp in [k for k in self._checkpoints if k < scan_id]:
            del self._checkpoints[cp]
        self._plant_changes = [c for c in self._plant_changes if c[0] >= scan_id]
        if scan_id > self._initial_scan_id:
            self._initial_scan_id = scan_id
            self._initial_state = new_floor_state
            self._causal_initial_tags = dict(new_floor_state.tags)
            self._causal_lineage = CausalLineage(self)
            while self._recent_state_cache:
                oldest_sid = next(iter(self._recent_state_cache))
                if oldest_sid >= scan_id:
                    break
                _, (_, evicted_est) = self._recent_state_cache.popitem(last=False)
                self._recent_state_cache_bytes -= evicted_est

    def _thin_checkpoints(self, keep: int) -> None:
        """Drop every other checkpoint once more than *keep* are held.

        A checkpoint only shortens a replay, since the scan log says the rest: one
        dropped costs run-up and never a value.  A checkpoint at a plant change stays,
        because no replay crosses one.  Its force record goes with it unless the forces
        changed on that scan.
        """
        if len(self._checkpoints) <= keep:
            return
        plant_changes = {changed_at for changed_at, _plant in self._plant_changes}
        dropped = {
            scan_id
            for position, scan_id in enumerate(sorted(self._checkpoints))
            if position % 2 == 1 and scan_id not in plant_changes
        }
        for scan_id in dropped:
            del self._checkpoints[scan_id]
        self._scan_log.drop_force_records_unchanged_at(dropped)

    def _bind_logic_sites(self) -> Any:
        """The site table of the logic this runner scans (binding it if never bound)."""

        program = self._program
        if program is not None:
            table = getattr(program, "_site_table", None)
            if table is None:
                program._assign_state_keys()
                table = program._site_table
            return table
        # A list whose rungs would clash comes back copied (originals keep their keys).
        self._logic, table = bind_logic_list(self._logic)
        return table

    def _soft_exec_program(self) -> Any:
        """The compilation unit for soft-exec replay: the user program bracketed
        by the synthesis overlay (``plant`` + ``holds`` + user rungs).

        Synthesis is *logic*, so replay re-derives it by compiling it into the
        replay kernel — bool TON/TOF compile natively, an opaque ``run_function``
        that may not be called again falls back to interpreted replay (which re-installs
        synthesis via ``fork_onto``).  This is the
        *soft* root; deploy (Click / CircuitPython) and ``prove`` compile the
        **bare** ``self._program`` instead — the brackets never reach a
        controller or the verifier.

        Returns the bare program when no overlay is installed; cached, since the
        bracketing is a pure function of the program + overlay rung lists (the
        harness invalidates the cache when the overlay changes).
        """
        syn = self._synthesis
        if self._program is None or syn is None or syn.is_empty():
            return self._program
        cached = self._soft_exec_program_cache
        if cached is not None:
            return cached
        from pyrung.core.program import Program

        bracketed = Program.__new__(Program)
        bracketed.rungs = [*syn.plant, *syn.holds, *self._program.rungs]
        bracketed.subroutines = self._program.subroutines
        self._soft_exec_program_cache = bracketed
        return bracketed

    def _compiled_replay_supported_kernel(self) -> CompiledKernel | None:
        from pyrung.circuitpy.codegen import compile_kernel

        cached = self._compiled_replay_kernel
        if isinstance(cached, CompiledKernel):
            return cached
        if cached is False:
            return None
        if self._time_mode != TimeMode.FIXED_STEP or self._program is None:
            self._compiled_replay_kernel = False
            return None
        if _program_writes_read_only_system_tags(
            self._program
        ) or _program_calls_what_a_kernel_may_not(self._soft_exec_program()):
            self._compiled_replay_kernel = False
            return None
        # Compile the synthesis pre-pass (``plant`` + ``holds`` — the leading
        # bracket rungs) as a separate step so the replay runner can drain recorded
        # patches *between* it and the user program: the plant reads the previous
        # commit (input-read) and holds steer inputs, both before the drain, so a
        # patch overrides a hold (canonical ``plant < holds < patches < forces``).
        # This matches the interpreted runner, which evals plant+holds before
        # ``apply_pre_scan``.
        syn = self._synthesis
        split_after = (
            len(syn.plant) + len(syn.holds) if (syn is not None and not syn.is_empty()) else None
        )
        try:
            kernel = compile_kernel(self._soft_exec_program(), split_after=split_after)
        except Exception as exc:
            # A gap is expected.  Anything else is pyrung's own bug and is said, but the kernel
            # is only speed: what the scans would answer, an interpreted scan answers too.
            if not _looks_like_compiled_replay_gap(exc):
                warnings.warn(
                    f"compiling the kernel failed ({type(exc).__name__}: {exc}); "
                    "these scans run interpreted, slower and with the same answers",
                    RuntimeWarning,
                    stacklevel=2,
                )
            self._compiled_replay_kernel = False
            return None
        if kernel.has_io_gaps:
            self._compiled_replay_kernel = False
            return None
        self._compiled_replay_kernel = kernel
        return kernel

    def _copy_current_synthesis_onto(self, target: PLC) -> None:
        """Give *target* this runner's current soft-execution overlay.

        Internal replay reconstructs one current executable World, so its
        interpreted and compiled paths must see the same plant and PILOT hold
        rungs. Public :meth:`fork` deliberately keeps its existing semantics;
        callers that own PILOT rung records rebuild them explicitly.
        """
        from pyrung.core.synthesis import Synthesis

        target._synthesis = (
            None
            if self._synthesis is None
            else Synthesis(
                holds=list(self._synthesis.holds),
                plant=list(self._synthesis.plant),
            )
        )
        target._compiled_replay_kernel = None
        target._soft_exec_program_cache = None

    def _fork_from_reconstructed_state(
        self,
        state: SystemState,
        *,
        rtc_at_state: datetime,
        forces: Mapping[str, bool | int | float | str],
        replay_mode: bool,
    ) -> PLC:
        fork = PLC(
            logic=self._program if self._program is not None else list(self._logic),
            initial_state=state,
            history=self._history_retention_scans,
            cache=self._cache_retention_scans,
            history_budget=self._recent_state_cache_budget,
            checkpoint_interval=self._checkpoint_interval,
            record_all_tags=self._record_all_tags,
            keep_forces_on_run=self._keep_forces_on_run,
            # Reuse the parent's tag/edge/constraint index: same program, so
            # re-walking the object graph in __init__ (a ~hundreds-of-ms BFS) is
            # pure waste — and the default-seeding it gates is overwritten by
            # ``fork._state = state`` immediately below anyway.  This is the hot
            # path under cause()'s historical-view replays.
            _tag_index=(
                self._known_tags_by_name,
                self._edge_tag_names,
                self._constrained_tags,
            ),
        )
        fork._tags_rungs_reach = self._tags_rungs_reach
        fork._names_no_rung_gave = set(self._names_no_rung_gave)
        fork._state = state
        fork._reset_cache(state)
        fork._initial_scan_id = state.scan_id
        fork._initial_state = state
        fork._playhead = state.scan_id
        fork._set_time_mode(TimeMode.FIXED_STEP, dt=self._dt)
        fork._set_rtc_internal(rtc_at_state, state.timestamp)
        # The reconstructed fork executes the exact same Program object. Reuse
        # its immutable dependency graph and capture filter instead of rebuilding
        # both for every historical scan replay.
        fork._pdg_cache = self._ensure_pdg()
        fork._pdg_consumed_tags = self._pdg_consumed_tags
        fork._input_overrides._forces.clear()
        fork._input_overrides._forces.update(forces)
        # Historical at-fire replay must execute the same current soft program
        # as its source. PILOT holds and Harness plant rungs live outside the
        # user Program, so copy both brackets explicitly.
        self._copy_current_synthesis_onto(fork)
        fork._replay_mode = replay_mode
        fork._sync_runtime_flags_from_state()
        return fork

    def _build_replay_fork(
        self, anchor: int | None
    ) -> tuple[PLC, ScanLogSnapshot, int, dict[int, list[LifecycleEvent]]]:
        """Construct a replay fork anchored at the given checkpoint scan_id.

        ``anchor`` is either a key in ``self._checkpoints`` (from
        ``_nearest_checkpoint_at_or_before``) or ``None``.  When ``None``
        the fork anchors at ``self._initial_scan_id``.  The returned
        fork has ``_replay_mode=True`` and (when anchored at a real
        checkpoint) its force map seeded from the log.  The checkpoint
        bypass guarantees every checkpoint scan carries a full force
        snapshot; anchoring at ``_initial_scan_id`` starts with an empty
        force map, matching default PLC construction.
        """
        log = self._scan_log.snapshot()
        anchor_scan_id = anchor if anchor is not None else self._initial_scan_id
        replay = self.fork(scan_id=anchor_scan_id, inherit_log=False)
        self._copy_current_synthesis_onto(replay)
        earlier_plant = self._earlier_plant_at(anchor_scan_id)
        if earlier_plant is not None:
            if replay._synthesis is None:
                from pyrung.core.synthesis import Synthesis

                replay._synthesis = Synthesis()
            replay._synthesis.plant = list(earlier_plant)
            if replay._harness is not None:
                # Today's couplings do not describe these rungs.
                replay._harness._detach()
        replay._replay_mode = True
        if anchor is not None:
            replay._input_overrides._forces.clear()
            replay._input_overrides._forces.update(log.force_changes_by_scan[anchor])
        lifecycle_by_scan: dict[int, list[LifecycleEvent]] = {}
        for event in log.lifecycle_events:
            lifecycle_by_scan.setdefault(event.at_scan_id, []).append(event)
        return replay, log, anchor_scan_id, lifecycle_by_scan

    def _apply_log_entries_for_scan(
        self,
        replay: PLC,
        scan_id: int,
        log: ScanLogSnapshot,
        lifecycle_by_scan: dict[int, list[LifecycleEvent]],
    ) -> None:
        """Prepare ``replay`` to step scan ``scan_id`` from the log.

        Applies captured lifecycle events, force-map replacements, RTC
        base changes, patches, and per-scan ``dt`` (REALTIME).  Does not
        call ``replay.step()`` — the caller decides whether to advance
        via ``step()`` (plain path) or by driving ``_scan_steps_debug``
        directly (trace-regeneration path).
        """
        for event in lifecycle_by_scan.get(scan_id, []):
            _apply_lifecycle_to_replay(replay, event)
        if scan_id in log.force_changes_by_scan:
            replay._input_overrides._forces.clear()
            replay._input_overrides._forces.update(log.force_changes_by_scan[scan_id])
        if scan_id in log.rtc_base_changes:
            base, base_sim_time = log.rtc_base_changes[scan_id]
            replay._set_rtc_internal(base, base_sim_time)
        if scan_id in log.patches_by_scan:
            replay.patch_as_recorded(log.patches_by_scan[scan_id])
        if log.dts is not None:
            replay._dt_override_for_next_scan = float(log.dts[scan_id - log.base_scan])
        submits = log.io_submits_by_scan.get(scan_id, {})
        drains = log.io_drains_by_scan.get(scan_id, {})
        if submits or drains:
            replay._next_scan_replay_io = (submits, drains)

    def _replay_force_map_at_scan(
        self,
        scan_id: int,
        log: ScanLogSnapshot,
    ) -> Mapping[str, bool | int | float | str]:
        force_scan = max((sid for sid in log.force_changes_by_scan if sid <= scan_id), default=None)
        if force_scan is None:
            return {}
        return log.force_changes_by_scan[force_scan]

    def _replay_rtc_at_state(
        self,
        state: SystemState,
        log: ScanLogSnapshot | ScanLog,
    ) -> datetime:
        base, base_sim_time = self._replay_rtc_base_at_state(state, log)
        return base + timedelta(seconds=float(state.timestamp) - base_sim_time)

    def _replay_rtc_base_at_state(
        self,
        state: SystemState,
        log: ScanLogSnapshot | ScanLog,
    ) -> tuple[datetime, float]:
        """The RTC base *state* ran on: a later change must not reach back to it."""
        recorded = log.rtc_base_at(state.scan_id)
        return (self._rtc_base, self._rtc_base_sim_time) if recorded is None else recorded

    def _replay_to_interpreted(self, target_scan_id: int) -> PLC:
        """Reconstruct historical state by forking and replaying the scan log.

        Anchors at the nearest retained checkpoint ``<= target_scan_id``
        (falling back to scan 0 when no earlier checkpoint exists) and
        walks the scan log forward to ``target_scan_id``, applying
        captured lifecycle events, force-map replacements, RTC base
        changes, patches, and per-scan ``dt`` in REALTIME mode.

        Returns a fork positioned at ``target_scan_id`` with
        ``_replay_mode=True``.  The returned fork is primarily for
        inspection; callers who want to continue it as a live
        investigation session can clear ``fork._replay_mode = False``.
        """
        if target_scan_id < self._initial_scan_id:
            raise ValueError(
                f"target_scan_id must be >= {self._initial_scan_id}, got {target_scan_id}"
            )
        if target_scan_id > self._state.scan_id:
            raise ValueError(
                f"target_scan_id {target_scan_id} is beyond current tip {self._state.scan_id}"
            )
        log_base = self._scan_log.base_scan
        if target_scan_id < log_base:
            raise ValueError(
                f"target_scan_id {target_scan_id} predates the log horizon "
                f"({log_base}); those scans have been trimmed"
            )

        anchor = self._nearest_checkpoint_at_or_before(target_scan_id)
        replay, log, anchor_scan_id, lifecycle_by_scan = self._build_replay_fork(anchor)

        for scan_id in range(anchor_scan_id + 1, target_scan_id + 1):
            self._apply_log_entries_for_scan(replay, scan_id, log, lifecycle_by_scan)
            replay.step()

        # Trailing lifecycle events that fired after the last committed
        # scan (e.g. a trailing stop() with no subsequent step).
        for event in lifecycle_by_scan.get(target_scan_id + 1, []):
            _apply_lifecycle_to_replay(replay, event)

        return replay

    def _replay_kernel_from(self, scan_id: int) -> CompiledKernel | None:
        """The kernel, if it can rebuild every key of the scans after *scan_id*'s checkpoint.

        Stepping adds a tag to the state on its first write.  A kernel holds an absent
        tag at its default and notices the first write only by the value leaving it, so
        a rung, patch or force that may store the default into a tag the checkpoint lacks
        replays interpreted.  compiled_run.run_scans holds a live run to the same rule.

        A rung reading the scan time minimum: the first scan's hook takes the scan time
        when the tag is absent, and a kernel has the tag, at 0, so it would stay 0.
        """
        if self._earlier_plant_at(scan_id) is not None:
            return None  # The kernel is compiled from the current plant.
        kernel = self._compiled_replay_supported_kernel()
        if kernel is None:
            return None
        anchor = self._nearest_checkpoint_at_or_before(scan_id)
        state = self._checkpoints[anchor] if anchor is not None else self._initial_state
        if _SCAN_TIME_MIN_NAME in kernel.referenced_tags and _SCAN_TIME_MIN_NAME not in state.tags:
            return None
        absent = absent_scalars(kernel, state.tags)
        if not absent:
            return kernel
        from pyrung.core.scan_trace import may_store_a_default

        if may_store_a_default(kernel, absent):
            return None
        log = self._scan_log.snapshot()
        for named in (*log.patches_by_scan.values(), *log.force_changes_by_scan.values()):
            if not absent.keys().isdisjoint(named):
                return None
        return kernel

    def _replay_to_compiled(
        self,
        target_scan_id: int,
        kernel: CompiledKernel,
    ) -> PLC:
        anchor = self._nearest_checkpoint_at_or_before(target_scan_id)
        log = self._scan_log.snapshot()
        anchor_scan_id = anchor if anchor is not None else self._initial_scan_id
        anchor_state = self._checkpoints[anchor] if anchor is not None else self._initial_state
        lifecycle_by_scan: dict[int, list[LifecycleEvent]] = {}
        for event in log.lifecycle_events:
            lifecycle_by_scan.setdefault(event.at_scan_id, []).append(event)

        replay = CompiledPLC(
            self._soft_exec_program(),
            initial_state=anchor_state,
            dt=self._dt,
            compiled=kernel,
            exact_anchor=True,
        )
        replay._set_rtc_internal(*self._replay_rtc_base_at_state(anchor_state, log))
        if anchor is not None:
            replay._input_overrides._forces.clear()
            replay._input_overrides._forces.update(log.force_changes_by_scan[anchor])

        for scan_id in range(anchor_scan_id + 1, target_scan_id + 1):
            for event in lifecycle_by_scan.get(scan_id, []):
                _apply_lifecycle_to_replay(replay, event)
            if scan_id in log.force_changes_by_scan:
                replay._input_overrides._forces.clear()
                replay._input_overrides._forces.update(log.force_changes_by_scan[scan_id])
            if scan_id in log.rtc_base_changes:
                base, base_sim_time = log.rtc_base_changes[scan_id]
                replay._set_rtc_internal(base, base_sim_time)
            if scan_id in log.patches_by_scan:
                replay.patch_as_recorded(log.patches_by_scan[scan_id])

            replay.step_replay()
            for record in log.io_submits_by_scan.get(scan_id, {}).values():
                for tag_name, value in record.tag_writes:
                    replay.apply_replay_io_write(tag_name, value)
            for record in log.io_drains_by_scan.get(scan_id, {}).values():
                for tag_name, value in record.tag_writes:
                    replay.apply_replay_io_write(tag_name, value)

        for event in lifecycle_by_scan.get(target_scan_id + 1, []):
            _apply_lifecycle_to_replay(replay, event)

        state = replay._materialize_replay_state()
        return self._fork_from_reconstructed_state(
            state,
            rtc_at_state=replay._rtc_at_sim_time(state.timestamp),
            forces=replay._input_overrides.forces_mutable,
            replay_mode=True,
        )

    def replay_to(self, target_scan_id: int) -> PLC:
        """Reconstruct historical state, preferring compiled replay when supported."""
        if target_scan_id < self._initial_scan_id:
            raise ValueError(
                f"target_scan_id must be >= {self._initial_scan_id}, got {target_scan_id}"
            )
        if target_scan_id > self._state.scan_id:
            raise ValueError(
                f"target_scan_id {target_scan_id} is beyond current tip {self._state.scan_id}"
            )
        log_base = self._scan_log.base_scan
        if target_scan_id < log_base:
            raise ValueError(
                f"target_scan_id {target_scan_id} predates the log horizon "
                f"({log_base}); those scans have been trimmed"
            )

        kernel = self._replay_kernel_from(target_scan_id)
        if kernel is None:
            return self._replay_to_interpreted(target_scan_id)
        return self._replay_to_compiled(target_scan_id, kernel)

    def replay_trace_at(self, target_scan_id: int) -> dict[int, RungTrace]:
        """Reconstruct the rung-trace dict for a historical scan.

        Runs the same replay walk as ``replay_to`` up to
        ``target_scan_id - 1`` on the plain scan path, then drives
        ``_scan_steps_debug`` for ``target_scan_id`` so the replay
        fork's ``_current_rung_traces`` gets populated.  Returns a copy
        of that dict; the replay fork is discarded.

        The ``_replay_mode`` guards in ``_commit_scan`` (monitors,
        breakpoints) and ``_set_rtc_and_record`` cover the debug path
        too — both generators funnel through the same commit sink.

        A one-slot cache (``_cached_replay_trace``) hits when the same
        ``target_scan_id`` is requested back-to-back.  It is cleared on
        any tip advance (``_run_single_scan``) and on reset paths that
        reset the log (reboot, stop→run, via
        ``_clear_retained_debug_trace_caches``).

        Traces only exist for scans that actually executed — the fork
        anchor / initial scan was never stepped in debug mode — so
        ``target_scan_id`` must be strictly greater than
        ``_initial_scan_id``.
        """
        if target_scan_id <= self._initial_scan_id:
            raise ValueError(
                f"target_scan_id must be > {self._initial_scan_id} "
                f"(no traces exist for the initial scan), got {target_scan_id}"
            )
        if target_scan_id > self._state.scan_id:
            raise ValueError(
                f"target_scan_id {target_scan_id} is beyond current tip {self._state.scan_id}"
            )

        cached = self._cached_replay_trace
        if cached is not None and cached[0] == target_scan_id:
            return dict(cached[1])

        anchor = self._nearest_checkpoint_at_or_before(target_scan_id)
        replay, log, anchor_scan_id, lifecycle_by_scan = self._build_replay_fork(anchor)

        for scan_id in range(anchor_scan_id + 1, target_scan_id):
            self._apply_log_entries_for_scan(replay, scan_id, log, lifecycle_by_scan)
            replay.step()

        # Final scan: drive the debug generator directly so the replay
        # fork's ``_current_rung_traces`` / ``_current_rung_traces_scan_id``
        # slots land populated post-commit.
        self._apply_log_entries_for_scan(replay, target_scan_id, log, lifecycle_by_scan)
        for _step in replay._scan_steps_debug():
            pass

        traces = dict(replay._current_rung_traces)
        self._cached_replay_trace = (target_scan_id, traces)
        return dict(traces)

    def _replay_capture_at(self, target_scan_id: int) -> ConditionViewCapture | None:
        """Run the on-demand interpreted replay of ``target_scan_id``.

        Mirrors :meth:`replay_trace_at`, but drives the *final* scan
        through ``execute_program`` — the live program scan path
        (runner.py ``_run_single_scan``) — with a
        :class:`ConditionViewCapture` observer.  Because it reuses the
        live execution path, the captured views and data reads match
        exactly what each rung read during the original scan.

        The run-up from the nearest checkpoint to ``target - 1`` uses the
        fast compiled kernel (``replay.step()``); only the target scan, all
        rungs, runs interpreted with the capturing observer.  Returns the
        populated capture (``.views`` + ``.reads``), or ``None`` (never
        raises) when there is nothing to replay: no Program (logic-list PLCs
        have no subroutines), the scan predates the first executed scan, or
        the scan is beyond the tip.  A bounded epoch-local LRU is shared by
        :meth:`_replay_node_views_at`, :meth:`_replay_rung_runs_at`, and
        :meth:`_replay_node_reads_at`.  The epoch boundary is the correctness
        boundary: sibling worlds may reuse an immutable prefix scan, but never
        a scan executed under a different synthesis overlay.
        """
        if self._program is None:
            return None
        if target_scan_id <= self._initial_scan_id or target_scan_id > self._state.scan_id:
            return None
        if target_scan_id < self._scan_log.base_scan:
            return None

        cached = self._cached_replay_captures.get(target_scan_id)
        if cached is not None:
            self._cached_replay_captures.move_to_end(target_scan_id)
            return cached

        capture = self._build_replay_capture_at(target_scan_id)
        if capture is None:
            return None

        self._cached_replay_captures[target_scan_id] = capture
        self._cached_replay_captures.move_to_end(target_scan_id)
        while len(self._cached_replay_captures) > _REPLAY_CAPTURE_CACHE_SCANS:
            self._cached_replay_captures.popitem(last=False)
        return capture

    def _build_replay_capture_at(
        self,
        target_scan_id: int,
    ) -> ConditionViewCapture | None:
        """Build one exact historical capture without retaining it."""
        if self._program is None:
            return None
        if target_scan_id <= self._initial_scan_id or target_scan_id > self._state.scan_id:
            return None
        if target_scan_id < self._scan_log.base_scan:
            return None

        log = self._scan_log.snapshot()
        lifecycle_by_scan: dict[int, list[LifecycleEvent]] = {}
        for event in log.lifecycle_events:
            lifecycle_by_scan.setdefault(event.at_scan_id, []).append(event)

        # Only the *target* scan needs to run interpreted (to capture each rung's
        # at-fire-time ConditionView).  Position the fork at ``target - 1`` from
        # the shared historical slab.  A causal walk asks for many adjacent and
        # repeated points, so the bounded slab is intentional amortization; its
        # refill may use a compiled run-up without throwing that locality away.
        prev_scan = target_scan_id - 1
        if prev_scan >= self._initial_scan_id and prev_scan >= self._scan_log.base_scan:
            prev_state = self._state_at(prev_scan)
            replay = self._fork_from_reconstructed_state(
                prev_state,
                rtc_at_state=self._replay_rtc_at_state(prev_state, log),
                forces=self._replay_force_map_at_scan(prev_scan, log),
                replay_mode=True,
            )
        else:
            # ``target`` is the first reconstructable scan — nothing before it to
            # replay to; build the fork directly at the anchor.
            anchor = self._nearest_checkpoint_at_or_before(target_scan_id)
            replay, log, anchor_scan_id, lifecycle_by_scan = self._build_replay_fork(anchor)
            for scan_id in range(anchor_scan_id + 1, target_scan_id):
                self._apply_log_entries_for_scan(replay, scan_id, log, lifecycle_by_scan)
                replay.step()

        # Final scan: run the live program path with a capturing observer.
        self._apply_log_entries_for_scan(replay, target_scan_id, log, lifecycle_by_scan)
        capture = ConditionViewCapture()
        ctx, _dt = replay._prepare_scan(synthesis_observer=capture)
        execute_program(replay._program, ctx, capture_rungs=True, observer=capture)
        # Finish observable scan phases without committing the disposable
        # replay. Post-logic forces and scan-end runtime writes remain part of
        # the same root journal, while timelines/history/monitors stay untouched.
        replay._input_overrides.apply_post_logic(ctx)
        replay._system_runtime.on_scan_end(ctx)
        capture.exit_tags = ctx.commit(dt=_dt).tags
        return capture

    def _replay_node_views_at(self, target_scan_id: int) -> dict[RungId, ConditionView]:
        """At-fire-time ``ConditionView`` per rung for a historical scan.

        Recorded ``cause()`` uses these to classify subroutine-writer contacts
        at the moment the rung fired rather than at end-of-scan.  Thin accessor
        over :meth:`_replay_capture_at`; returns ``{}`` when there is nothing to
        replay.
        """
        owner = self._causal_lineage.owner_at(target_scan_id)
        capture = owner.replay_capture_at(target_scan_id) if owner is not None else None
        return capture.views if capture is not None else {}

    def _replay_rung_runs_at(self, target_scan_id: int) -> tuple[RungRun, ...]:
        """Every rung occurrence from one historical scan, in execution order.

        Unlike the compact node timelines and ``_replay_node_views_at`` map,
        this preserves repeated calls of the same subroutine rung separately.
        It is reconstructed on demand and never retained per scan.
        """
        owner = self._causal_lineage.owner_at(target_scan_id)
        capture = owner.replay_capture_at(target_scan_id) if owner is not None else None
        return capture.runs if capture is not None else ()

    def _replay_rung_write_projection_at(self, target_scan_id: int) -> Any:
        """Exact read/write projection cached with its immutable scan journal."""
        owner = self._causal_lineage.owner_at(target_scan_id)
        capture = owner.replay_capture_at(target_scan_id) if owner is not None else None
        return self._projection_from_capture(target_scan_id, capture)

    def _replay_pilot_rung_write_projection_at(self, target_scan_id: int) -> Any:
        """Pilot guard projection including exact tag and memory reads.

        The public causal projection remains tag-only. Pilot's retained compact
        stream includes memory-backed guard reads, so its all-or-nothing
        historical fallback must reconstruct the same broader surface.
        """

        owner = self._causal_lineage.owner_at(target_scan_id)
        capture = owner.replay_capture_uncached_at(target_scan_id) if owner is not None else None
        try:
            projection = self._projection_from_capture(
                target_scan_id,
                capture,
                include_memory_reads=True,
            )
            if projection is None:
                return None

            from pyrung.core.analysis.causal._rung_writes import (
                compact_projection_condition_views,
            )

            compact_projection_condition_views(projection)
        except ValueError:
            # A projection that cannot be compacted is unavailable evidence.
            # Pilot fails closed to UNKNOWN; unexpected implementation faults
            # remain visible to callers.
            return None
        return projection

    def _projection_from_capture(
        self,
        target_scan_id: int,
        capture: ConditionViewCapture | None,
        *,
        include_memory_reads: bool = False,
    ) -> Any:
        if capture is None:
            return None
        if include_memory_reads:
            from pyrung.core.analysis.causal._rung_writes import (
                build_scan_rung_write_projection,
            )

            # Pilot's transition-local compact views also need exact memory
            # guard reads. Keep that broader surface out of the capture's
            # generic tag-only causal cache.
            return build_scan_rung_write_projection(
                self.history,
                target_scan_id,
                capture.runs,
                entry_tags=capture.entry_tags,
                exit_tags=capture.exit_tags,
                include_memory_reads=True,
            )
        projection = capture._causal_projection
        if projection is None:
            from pyrung.core.analysis.causal._rung_writes import (
                build_scan_rung_write_projection,
            )

            projection = build_scan_rung_write_projection(
                self.history,
                target_scan_id,
                capture.runs,
                entry_tags=capture.entry_tags,
                exit_tags=capture.exit_tags,
            )
            capture._causal_projection = projection
        return projection

    def _replay_node_reads_at(self, target_scan_id: int) -> dict[RungId, set[str]]:
        """Per-node data-read footprint for a historical scan (Crossings Tier 2).

        The set of operand tags each rung actually read during ``target_scan_id``
        — resolved indirect addresses, and only the branch that fired — captured
        in the same interpreted replay as :meth:`_replay_node_views_at`.  Recorded
        ``cause()`` prefers these over the static (union) PDG footprint when
        crossing an opaque data-writer.  Returns ``{}`` when there is nothing to
        replay.
        """
        owner = self._causal_lineage.owner_at(target_scan_id)
        capture = owner.replay_capture_at(target_scan_id) if owner is not None else None
        return capture.reads if capture is not None else {}

    def _replay_range_interpreted(self, start_scan_id: int, end_scan_id: int) -> list[SystemState]:
        """Reconstruct ``SystemState`` for every scan in ``[start, end]``.

        Anchors once at the nearest checkpoint ``<= start`` (falling
        back to scan 0) and walks the scan log forward to
        ``end_scan_id``, accumulating the committed state after each
        scan in the requested range.  Cheaper than N independent
        ``replay_to`` calls because it pays the fork-from-checkpoint
        cost once.

        Used by ``History.range`` / ``History.latest`` when the
        requested range falls outside the live recent-state window.
        """
        if start_scan_id < self._initial_scan_id or end_scan_id < start_scan_id:
            return []
        tip = self._state.scan_id
        if start_scan_id > tip:
            return []
        end_scan_id = min(end_scan_id, tip)

        anchor = self._nearest_checkpoint_at_or_before(start_scan_id)
        replay, log, anchor_scan_id, lifecycle_by_scan = self._build_replay_fork(anchor)

        results: list[SystemState] = []
        if anchor_scan_id >= start_scan_id:
            results.append(replay.current_state)

        for scan_id in range(anchor_scan_id + 1, end_scan_id + 1):
            self._apply_log_entries_for_scan(replay, scan_id, log, lifecycle_by_scan)
            replay.step()
            if scan_id >= start_scan_id:
                results.append(replay.current_state)

        return results

    def _replay_range_compiled(
        self,
        start_scan_id: int,
        end_scan_id: int,
        kernel: CompiledKernel,
    ) -> list[SystemState]:
        anchor = self._nearest_checkpoint_at_or_before(start_scan_id)
        log = self._scan_log.snapshot()
        anchor_scan_id = anchor if anchor is not None else self._initial_scan_id
        anchor_state = self._checkpoints[anchor] if anchor is not None else self._initial_state
        lifecycle_by_scan: dict[int, list[LifecycleEvent]] = {}
        for event in log.lifecycle_events:
            lifecycle_by_scan.setdefault(event.at_scan_id, []).append(event)

        replay = CompiledPLC(
            self._soft_exec_program(),
            initial_state=anchor_state,
            dt=self._dt,
            compiled=kernel,
            exact_anchor=True,
        )
        replay._set_rtc_internal(*self._replay_rtc_base_at_state(anchor_state, log))
        if anchor is not None:
            replay._input_overrides._forces.clear()
            replay._input_overrides._forces.update(log.force_changes_by_scan[anchor])

        results: list[SystemState] = []
        if anchor_scan_id >= start_scan_id:
            results.append(replay.current_state)

        for scan_id in range(anchor_scan_id + 1, end_scan_id + 1):
            for event in lifecycle_by_scan.get(scan_id, []):
                _apply_lifecycle_to_replay(replay, event)
            if scan_id in log.force_changes_by_scan:
                replay._input_overrides._forces.clear()
                replay._input_overrides._forces.update(log.force_changes_by_scan[scan_id])
            if scan_id in log.rtc_base_changes:
                base, base_sim_time = log.rtc_base_changes[scan_id]
                replay._set_rtc_internal(base, base_sim_time)
            if scan_id in log.patches_by_scan:
                replay.patch_as_recorded(log.patches_by_scan[scan_id])
            if scan_id >= start_scan_id:
                replay.step()
                results.append(replay.current_state)
            else:
                replay.step_replay()

        return results

    def _replay_range_from_compiled(
        self,
        positioned: PLC,
        start_scan_id: int,
        end_scan_id: int,
        kernel: CompiledKernel,
    ) -> list[SystemState]:
        """Materialize one bounded range after a separately-positioned run-up.

        ``positioned`` must be at ``start_scan_id - 1``.  This second phase
        executes every requested scan and retains exactly those states for the
        causal slab.
        """
        if positioned.state.scan_id != start_scan_id - 1:
            raise ValueError(
                "positioned replay must be at start_scan_id - 1 "
                f"(got {positioned.state.scan_id}, expected {start_scan_id - 1})"
            )

        log = self._scan_log.snapshot()
        lifecycle_by_scan: dict[int, list[LifecycleEvent]] = {}
        for event in log.lifecycle_events:
            lifecycle_by_scan.setdefault(event.at_scan_id, []).append(event)

        replay = CompiledPLC(
            self._soft_exec_program(),
            initial_state=positioned.state,
            dt=self._dt,
            compiled=kernel,
            exact_anchor=True,
        )
        replay._set_rtc_internal(
            positioned._rtc_at_sim_time(positioned.state.timestamp),
            positioned.state.timestamp,
        )
        replay._input_overrides._forces.clear()
        replay._input_overrides._forces.update(positioned._input_overrides.forces)

        results: list[SystemState] = []
        for scan_id in range(start_scan_id, end_scan_id + 1):
            for event in lifecycle_by_scan.get(scan_id, []):
                _apply_lifecycle_to_replay(replay, event)
            if scan_id in log.force_changes_by_scan:
                replay._input_overrides._forces.clear()
                replay._input_overrides._forces.update(log.force_changes_by_scan[scan_id])
            if scan_id in log.rtc_base_changes:
                base, base_sim_time = log.rtc_base_changes[scan_id]
                replay._set_rtc_internal(base, base_sim_time)
            if scan_id in log.patches_by_scan:
                replay.patch_as_recorded(log.patches_by_scan[scan_id])
            replay.step()
            for record in log.io_submits_by_scan.get(scan_id, {}).values():
                for tag_name, value in record.tag_writes:
                    replay.apply_replay_io_write(tag_name, value)
            for record in log.io_drains_by_scan.get(scan_id, {}).values():
                for tag_name, value in record.tag_writes:
                    replay.apply_replay_io_write(tag_name, value)
            if log.io_submits_by_scan.get(scan_id) or log.io_drains_by_scan.get(scan_id):
                replay._materialize_replay_state()
            results.append(replay.current_state)
        return results

    def _replay_range(self, start_scan_id: int, end_scan_id: int) -> list[SystemState]:
        for changed_at, _plant in self._plant_changes:
            if start_scan_id < changed_at <= end_scan_id:
                return [
                    *self._replay_range(start_scan_id, changed_at - 1),
                    *self._replay_range(changed_at, end_scan_id),
                ]
        kernel = self._replay_kernel_from(start_scan_id)
        if kernel is None:
            return self._replay_range_interpreted(start_scan_id, end_scan_id)
        return self._replay_range_compiled(start_scan_id, end_scan_id, kernel)

    @property
    def simulation_time(self) -> float:
        """Current simulation clock in seconds."""
        return self._state.timestamp

    @property
    def dt(self) -> float:
        """Configured scan period in seconds."""
        return self._dt

    @property
    def time_mode(self) -> TimeMode:
        """Current time mode."""
        return self._time_mode

    @property
    def debug(self) -> _DebugNamespace:
        """Debugger-facing methods and internal runtime access."""
        return self._debug_ns

    @property
    def mode(self) -> Literal["run", "stop"]:
        """PLC mode: ``"run"`` or ``"stop"``."""
        return "run" if self._running else "stop"

    def start(self) -> None:
        """Transition STOP -> RUN now, without executing a scan.

        Performs the same transition the next ``step()`` would: non-retentive
        tags reset, ``scan_id`` and ``timestamp`` restart at 0, pending patches
        are dropped, and forces are cleared unless :attr:`keep_forces_on_run`.
        Patches and forces applied afterwards reach the first scan.  Does
        nothing in RUN mode.
        """
        self._ensure_running()

    @property
    def keep_forces_on_run(self) -> bool:
        """Whether active forces survive a STOP -> RUN transition.

        Off by default: entering RUN clears forces along with patches, like a
        fresh start.  When on, forces are kept (and apply from the first scan);
        tags and pending patches still reset.  ``reboot()`` clears forces
        either way.
        """
        return self._keep_forces_on_run

    @keep_forces_on_run.setter
    def keep_forces_on_run(self, value: bool) -> None:
        self._keep_forces_on_run = bool(value)

    def stop(self) -> None:
        """Transition PLC to STOP mode."""
        if not self._running:
            return
        self._running = False
        self._state = self._apply_runtime_memory_flags(
            self._state,
            mode_run=False,
            battery_present=self._battery_present,
        )
        self._record_lifecycle("stop")

    @property
    def battery_present(self) -> bool:
        """Simulated backup battery presence."""
        return self._battery_present

    @battery_present.setter
    def battery_present(self, value: bool) -> None:
        new_value = bool(value)
        if new_value == self._battery_present:
            return
        self._battery_present = new_value
        self._state = self._apply_runtime_memory_flags(
            self._state,
            mode_run=self._running,
            battery_present=self._battery_present,
        )
        self._record_lifecycle("battery_present", value=new_value)

    def reboot(self) -> SystemState:
        """Power-cycle the runner and return the reset state.

        Reboot is destructive: with a battery every tag keeps its value, and
        without one every tag returns to its default.  ``state.scan_id`` and
        ``state.timestamp`` return to 0.  Because post-reboot scan_ids
        would alias pre-reboot entries in every sparse channel
        (patches, forces, rtc_base_changes, dts), the scan log and
        checkpoints are reset to a fresh recording session rooted at
        the post-reboot scan 0.  Pre-reboot history is not
        replay-addressable — users who need that should ``fork()``
        before rebooting.
        """
        tag_values = self._rebuild_tags_for_reset(
            preserve_all=self._battery_present,
            preserve_retentive=False,
        )
        self._reset_runtime_scope(
            tag_values=tag_values,
            mode_run=True,
            preserve_rtc_continuity=self._battery_present,
        )
        self._running = True
        self._reset_recording()
        return self._state

    def _reset_recording(self) -> None:
        """Start a fresh recording rooted at scan 0, which a reset has just returned to.

        The scan ids to come would alias every sparse channel of the old log (patches,
        forces, rtc_base_changes, dts), so history replayed the old run's entries, and
        every state in a replay slab, so a seek returned the old run's state outright.
        """
        self._scan_log = ScanLog(time_mode=self._time_mode, base_scan=0)
        self._checkpoints = {}
        self._plant_changes = []
        self._epoch_caches.on_recording_reset()
        self._forces_last_recorded = {}
        self._this_scan_drained_patches = {}

    def set_rtc(self, value: datetime) -> None:
        """Set the current RTC value for the runner."""
        self._set_rtc_and_record(self._normalize_rtc_datetime(value), self._state.timestamp)

    def _set_time_mode(self, mode: TimeMode, *, dt: float = 0.010) -> None:
        """Set the time mode (internal, used by fork)."""
        self._time_mode = mode
        self._dt = dt
        if mode == TimeMode.REALTIME:
            self._last_step_time = time.perf_counter()
        # Reinitialize scan log so ``dts`` is present in REALTIME and absent
        # in FIXED_STEP.  Only called early in fork() so the log is empty
        # and no recorded history is lost.
        self._scan_log = ScanLog(time_mode=self._time_mode, base_scan=self._state.scan_id)
        self._checkpoints = {}
        self._epoch_caches.on_recording_reset()
        self._forces_last_recorded = dict(self._input_overrides.forces)

    @staticmethod
    def _apply_runtime_memory_flags(
        state: SystemState,
        *,
        mode_run: bool,
        battery_present: bool,
    ) -> SystemState:
        memory = state.memory.set(_MODE_RUN_KEY, bool(mode_run)).set(
            _BATTERY_PRESENT_KEY, bool(battery_present)
        )
        return state.set(memory=memory)

    def _refresh_known_tags_and_edges(self) -> frozenset[str]:
        from pyrung.core.system_points import _DERIVED_EDGE_TAGS

        walked = self._tags_rungs_reach.walk()
        for tag in walked.named:
            self._register_known_tag(tag)
        for tag in walked.beyond_rungs:
            self._register_tag_no_rung_gave(tag)
        return walked.edge_names - _DERIVED_EDGE_TAGS.keys()

    def _type_a_name_holds(self, name: str) -> TagType | None:
        """The type a patch or force by *name* stores, or None if it stores as given.

        A tag known only because something else named it, an override or a cached slot
        no rung reaches, doesn't change what the name means: the rungs say that.
        """
        known = self._known_tags_by_name.get(name)
        if known is None or name in self._names_no_rung_gave:
            return self._tags_rungs_reach.type_of(name)
        return known.type

    def _tag_known_or_reached(self, name: str) -> Tag | None:
        known = self._known_tags_by_name.get(name)
        return known if known is not None else self._tags_rungs_reach.get(name)

    def _register_tag_no_rung_gave(self, tag: Tag) -> None:
        no_rung = tag.name not in self._known_tags_by_name or tag.name in self._names_no_rung_gave
        self._register_known_tag(tag)
        if no_rung and tag.name not in SYSTEM_TAGS_BY_NAME:
            self._names_no_rung_gave.add(tag.name)

    def _register_known_tag(self, tag: Tag) -> None:
        if tag.name in SYSTEM_TAGS_BY_NAME:
            return
        self._names_no_rung_gave.discard(tag.name)
        existing = self._known_tags_by_name.get(tag.name)
        if existing is None:
            self._known_tags_by_name[tag.name] = tag
            return
        if (
            existing.type != tag.type
            or existing.retentive != tag.retentive
            or existing.default != tag.default
        ):
            raise ValueError(
                f"Conflicting tag metadata for {tag.name!r}: existing "
                f"(type={existing.type.name}, retentive={existing.retentive}, "
                f"default={existing.default!r}) vs new "
                f"(type={tag.type.name}, retentive={tag.retentive}, default={tag.default!r})."
            )

    def _register_known_tags_from_mapping_keys(
        self,
        tags: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
    ) -> None:
        from pyrung.core.tag import Tag as TagClass

        for key in tags:
            if isinstance(key, TagClass):
                self._register_tag_no_rung_gave(key)

    def _rebuild_tags_for_reset(
        self,
        *,
        preserve_all: bool,
        preserve_retentive: bool,
    ) -> dict[str, Any]:
        current_tags = self._state.tags
        rebuilt: dict[str, Any] = {}
        for tag in self._known_tags_by_name.values():
            if preserve_all:
                rebuilt[tag.name] = current_tags.get(tag.name, tag.default)
                continue
            if preserve_retentive and tag.retentive and tag.name in current_tags:
                rebuilt[tag.name] = current_tags[tag.name]
                continue
            rebuilt[tag.name] = tag.default
        self._tags_rungs_reach.kept_through_a_reset(
            current_tags,
            rebuilt,
            preserve_all=preserve_all,
            preserve_retentive=preserve_retentive,
        )
        # A plant is the machine's physics, not the controller's memory: nothing of it
        # survives a reset but the tags themselves, which start over at their defaults.
        return {**self._plant_tag_defaults(), **rebuilt}

    def _clear_retained_debug_trace_state(self) -> None:
        self._current_rung_traces = {}
        self._current_rung_traces_scan_id = None
        self._clear_inflight_debug_scan()
        self._latest_committed_trace_event = None

    def _clear_retained_debug_trace_caches(self) -> None:
        self._clear_retained_debug_trace_state()
        self._epoch_caches.on_replay_evidence_discarded()

    def _normalize_rtc_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone().replace(tzinfo=None)

    def _set_rtc_internal(self, value: datetime, sim_time: float) -> None:
        self._rtc_base = value
        self._rtc_base_sim_time = float(sim_time)

    def _set_rtc_and_record(
        self, value: datetime, sim_time: float, *, scan_id: int | None = None
    ) -> None:
        """Set RTC base and record the change for replay.

        Used for user-initiated (``set_rtc()``) and in-scan
        (``_apply_rtc_date/time`` via the system-points ``rtc_setter``
        callback) paths.  Internal lifecycle paths (reboot,
        stop-to-run) use ``_set_rtc_internal`` directly; the lifecycle
        event itself implies the RTC transition at replay time.

        *scan_id* is the scan the change belongs to, the next one by default.  A
        compiled range, which applied it some scans ago, says which.

        No-op in replay mode — the log's ``rtc_base_changes`` entry is
        load-bearing, and ``replay_to`` applies it via
        ``_set_rtc_internal`` directly.  Re-firing here would
        duplicate-record and (worse) shift the recorded scan_id.
        """
        if self._replay_mode:
            return
        previous = (self._rtc_base, self._rtc_base_sim_time)
        self._set_rtc_internal(value, sim_time)
        self._causal_lineage.invalidate_current_epoch()
        self._scan_log.record_rtc_base_change(
            self._state.scan_id + 1 if scan_id is None else scan_id,
            value,
            float(sim_time),
            previous=previous,
        )

    def _record_lifecycle(self, kind: LifecycleKind, value: bool | None = None) -> None:
        self._scan_log.record_lifecycle(
            LifecycleEvent(
                at_sim_time=float(self._state.timestamp),
                at_scan_id=self._state.scan_id + 1,
                kind=kind,
                value=value,
            )
        )

    def _rtc_at_sim_time(self, sim_time: float) -> datetime:
        return self._rtc_base + timedelta(seconds=float(sim_time) - self._rtc_base_sim_time)

    def _reset_runtime_scope(
        self,
        *,
        tag_values: Mapping[str, Any],
        mode_run: bool,
        preserve_rtc_continuity: bool = True,
    ) -> None:
        self._epoch_caches.on_runtime_scope_reset()
        rtc_after_reset = (
            self._rtc_at_sim_time(self._state.timestamp)
            if preserve_rtc_continuity
            else self._normalize_rtc_datetime(datetime.now())
        )
        next_state = SystemState().with_tags(dict(tag_values))
        self._state = self._apply_runtime_memory_flags(
            next_state,
            mode_run=mode_run,
            battery_present=self._battery_present,
        )
        self._set_rtc_internal(rtc_after_reset, self._state.timestamp)
        self._cache_state(self._state)
        self._initial_scan_id = self._state.scan_id
        self._initial_state = self._state
        self._causal_lineage = CausalLineage(self)
        self._history._reset_labels()
        self._playhead = self._state.scan_id

        self._pending_patches.clear()
        self._forces.clear()
        self._pause_requested_this_scan = False
        self._clear_retained_debug_trace_state()
        # Reboot drops the firing timelines together with the scan log
        # and checkpoints — Option B treats reboot like a fresh session
        # (see stage-4 notes in the design doc).
        self._rung_firing_timelines.reset()
        self._committed_tag_timelines.reset()
        self._node_firing_timelines.reset()
        self._unrecorded_scans = []
        self._causal_initial_tags = dict(self._state.tags)

        if self._time_mode == TimeMode.REALTIME:
            self._last_step_time = time.perf_counter()
        else:
            self._last_step_time = None

    def _stop_to_run_transition(self) -> None:
        if self._running:
            return
        kept_forces = dict(self._forces) if self._keep_forces_on_run else {}
        tag_values = self._rebuild_tags_for_reset(
            preserve_all=False,
            preserve_retentive=True,
        )
        self._reset_runtime_scope(
            tag_values=tag_values,
            mode_run=True,
            preserve_rtc_continuity=True,
        )
        self._reset_recording()
        self._forces.update(kept_forces)
        self._running = True

    def _requeue_drained_patches(self) -> None:
        drained = self._this_scan_drained_patches
        if not drained:
            return
        restored = {**drained, **self._pending_patches}
        self._pending_patches.clear()
        self._pending_patches.update(restored)
        self._this_scan_drained_patches = {}

    def _ensure_running(self) -> None:
        # A replay walks one recording, and a restart ends a recording: a stop inside
        # it scanned on until a later call, which is past the end of this log.
        if not self._running and not self._replay_mode:
            if self._search_refuses_restart:
                raise SearchStopped(self._state.scan_id)
            self._stop_to_run_transition()

    def _sync_runtime_flags_from_state(self) -> None:
        memory = self._state.memory
        self._running = bool(memory.get(_MODE_RUN_KEY, True))
        self._battery_present = bool(memory.get(_BATTERY_PRESENT_KEY, self._battery_present))

    def __enter__(self) -> PLC:
        """Bind this runner as active for live Tag.value access."""
        self._active_tokens.append(set_active_runner(self))
        return self

    def __exit__(self, *exc: object) -> None:
        if self._active_tokens:
            reset_active_runner(self._active_tokens.pop())

    def patch(
        self,
        tags: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
    ) -> None:
        """Queue tag values for next scan (one-shot).

        Values are applied at the start of the next step() call,
        then cleared. Use for momentary inputs like button presses.

        Args:
            tags: Dict of tag names or Tag objects to values.
        """
        self._register_known_tags_from_mapping_keys(tags)
        self._input_overrides.patch(tags)

    def patch_as_recorded(self, recorded: Mapping[str, bool | int | float | str]) -> None:
        """Queue a patch a scan log recorded: see ``InputOverrideManager.restore_patch``."""
        self._register_known_tags_from_mapping_keys(recorded)
        self._input_overrides.restore_patch(recorded)

    def force(self, tag: str | Tag, value: bool | int | float | str) -> None:
        """Persistently override a tag value until explicitly removed.

        The forced value is applied at the pre-logic force pass (phase 3) and
        re-applied at the post-logic force pass (phase 5) every scan.  Logic
        may temporarily diverge the value mid-scan, but the post-logic pass
        restores it before outputs are written.

        Forces persist across scans until `unforce()` or `clear_forces()`
        is called.  Multiple forces may be active simultaneously.

        If a tag is both patched and forced in the same scan, the force
        overwrites the patch during the pre-logic pass.

        Args:
            tag: Tag name or `Tag` object to override.
            value: Value to hold. Must be compatible with the tag's type.

        Raises:
            ValueError: If the tag is a read-only system point.
        """
        from pyrung.core.tag import Tag as TagClass

        if isinstance(tag, TagClass):
            self._register_tag_no_rung_gave(tag)
        self._input_overrides.add_force(tag, value)

    def unforce(self, tag: str | Tag) -> None:
        """Remove a single persistent force override.

        After removal the tag resumes its logic-computed value starting
        from the next scan.

        Args:
            tag: Tag name or `Tag` object whose force to remove.
        """
        self._input_overrides.remove_force(tag)

    def clear_forces(self) -> None:
        """Remove all active persistent force overrides.

        All forced tags resume their logic-computed values starting
        from the next scan.
        """
        had_forces = bool(self._input_overrides.forces)
        self._input_overrides.clear_forces()
        if had_forces:
            self._record_lifecycle("clear_forces")

    @contextmanager
    def forced(
        self,
        overrides: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
    ) -> Iterator[PLC]:
        """Temporarily apply forces for the duration of the context.

        On entry, saves the current force map and adds the given overrides.
        On exit (normally or on exception), the exact previous force map is
        restored — forces that existed before the context are reinstated, and
        forces added inside the context are removed.

        Safe for nesting: inner ``forced()`` contexts layer on top of outer ones
        without disrupting them.

        Args:
            overrides: Mapping of tag name / ``Tag`` object to forced value.

        Example:
            ```python
            with plc.forced({"AutoMode": True, "Fault": False}):
                plc.run(5)
            # AutoMode and Fault forces released here
            ```
        """
        self._register_known_tags_from_mapping_keys(overrides)
        with self._input_overrides.force(overrides):
            yield self

    @property
    def forces(self) -> Mapping[str, bool | int | float | str]:
        """Read-only view of active persistent overrides."""
        return self._input_overrides.forces

    @property
    def pending_patches(self) -> Mapping[str, bool | int | float | str]:
        """Read-only view of patches not yet in the committed state.

        Holds the patches queued for the next scan, plus any already taken
        by a scan still being stepped (``debug.scan_steps_debug()``) that has
        not committed.
        """
        drained = self._this_scan_drained_patches
        if not drained:
            return MappingProxyType(self._pending_patches)
        return MappingProxyType({**drained, **self._pending_patches})

    def monitor(self, tag: str | Tag, callback: Callable[[Any, Any], None]) -> _RunnerHandle:
        """Call ``callback(current, previous)`` after commit when tag value changes."""
        tag_name = self._normalize_tag_name(tag, method="monitor")
        monitor_id = self._next_handle_id()
        self._monitors_by_id[monitor_id] = _MonitorRegistration(
            id=monitor_id,
            tag_name=tag_name,
            callback=callback,
        )
        return _RunnerHandle(
            handle_id=monitor_id,
            remove=self._remove_monitor,
            enable=self._enable_monitor,
            disable=self._disable_monitor,
        )

    def when(
        self,
        *conditions: Condition
        | Tag
        | Callable[[SystemState], bool]
        | tuple[Condition | Tag, ...]
        | list[Condition | Tag],
    ) -> _BreakpointBuilder:
        """Create a breakpoint builder evaluated after each committed scan.

        Accepts ``Tag``/``Condition`` expressions (implicit AND) or a single
        callable predicate receiving ``SystemState``.
        """
        if self._is_fn_predicate(conditions):
            return _BreakpointBuilder(self, conditions[0])
        predicate = self._compile_condition_predicate(*conditions, method="when")  # ty: ignore[invalid-argument-type]
        return _BreakpointBuilder(self, predicate)

    @staticmethod
    def _is_fn_predicate(
        conditions: tuple[Any, ...],
    ) -> TypeGuard[tuple[Callable[[SystemState], bool]]]:
        """Return True if conditions is a single callable predicate (not a Tag/Condition)."""
        if len(conditions) != 1 or not callable(conditions[0]):
            return False
        from pyrung.core.condition import Condition as ConditionClass
        from pyrung.core.tag import Tag as TagClass

        return not isinstance(conditions[0], (TagClass, ConditionClass))

    def _compile_condition_predicate(
        self,
        *conditions: Condition | Tag | tuple[Condition | Tag, ...] | list[Condition | Tag],
        method: Literal["run_until", "when"],
    ) -> Callable[[SystemState], bool]:
        """Compile a Tag/Condition expression into a ``SystemState`` predicate."""
        if not conditions:
            raise TypeError(f"{method}() requires at least one condition")

        from pyrung.core.condition import _as_condition, _normalize_and_condition

        normalized = _normalize_and_condition(
            *conditions,
            coerce=_as_condition,
            empty_error=f"{method}() requires at least one condition",
            group_empty_error=f"{method}() condition group cannot be empty",
        )

        def _context(state: SystemState) -> ScanContext:
            return ScanContext(
                state,
                resolver=self._system_runtime.resolve,
                read_only_tags=self._system_runtime.read_only_tags,
            )

        from pyrung.core.site_memory import private_edges

        # An edge here is no contact of the program: it gets a key of its own
        # and compares against the bit as this predicate last judged it (as of
        # now, when armed), so rise(Y) holds on the first judgement after Y
        # turns on.
        condition, edges = private_edges(normalized, method)
        if not edges:
            return _ConditionPredicate(normalized, _context)

        return _EdgePredicate(condition, edges, _context, lambda: self._state)

    def _normalize_tag_name(self, tag: str | Tag, *, method: str) -> str:
        from pyrung.core.tag import Tag as TagClass

        if isinstance(tag, TagClass):
            return tag.name
        if isinstance(tag, str):
            return tag
        raise TypeError(f"{method}() tag must be str or Tag, got {type(tag).__name__}")

    def _next_handle_id(self) -> int:
        handle_id = self._next_debug_handle_id
        self._next_debug_handle_id += 1
        return handle_id

    def _remove_monitor(self, monitor_id: int) -> None:
        registration = self._monitors_by_id.get(monitor_id)
        if registration is None or registration.removed:
            return
        registration.removed = True
        registration.enabled = False

    def _enable_monitor(self, monitor_id: int) -> None:
        registration = self._monitors_by_id.get(monitor_id)
        if registration is None or registration.removed:
            return
        registration.enabled = True

    def _disable_monitor(self, monitor_id: int) -> None:
        registration = self._monitors_by_id.get(monitor_id)
        if registration is None or registration.removed:
            return
        registration.enabled = False

    def _register_breakpoint(
        self,
        *,
        predicate: Callable[[SystemState], bool],
        action: Literal["pause", "snapshot", "do"],
        label: str | None,
        callback: Callable[[SystemState], None] | None = None,
    ) -> _RunnerHandle:
        breakpoint_id = self._next_handle_id()
        if isinstance(predicate, _EdgePredicate):
            predicate = predicate.armed()
        self._breakpoints_by_id[breakpoint_id] = _BreakpointRegistration(
            id=breakpoint_id,
            predicate=predicate,
            action=action,
            label=label,
            callback=callback,
        )
        return _RunnerHandle(
            handle_id=breakpoint_id,
            remove=self._remove_breakpoint,
            enable=self._enable_breakpoint,
            disable=self._disable_breakpoint,
        )

    def _remove_breakpoint(self, breakpoint_id: int) -> None:
        registration = self._breakpoints_by_id.get(breakpoint_id)
        if registration is None or registration.removed:
            return
        registration.removed = True
        registration.enabled = False

    def _enable_breakpoint(self, breakpoint_id: int) -> None:
        registration = self._breakpoints_by_id.get(breakpoint_id)
        if registration is None or registration.removed:
            return
        if not registration.enabled and isinstance(registration.predicate, _EdgePredicate):
            registration.predicate.rearm()
        registration.enabled = True

    def _disable_breakpoint(self, breakpoint_id: int) -> None:
        registration = self._breakpoints_by_id.get(breakpoint_id)
        if registration is None or registration.removed:
            return
        registration.enabled = False

    def _note_unrecorded_scans(self, first: int, last: int) -> None:
        """These scans ran and left no records: a question that reaches them settles them."""
        windows = self._unrecorded_scans
        if windows and windows[-1][1] == first - 1:
            windows[-1] = (windows[-1][0], last)
        else:
            windows.append((first, last))

    def _record_landing(self, first_scan_id: int | None = None) -> None:
        """Cache the state a compiled run landed on, and checkpoint it if one is due.

        It is the exact state of its scan, so history may serve it, and a replay may
        anchor on it, as on any stepped scan.  *first_scan_id* is the first of the
        scans that ran with no records: a question that reaches them settles them.
        """
        state = self._state
        scan_id = state.scan_id
        if first_scan_id is not None and first_scan_id <= scan_id:
            self._note_unrecorded_scans(first_scan_id, scan_id)
        self._cache_state(state)
        if scan_id <= 0 or scan_id % self._checkpoint_interval != 0:
            return
        current_forces = dict(self._input_overrides.forces)
        self._scan_log.record_force_changes(scan_id, current_forces)
        self._forces_last_recorded = current_forces
        self._checkpoints[scan_id] = state

    def _consume_pause_request(self) -> bool:
        pause_requested = self._pause_requested_this_scan
        self._pause_requested_this_scan = False
        return pause_requested

    def _peek_live_tag_value(self, name: str, default: Any) -> Any:
        """Read a tag as seen by live Tag.value access."""
        has_override, value = self._input_overrides.get_live_override(name)
        if has_override:
            return value

        resolved, value = self._system_runtime.resolve(name, self._state)
        if resolved:
            return value

        return self._state.tags.get(name, default)

    def _calculate_dt(self) -> float:
        """Calculate scan delta time based on current time mode."""
        if self._dt_override_for_next_scan is not None:
            dt_override = self._dt_override_for_next_scan
            self._dt_override_for_next_scan = None
            return dt_override
        if self._time_mode == TimeMode.REALTIME:
            now = time.perf_counter()
            if self._last_step_time is None:
                self._last_step_time = now
            dt = now - self._last_step_time
            self._last_step_time = now
            return dt
        return self._dt

    def _prepare_scan(
        self,
        *,
        fast_reads: bool = False,
        synthesis_observer: ConditionViewCapture | None = None,
    ) -> tuple[ScanContext, float]:
        """Create and initialize scan context before logic evaluation."""
        replay_io = getattr(self, "_next_scan_replay_io", None)
        self._next_scan_replay_io = None
        state_tags_read: Mapping[str, Any] | None = None
        if fast_reads:
            if self._state_tags_read_cache_state is not self._state:
                self._state_tags_read_cache = dict(self._state.tags)
                self._state_tags_read_cache_state = self._state
            state_tags_read = self._state_tags_read_cache
        ctx = ScanContext(
            self._state,
            resolver=self._system_runtime.resolve,
            read_only_tags=self._system_runtime.read_only_tags,
            consumed_tags_getter=self._consumed_tags_for_capture,
            replay_io=replay_io,
            state_tags_read=state_tags_read,
        )
        if synthesis_observer is not None:
            # Start the selected historical journal before scan-start runtime,
            # patches, and pre-logic forces. Later synthesis and user runs nest
            # into this same root execution body.
            synthesis_observer.attach(ctx)

        # An attached observer records what the hook reads, so it runs the hook.
        if synthesis_observer is not None or not self._system_runtime.scan_start_is_quiet(
            ctx._state_tags_read, ctx._state_memory
        ):
            self._system_runtime.on_scan_start(ctx)

        dt = self._calculate_dt()
        if self._state.memory.get("_dt", _SENTINEL) != dt:
            ctx.set_memory("_dt", dt)
        if self._synthesis is not None:
            # ``plant`` (pre / input-read): read the PREVIOUS commit's settled
            # command and synthesize feedback as *this* scan's input image —
            # before patches drain and holds steer, so even a directly-patched
            # command lags one scan.  Feedback is an input, read here at the top;
            # the scan boundary is the plant latency.  dt is already in ctx, so
            # the TON/TOF timers ride the native dt knob.
            self._evaluate_synthesis(
                ctx,
                self._synthesis.plant,
                namespace="plant",
                observer=synthesis_observer,
            )
            # ``holds`` (pre / input-steer): PILOT pins inputs after the plant lays
            # down feedback but *before* the drain — canonical precedence
            # ``plant < holds < patches < forces`` (a patch overrides a hold, a
            # ``force`` is the hard pin applied in the pre/post force pass).
            self._evaluate_synthesis(
                ctx,
                self._synthesis.holds,
                namespace="PILOT",
                observer=synthesis_observer,
            )
        ctx._read_sink = None
        self._this_scan_drained_patches = self._input_overrides.apply_pre_scan(ctx)
        sites = self._randint_site_list()
        if sites:
            self._input_overrides.apply_randint_draws(ctx, sites, self._this_scan_drained_patches)
        return ctx, dt

    def _randint_site_list(self) -> tuple[RandIntExpr, ...]:
        """The program's ``randint`` sites, from its known tags."""
        count = len(self._known_tags_by_name)
        cached = self._randint_sites_cache
        if cached is None or cached[0] != count:
            cached = (count, randint_sites(self._known_tags_by_name))
            self._randint_sites_cache = cached
        return cached[1]

    def _evaluate_synthesis(
        self,
        ctx: ScanContext,
        rungs: list[Rung],
        *,
        namespace: str,
        observer: ConditionViewCapture | None = None,
    ) -> None:
        """Evaluate synthesis bracket rungs in their own condition scope.

        The brackets are scanned like a subroutine body: a fresh condition
        scope, writes batched into the same ctx/commit as the user rungs.  Each
        entry records under a synthetic ``RungId`` in the node timeline, leaving
        the public integer main-rung timeline unchanged.
        """
        if not rungs:
            return
        saved_scope = ctx._condition_scope_token
        ctx._condition_scope_token = object()
        try:
            for rung_index, rung in enumerate(rungs):
                rung_id = RungId(namespace, rung_index)
                with ctx.capturing_node(rung_id, retain_all_writes=True):
                    if observer is not None and self._program is not None:
                        execute_observed_rung(
                            self._program,
                            ctx,
                            rung_index,
                            rung,
                            observer=observer,
                            namespace=namespace,
                        )
                    else:
                        rung.evaluate(ctx)
        finally:
            ctx._condition_scope_token = saved_scope

    def _commit_scan(
        self,
        ctx: ScanContext,
        dt: float,
        *,
        before_callbacks: Callable[[], None] | None = None,
    ) -> None:
        """Finalize one scan and commit all batched writes.

        Rung firings are read from ``ctx.rung_firings`` — both the non-debug
        and debug scan paths populate it via ``ctx.capturing_rung(i)`` around
        each top-level rung evaluation.  Scans with no firings (e.g. manual
        commits from tests) simply record nothing.
        """
        previous_state = self._state
        previous_tip_scan_id = previous_state.scan_id
        self._input_overrides.apply_post_logic(ctx)

        self._system_runtime.on_scan_end(ctx)
        if self._constrained_tags:
            self._bounds_violations = check_bounds(ctx._tags_pending, self._constrained_tags)
            for v in self._bounds_violations.values():
                warnings.warn(str(v), stacklevel=2)
        else:
            self._bounds_violations = {}
        new_state = self._state = ctx.commit(dt=dt)
        new_scan_id = new_state.scan_id
        # Record the actual committed boundary, not attempted rung writes. This
        # is the cheap exactness index consumed by cause(): later same-scan
        # writers, synthesis, and forces have already won before this point.
        # A commit stores each pending value or keeps one it stores identically
        # (same_stored_value), so the pending dict says what the state holds.
        pending = ctx._tags_pending
        entry_tags = ctx._state_tags_read if ctx._state is previous_state else previous_state.tags
        for name, after in pending.items():
            if entry_tags.get(name) != after:
                self._committed_tag_timelines.append(name, new_scan_id, {name: after})
        if (
            self._state_tags_read_cache is not None
            and ctx._state_tags_read is self._state_tags_read_cache
        ):
            self._state_tags_read_cache.update(pending)
            self._state_tags_read_cache_state = new_state
        # Replay recorder: capture nondeterminism for this scan.
        # Checkpoint bypass: the force-map write at checkpoint boundaries
        # is unconditional — replay reads force state from the checkpoint
        # scan's log entry, so diff-eliding it would strand reconstruction.
        is_checkpoint = new_scan_id > 0 and new_scan_id % self._checkpoint_interval == 0
        if self._this_scan_drained_patches:
            self._scan_log.record_patches(new_scan_id, self._this_scan_drained_patches)
            self._this_scan_drained_patches = {}
        current_forces = dict(self._input_overrides.forces)
        if is_checkpoint or current_forces != self._forces_last_recorded:
            self._scan_log.record_force_changes(new_scan_id, current_forces)
            self._forces_last_recorded = current_forces
        if is_checkpoint:
            self._checkpoints[new_scan_id] = self._state
        if self._scan_log.records_dt:
            self._scan_log.record_dt(new_scan_id, dt)
        if ctx._io_submit_staging:
            for key, record in ctx._io_submit_staging.items():
                self._scan_log.record_io_submit(new_scan_id, key, record)
        if ctx._io_drain_staging:
            for key, record in ctx._io_drain_staging.items():
                self._scan_log.record_io_drain(new_scan_id, key, record)
        # Only firing rungs append. Their value columns independently extend
        # stable, alternating, or arithmetic ranges; varied=true is sparse.
        new_firings = ctx._rung_firings
        for rung_index, writes in new_firings.items():
            self._rung_firing_timelines.append(
                rung_index,
                new_scan_id,
                writes,
                ctx._rung_firing_varied.get(rung_index, frozenset()),
            )
        node_firings = ctx._node_firings
        for rung_id, writes in node_firings.items():
            self._node_firing_timelines.append(
                rung_id,
                new_scan_id,
                writes,
                ctx._node_firing_varied.get(rung_id, frozenset()),
            )
        # Rung traces are per-commit, not per-history. The debug path
        # repopulates _current_rung_traces after commit_scan returns; any
        # other commit path leaves the slot empty for this scan.
        if self._current_rung_traces_scan_id != self._state.scan_id:
            self._current_rung_traces = {}
            self._current_rung_traces_scan_id = None
            self._latest_committed_trace_event = None
        self._cache_state(self._state)

        if is_checkpoint:
            self._apply_history_retention(new_scan_id)

        # Keep playhead following newest state unless manually moved.
        if self._playhead == previous_tip_scan_id:
            self._playhead = self._state.scan_id

        if before_callbacks is not None:
            before_callbacks()
        if not self._replay_mode:
            self._evaluate_monitors(previous_state=previous_state, current_state=self._state)
            self._evaluate_breakpoints(state=self._state)
        self._sync_runtime_flags_from_state()

    def _apply_history_retention(self, new_scan_id: int) -> None:
        """Retention-policy auto-trim, piggybacked on checkpoint cadence."""
        if new_scan_id % self._checkpoint_interval != 0 or new_scan_id <= 0:
            return
        if (
            self._history_retention_scans is not None
            and new_scan_id > self._history_retention_scans
        ):
            horizon = new_scan_id - self._history_retention_scans
            surviving_cp = self._nearest_checkpoint_at_or_after(horizon)
            if surviving_cp is not None:
                self._trim_history_before(surviving_cp)
            total = (
                self._recent_state_cache_bytes
                + sum(_estimate_state_bytes(s) for s in self._checkpoints.values())
                + self._scan_log.bytes_estimate()
            )
            if total > self._recent_state_cache_budget:
                extra_horizon = horizon + self._checkpoint_interval
                extra_cp = self._nearest_checkpoint_at_or_after(extra_horizon)
                if extra_cp is not None:
                    self._trim_history_before(extra_cp)

    def _evaluate_monitors(
        self, *, previous_state: SystemState, current_state: SystemState
    ) -> None:
        for monitor_id in sorted(self._monitors_by_id):
            registration = self._monitors_by_id[monitor_id]
            if registration.removed or not registration.enabled:
                continue

            previous_value = previous_state.tags.get(registration.tag_name)
            current_value = current_state.tags.get(registration.tag_name)
            if current_value != previous_value:
                registration.callback(current_value, previous_value)

    def _evaluate_breakpoints(self, *, state: SystemState) -> None:
        self._pause_requested_this_scan = False
        for breakpoint_id in sorted(self._breakpoints_by_id):
            registration = self._breakpoints_by_id[breakpoint_id]
            if registration.removed or not registration.enabled:
                continue

            if not registration.predicate(state):
                continue

            if registration.action == "snapshot":
                assert registration.label is not None
                self._history._label_scan(
                    registration.label,
                    state.scan_id,
                    metadata=self._snapshot_metadata_for_state(state),
                )
                continue

            if registration.action == "do":
                assert registration.callback is not None
                registration.callback(state)
                continue

            self._pause_requested_this_scan = True

    def _snapshot_metadata_for_state(self, state: SystemState) -> dict[str, Any]:
        rtc_now = self._rtc_at_sim_time(state.timestamp)
        wall_now = self._normalize_rtc_datetime(datetime.now())
        return {
            "rtc_iso": rtc_now.isoformat(),
            "rtc_offset_seconds": float((rtc_now - wall_now).total_seconds()),
        }

    def _scan_steps(self) -> Generator[tuple[int, Rung, ScanContext], None, None]:
        """Execute one scan cycle and yield after each rung evaluation.

        Scan phases:
        1. Create ScanContext from current state
        2. Apply pending patches to context
        3. Apply persistent force overrides (pre-logic)
        4. Calculate dt and inject into context
        5. Evaluate all logic (writes batched in context), yielding after each rung
        6. Re-apply force overrides (post-logic)
        7. Commit all changes in single operation

        Edge contacts store their own memory while their rows are scanned
        (``_edge:e{N}``, see :mod:`pyrung.core.site_memory`).

        The commit in phase 7 only happens when the generator is exhausted.
        """
        self._ensure_running()
        ctx, dt = self._prepare_scan()

        for i, rung in enumerate(self._logic):
            with ctx.capturing_rung(i):
                rung.evaluate(ctx)
            yield i, rung, ctx

        self._commit_scan(ctx, dt)

    def _scan_steps_debug(self) -> Generator[ScanStep, None, None]:
        """Execute one scan cycle and yield ``ScanStep`` objects at all boundaries.

        Yields a `ScanStep` at each:

        - Top-level rung boundary (``kind="rung"``)
        - Branch entry / exit (``kind="branch"``)
        - Subroutine call and body steps (``kind="subroutine"``)
        - Individual instruction boundaries (``kind="instruction"``)

        Each `ScanStep` carries source location metadata (``source_file``,
        ``source_line``, ``end_line``), rung enable state, and a trace of
        evaluated conditions and instructions.

        This is the API used by the DAP adapter via ``plc.debug.scan_steps_debug()``.
        Prefer ``_scan_steps()`` for non-debug consumers — it has less overhead
        and a simpler yield type.

        Note:
            The scan is committed only when the generator is **fully exhausted**.
        """
        self._ensure_running()
        pending_scan_id = self._state.scan_id + 1
        events_by_rung: dict[int, list[RungTraceEvent]] = {}
        self._start_inflight_debug_scan(scan_id=pending_scan_id, events_by_rung=events_by_rung)

        try:
            for step in self._debugger.scan_steps_debug(self._debug_ns):
                event = self._rung_trace_event_from_step(step)
                events_by_rung.setdefault(step.rung_index, []).append(event)
                self._latest_inflight_trace_event = (pending_scan_id, step.rung_index, event)
                yield step

            scan_id = self._state.scan_id
            self._current_rung_traces = {
                rung_id: RungTrace(scan_id=scan_id, rung_id=rung_id, events=tuple(events))
                for rung_id, events in sorted(events_by_rung.items())
            }
            self._current_rung_traces_scan_id = scan_id
            if self._latest_inflight_trace_event is not None:
                _inflight_scan_id, latest_rung_id, latest_event = self._latest_inflight_trace_event
                self._latest_committed_trace_event = (scan_id, latest_rung_id, latest_event)
        finally:
            self._clear_inflight_debug_scan()

    def _start_inflight_debug_scan(
        self,
        *,
        scan_id: int,
        events_by_rung: dict[int, list[RungTraceEvent]],
    ) -> None:
        self._inflight_scan_id = scan_id
        self._inflight_rung_events = events_by_rung
        self._latest_inflight_trace_event = None

    def _clear_inflight_debug_scan(self) -> None:
        self._inflight_scan_id = None
        self._inflight_rung_events = {}
        self._latest_inflight_trace_event = None

    def _rung_trace_event_from_step(self, step: ScanStep) -> RungTraceEvent:
        return RungTraceEvent(
            kind=step.kind,
            source_file=step.source_file,
            source_line=step.source_line,
            end_line=step.end_line,
            subroutine_name=step.subroutine_name,
            depth=step.depth,
            call_stack=step.call_stack,
            enabled_state=step.enabled_state,
            instruction_kind=step.instruction_kind,
            trace=step.trace,
            branch_path=step.branch_path,
        )

    def _evaluate_condition_value(
        self,
        condition: Any,
        ctx: ScanContext | ConditionView,
    ) -> tuple[bool, list[dict[str, Any]]]:
        return self._condition_trace.evaluate(condition, ctx)

    def _condition_term_text(self, condition: Any, details: list[dict[str, Any]]) -> str:
        return self._condition_trace.summary(condition, details)

    def _condition_annotation(self, *, status: str, expression: str, summary: str) -> str:
        return self._condition_trace.annotation(
            status=status,
            expression=expression,
            summary=summary,
        )

    def _condition_expression(self, condition: Any) -> str:
        return self._condition_trace.expression(condition)

    def step(self) -> SystemState:
        """Execute one full scan cycle and return the committed state."""
        self._ensure_running()
        return self._run_single_scan(consume_pause_request=True)

    def _run_single_scan(
        self,
        *,
        consume_pause_request: bool,
        capture_execution: bool = False,
        execution_capture: ConditionViewCapture | None = None,
        capture_sink: Callable[[int, ConditionViewCapture], None] | None = None,
    ) -> SystemState:
        self._epoch_caches.on_tip_advanced()
        if capture_execution and execution_capture is not None:
            raise ValueError("provide capture_execution or execution_capture, not both")
        capture = execution_capture
        if capture is None and capture_execution and self._program is not None:
            capture = ConditionViewCapture()
        ctx, dt = self._prepare_scan(
            fast_reads=True,
            synthesis_observer=capture,
        )
        if self._program is not None:
            if capture is None:
                execute_program(self._program, ctx, capture_rungs=True)
            else:
                execute_program(
                    self._program,
                    ctx,
                    capture_rungs=True,
                    observer=capture,
                )
        else:
            for i, rung in enumerate(self._logic):
                journal = ctx._begin_capture()
                try:
                    rung.evaluate(ctx)
                finally:
                    ctx._finish_rung_capture(i, journal)

        def _retain_capture_before_callbacks() -> None:
            assert capture is not None
            capture.exit_tags = self._state.tags
            if capture_sink is not None:
                capture_sink(self._state.scan_id, capture)
            else:
                self._cached_replay_captures[self._state.scan_id] = capture
                self._cached_replay_captures.move_to_end(self._state.scan_id)
                while len(self._cached_replay_captures) > _REPLAY_CAPTURE_CACHE_SCANS:
                    self._cached_replay_captures.popitem(last=False)

        self._commit_scan(
            ctx,
            dt,
            before_callbacks=(_retain_capture_before_callbacks if capture is not None else None),
        )

        if consume_pause_request:
            self._consume_pause_request()
        return self._state

    def run(self, cycles: int) -> SystemState:
        """Execute up to ``cycles`` scans, stopping early on pause breakpoints.

        Args:
            cycles: Number of scans to execute.

        Returns:
            The final SystemState after all cycles.
        """
        self._ensure_running()
        for _ in range(cycles):
            self._consume_pause_request()
            self._run_single_scan(consume_pause_request=False)
            if self._consume_pause_request():
                break
        return self._state

    def run_for(self, seconds: float, *, jit: bool = True) -> SystemState:
        """Run until simulation time advances by N seconds or a pause breakpoint fires.

        Args:
            seconds: Minimum simulation time to advance.
            jit: When True (default), a long run goes through the compiled kernel
                and its guarded loop, which run every scan without recording
                causal timelines. False steps every scan interpreted.

        Returns:
            The final SystemState after reaching the target time.
        """
        self._ensure_running()
        if jit and self._logic and self._time_mode == TimeMode.FIXED_STEP:
            from pyrung.core.compiled_run import _stepped_scans_until, run_scans

            scans = _stepped_scans_until(
                self._state.timestamp,
                self._state.timestamp + seconds,
                self._dt,
                max_scans=_RUN_FOR_MAX_COUNTED_SCANS,
            )
            if scans is not None:
                return run_scans(self, scans) if scans else self._state
        target_time = self._state.timestamp + seconds
        while self._state.timestamp < target_time:
            self._consume_pause_request()
            self._run_single_scan(consume_pause_request=False)
            if self._consume_pause_request():
                break
        return self._state

    def run_until(
        self,
        *conditions: Condition
        | Tag
        | Callable[[SystemState], bool]
        | tuple[Condition | Tag, ...]
        | list[Condition | Tag],
        max_cycles: int = 10000,
        jit: bool = True,
    ) -> SystemState:
        """Run until condition is true, pause breakpoint fires, or max_cycles reached.

        Accepts ``Tag``/``Condition`` expressions (implicit AND) or a single
        callable predicate receiving ``SystemState``.

        Args:
            conditions: Condition expressions or a single callable predicate.
            max_cycles: Maximum scans before giving up (default 10000).
            jit: When True (default), a long run goes through the compiled kernel
                and its guarded loop, which run every scan without recording
                causal timelines. False, or a callable predicate, steps every scan
                interpreted.

        Returns:
            The state that matched the condition, or final state if max reached.
        """
        opaque = self._is_fn_predicate(conditions)
        if opaque:
            predicate = conditions[0]
        else:
            predicate = self._compile_condition_predicate(*conditions, method="run_until")  # ty: ignore[invalid-argument-type]
        self._ensure_running()
        if jit and self._logic and self._time_mode == TimeMode.FIXED_STEP:
            from pyrung.core.compiled_run import run_scans

            if max_cycles <= 0:
                return self._state
            # A callable may read anything, the runner included: it steps.
            return run_scans(self, max_cycles, predicate, compiled_predicate=not opaque)
        for _ in range(max_cycles):
            self._consume_pause_request()
            self._run_single_scan(consume_pause_request=False)
            pause_requested = self._consume_pause_request()
            if predicate(self._state) or pause_requested:
                break
        return self._state
