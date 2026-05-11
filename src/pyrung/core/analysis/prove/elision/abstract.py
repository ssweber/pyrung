"""Abstract provenance analysis for state-key elision."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import product
from typing import TYPE_CHECKING, Any, cast

from pyrung.core.analysis.pdg import ProgramGraph, _extract_tag_names, _implicit_fault_writes
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.coils import LatchInstruction, OutInstruction, ResetInstruction
from pyrung.core.instruction.control import CallInstruction, ForLoopInstruction, ReturnInstruction
from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction
from pyrung.core.memory_block import BlockRange, IndirectBlockRange, IndirectExprRef, IndirectRef
from pyrung.core.tag import ImmediateRef, Tag, TagType

from ..absorb import _all_write_targets
from ..results import PENDING

if TYPE_CHECKING:
    from pyrung.core.condition import Condition
    from pyrung.core.context import ConditionView, ScanContext
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung

    from . import _ElisionContext

_EXPR_ENUM_LIMIT = 128

_PRIMITIVE_TYPES = {bool, int, float, str, bytes, bytearray}
_NUMERIC_PRIMITIVE_TYPES = {bool, int, float}


_NO_CONST = object()


@dataclass(frozen=True, slots=True)
class _ConstEntry:
    """Constant entry summary — safe to materialize at a future scan entry."""

    value: Any


@dataclass(frozen=True, slots=True)
class _UnavailableEntry:
    """Tag may be elidable for same-scan purposes, but its future scan-entry
    value is not reconstructible from retained state."""


_EntrySummary = _ConstEntry | _UnavailableEntry

_UNAVAILABLE_ENTRY = _UnavailableEntry()


@dataclass(frozen=True, slots=True)
class _AbsValue:
    """Abstract provenance/value summary for one tag or expression."""

    const: Any = _NO_CONST
    depends_on_retained: bool = False
    depends_on_inputs: bool = False
    depends_on_entry: bool = False
    unknown: bool = False

    @property
    def is_const(self) -> bool:
        return (
            self.const is not _NO_CONST
            and not self.depends_on_retained
            and not self.depends_on_inputs
            and not self.depends_on_entry
            and not self.unknown
        )

    @property
    def same_scan_safe(self) -> bool:
        return not self.depends_on_entry and not self.unknown

    @property
    def is_canonical(self) -> bool:
        return not self.depends_on_inputs and not self.depends_on_entry and not self.unknown

    def as_entry_summary(self) -> _EntrySummary:
        """Summary to use as the next scan's entry value for accepted tags.

        Only constants are reusable across scan boundaries.  Any non-constant
        exit — even if canonical/retained-derived — is phase-local only.
        """
        if self.is_const:
            return _ConstEntry(self.const)
        return _UNAVAILABLE_ENTRY


_CONST_FALSE = _AbsValue(const=False)

_CONST_TRUE = _AbsValue(const=True)

_RETAINED_VALUE = _AbsValue(depends_on_retained=True)

_INPUT_VALUE = _AbsValue(depends_on_inputs=True)

_ENTRY_VALUE = _AbsValue(depends_on_entry=True)

_UNKNOWN_VALUE = _AbsValue(unknown=True)

_ZERO_VALUE = _AbsValue(const=0)


def _edge_source_tags(program: Program) -> frozenset[str]:
    from pyrung.core.analysis.simplified import And, Atom, Or, _condition_to_expr

    result: set[str] = set()

    def _walk_expr(expr: Any) -> None:
        if isinstance(expr, Atom):
            if expr.form in {"rise", "fall"}:
                result.add(expr.tag)
            return
        if isinstance(expr, (And, Or)):
            for term in expr.terms:
                _walk_expr(term)

    def _walk_rung(rung: Rung) -> None:
        for condition in rung._conditions:
            _walk_expr(_condition_to_expr(condition))
        for branch in rung._branches:
            _walk_rung(branch)

    for rung in program.rungs:
        _walk_rung(rung)
    for rungs in program.subroutines.values():
        for rung in rungs:
            _walk_rung(rung)

    return frozenset(result)


def _dep_union(*values: _AbsValue) -> _AbsValue:
    retained = False
    inputs = False
    entry = False
    unknown = False
    for v in values:
        retained = retained or v.depends_on_retained
        inputs = inputs or v.depends_on_inputs
        entry = entry or v.depends_on_entry
        unknown = unknown or v.unknown
    return _AbsValue(
        depends_on_retained=retained,
        depends_on_inputs=inputs,
        depends_on_entry=entry,
        unknown=unknown,
    )


def _merge_values(a: _AbsValue, b: _AbsValue, guard_dep: _AbsValue | None = None) -> _AbsValue:
    if a is b or a == b:
        return a
    return _dep_union(a, b, guard_dep or _ZERO_VALUE)


@dataclass(slots=True)
class _AbstractState:
    """Sparse abstract tag state layered on a fixed base mapping."""

    base: Mapping[str, _AbsValue]
    overrides: dict[str, _AbsValue]

    def copy(self) -> _AbstractState:
        return _AbstractState(self.base, dict(self.overrides))

    def get(self, name: str) -> _AbsValue:
        if name in self.overrides:
            return self.overrides[name]
        return self.base.get(name, _UNKNOWN_VALUE)

    def set(self, name: str, value: _AbsValue) -> None:
        base_value = self.base.get(name, _UNKNOWN_VALUE)
        if value is base_value or value == base_value:
            self.overrides.pop(name, None)
            return
        self.overrides[name] = value


@dataclass(frozen=True, slots=True)
class _ExecutionResult:
    continue_state: _AbstractState | None
    returned_state: _AbstractState | None = None
    returned_dep: _AbsValue | None = None


@dataclass(frozen=True, slots=True)
class _ElidedSummary:
    exit_value: _AbsValue
    entry_summary: _EntrySummary


@dataclass(frozen=True, slots=True)
class _CandidateRun:
    same_scan_safe: bool
    saw_entry_read: bool
    saw_unknown_read: bool
    exit_value: _AbsValue


def _merge_states(
    left: _AbstractState | None,
    right: _AbstractState | None,
    guard_dep: _AbsValue | None = None,
) -> _AbstractState | None:
    if left is None:
        return right.copy() if right is not None else None
    if right is None:
        return left.copy()

    merged = _AbstractState(left.base, {})
    keys = set(left.overrides) | set(right.overrides)
    for name in keys:
        value = _merge_values(left.get(name), right.get(name), guard_dep)
        merged.set(name, value)
    return merged


def _merge_return_paths(
    current_state: _AbstractState | None,
    current_dep: _AbsValue | None,
    new_state: _AbstractState | None,
    new_dep: _AbsValue | None,
) -> tuple[_AbstractState | None, _AbsValue | None]:
    if new_state is None:
        return current_state, current_dep
    if current_state is None:
        return new_state.copy(), new_dep
    dep = _dep_union(current_dep or _ZERO_VALUE, new_dep or _ZERO_VALUE)
    return _merge_states(current_state, new_state, dep), dep


class _ExactContext:
    """Exact evaluator context for const-only expression/condition evaluation."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = values

    def get_tag(self, name: str, default: Any = None) -> Any:
        return self._values.get(name, default)

    def get_memory(self, _name: str, default: Any = None) -> Any:
        return default

    @property
    def scan_id(self) -> int:
        return 0


class _TagElisionCheck:
    """One candidate-check run under a fixed retained/accepted environment."""

    def __init__(
        self,
        program: Program,
        graph: ProgramGraph,
        known_domains: Mapping[str, tuple[Any, ...]],
        nondeterministic_names: frozenset[str],
        retained: frozenset[str],
        accepted: Mapping[str, _ElidedSummary],
        candidate: str,
        candidate_entry: _AbsValue,
        read_names_cache: dict[int, tuple[str, ...]],
        tag_refs: dict[str, Tag],
    ) -> None:
        self._program = program
        self._graph = graph
        self._known_domains = known_domains
        self._nondeterministic_names = nondeterministic_names
        self._retained = retained
        self._accepted = accepted
        self._candidate = candidate
        self._candidate_entry = candidate_entry
        self._saw_entry_read = False
        self._saw_unknown_read = False
        self._read_names_cache = read_names_cache
        self._tag_refs = tag_refs
        self._base = self._build_base_state()

    def run(self) -> _CandidateRun:
        start = _AbstractState(self._base, {})
        result = self._execute_rungs(self._program.rungs, start)
        final_state = result.continue_state
        if final_state is None:
            final_state = result.returned_state
        elif result.returned_state is not None:
            final_state = _merge_states(final_state, result.returned_state, result.returned_dep)
        if final_state is None:
            final_state = start
        exit_value = final_state.get(self._candidate)
        same_scan_safe = not self._saw_entry_read and not self._saw_unknown_read
        return _CandidateRun(
            same_scan_safe=same_scan_safe,
            saw_entry_read=self._saw_entry_read,
            saw_unknown_read=self._saw_unknown_read,
            exit_value=exit_value,
        )

    def _build_base_state(self) -> dict[str, _AbsValue]:
        base: dict[str, _AbsValue] = {}
        written = frozenset(self._graph.writers_of)
        for name, tag in self._graph.tags.items():
            if name == self._candidate:
                base[name] = self._candidate_entry
            elif name in self._accepted:
                summary = self._accepted[name].entry_summary
                if type(summary) is _ConstEntry:
                    base[name] = _AbsValue(const=summary.value)
                else:
                    base[name] = _ENTRY_VALUE
            elif name in self._retained:
                base[name] = _RETAINED_VALUE
            elif (
                name in self._nondeterministic_names
                or (tag.external and name not in written)
                or self._graph.is_physical_input(name)
            ):
                base[name] = _INPUT_VALUE
            elif name in written:
                base[name] = _ENTRY_VALUE
            else:
                base[name] = _AbsValue(const=tag.default)
        return base

    def _execute_rungs(
        self,
        rungs: list[Rung],
        state: _AbstractState,
    ) -> _ExecutionResult:
        current_state: _AbstractState | None = state
        prev_snapshot: _AbstractState = state
        returned_state: _AbstractState | None = None
        returned_dep: _AbsValue | None = None

        for rung in rungs:
            if current_state is None:
                break
            if getattr(rung, "_use_prior_snapshot", False):
                snapshot = prev_snapshot
            else:
                snapshot = current_state
            prev_snapshot = current_state
            rung_result = self._execute_rung(rung, current_state, snapshot, enabled=None)
            returned_state, returned_dep = _merge_return_paths(
                returned_state,
                returned_dep,
                rung_result.returned_state,
                rung_result.returned_dep,
            )
            current_state = rung_result.continue_state

        return _ExecutionResult(current_state, returned_state, returned_dep)

    def _execute_rung(
        self,
        rung: Rung,
        state: _AbstractState,
        snapshot_state: _AbstractState,
        *,
        enabled: bool | None,
    ) -> _ExecutionResult:
        if enabled is None:
            guard = self._eval_conditions(getattr(rung, "_conditions", ()), snapshot_state)
        else:
            guard = _CONST_TRUE if enabled else _CONST_FALSE

        if guard.is_const:
            return self._execute_rung_body(
                rung,
                state,
                snapshot_state,
                enabled=bool(guard.const),
            )

        enabled_result = self._execute_rung_body(rung, state.copy(), snapshot_state, enabled=True)
        disabled_result = self._execute_rung_body(rung, state.copy(), snapshot_state, enabled=False)
        guard_dep = _dep_union(guard)

        continue_state = _merge_states(
            enabled_result.continue_state,
            disabled_result.continue_state,
            guard_dep,
        )

        returned_state: _AbstractState | None = None
        returned_dep: _AbsValue | None = None
        if enabled_result.returned_state is not None:
            dep = _dep_union(enabled_result.returned_dep or _ZERO_VALUE, guard_dep)
            returned_state, returned_dep = _merge_return_paths(
                returned_state,
                returned_dep,
                enabled_result.returned_state,
                dep,
            )
        if disabled_result.returned_state is not None:
            dep = _dep_union(disabled_result.returned_dep or _ZERO_VALUE, guard_dep)
            returned_state, returned_dep = _merge_return_paths(
                returned_state,
                returned_dep,
                disabled_result.returned_state,
                dep,
            )

        return _ExecutionResult(continue_state, returned_state, returned_dep)

    def _execute_rung_body(
        self,
        rung: Rung,
        state: _AbstractState,
        snapshot_state: _AbstractState,
        *,
        enabled: bool,
    ) -> _ExecutionResult:
        branch_guards: dict[int, _AbsValue] = {}
        for item in getattr(rung, "_execution_items", ()):
            if hasattr(item, "_branch_condition_start"):
                local_conds = item._conditions[item._branch_condition_start :]
                branch_guard = self._eval_conditions(local_conds, snapshot_state)
                if not enabled:
                    branch_guards[id(item)] = _CONST_FALSE
                elif branch_guard.is_const:
                    branch_guards[id(item)] = (
                        _CONST_TRUE if bool(branch_guard.const) else _CONST_FALSE
                    )
                else:
                    branch_guards[id(item)] = branch_guard

        current = state
        returned_state: _AbstractState | None = None
        returned_dep: _AbsValue | None = None

        for item in getattr(rung, "_execution_items", ()):
            if hasattr(item, "_branch_condition_start"):
                branch_guard = branch_guards.get(id(item), _CONST_FALSE)
                if branch_guard.is_const:
                    branch_result = self._execute_rung(
                        item,
                        current,
                        snapshot_state,
                        enabled=bool(branch_guard.const),
                    )
                else:
                    branch_true = self._execute_rung(
                        item,
                        current.copy(),
                        snapshot_state,
                        enabled=True,
                    )
                    branch_false = self._execute_rung(
                        item,
                        current.copy(),
                        snapshot_state,
                        enabled=False,
                    )
                    guard_dep = _dep_union(branch_guard)
                    continue_state = _merge_states(
                        branch_true.continue_state,
                        branch_false.continue_state,
                        guard_dep,
                    )
                    branch_returned: _AbstractState | None = None
                    branch_returned_dep: _AbsValue | None = None
                    if branch_true.returned_state is not None:
                        dep = _dep_union(branch_true.returned_dep or _ZERO_VALUE, guard_dep)
                        branch_returned, branch_returned_dep = _merge_return_paths(
                            branch_returned,
                            branch_returned_dep,
                            branch_true.returned_state,
                            dep,
                        )
                    if branch_false.returned_state is not None:
                        dep = _dep_union(branch_false.returned_dep or _ZERO_VALUE, guard_dep)
                        branch_returned, branch_returned_dep = _merge_return_paths(
                            branch_returned,
                            branch_returned_dep,
                            branch_false.returned_state,
                            dep,
                        )
                    branch_result = _ExecutionResult(
                        continue_state,
                        branch_returned,
                        branch_returned_dep,
                    )
            else:
                branch_result = self._execute_instruction(item, current, enabled=enabled)

            returned_state, returned_dep = _merge_return_paths(
                returned_state,
                returned_dep,
                branch_result.returned_state,
                branch_result.returned_dep,
            )
            current = branch_result.continue_state
            if current is None:
                break

        return _ExecutionResult(current, returned_state, returned_dep)

    def _execute_instruction(
        self,
        instr: Any,
        state: _AbstractState,
        *,
        enabled: bool,
    ) -> _ExecutionResult:
        instr_type = type(instr)

        if instr_type is CallInstruction:
            if not enabled:
                return _ExecutionResult(state)
            sub_rungs = self._program.subroutines.get(instr.subroutine_name, [])
            sub_result = self._execute_rungs(sub_rungs, state.copy())
            final_state = sub_result.continue_state
            if final_state is None:
                final_state = sub_result.returned_state
            elif sub_result.returned_state is not None:
                final_state = _merge_states(
                    final_state,
                    sub_result.returned_state,
                    sub_result.returned_dep,
                )
            return _ExecutionResult(final_state)

        if instr_type is ReturnInstruction:
            if enabled:
                return _ExecutionResult(None, state.copy(), _CONST_TRUE)
            return _ExecutionResult(state)

        if instr_type is ForLoopInstruction:
            return self._execute_for_loop(instr, state, enabled=enabled)

        if not enabled and instr.is_inert_when_disabled():
            return _ExecutionResult(state)

        if getattr(instr, "oneshot", False) and instr_type is not OutInstruction:
            next_state = state.copy()
            self._apply_unknown_writes(next_state, instr, enabled=enabled)
            self._apply_implicit_faults(next_state, instr, enabled=enabled)
            return _ExecutionResult(next_state)

        if instr_type is OutInstruction:
            value = _CONST_TRUE if enabled else _CONST_FALSE
            next_state = state.copy()
            self._apply_direct_write(next_state, instr.target, value)
            return _ExecutionResult(next_state)

        if instr_type is LatchInstruction:
            if not enabled:
                return _ExecutionResult(state)
            next_state = state.copy()
            self._apply_direct_write(next_state, instr.target, _CONST_TRUE)
            return _ExecutionResult(next_state)

        if instr_type is ResetInstruction:
            if not enabled:
                return _ExecutionResult(state)
            next_state = state.copy()
            self._apply_reset(next_state, instr.target)
            return _ExecutionResult(next_state)

        if instr_type is CopyInstruction:
            next_state = state.copy()
            value = (
                _UNKNOWN_VALUE
                if instr.convert is not None
                else self._eval_value(instr.source, state)
            )
            self._apply_copy_like_write(next_state, instr.dest, value)
            self._apply_implicit_faults(next_state, instr, enabled=enabled)
            return _ExecutionResult(next_state)

        if instr_type is FillInstruction:
            next_state = state.copy()
            value = self._eval_value(instr.value, state)
            self._apply_copy_like_write(next_state, instr.dest, value)
            return _ExecutionResult(next_state)

        if instr_type is CalcInstruction:
            next_state = state.copy()
            value = self._eval_value(instr.expression, state)
            self._apply_calc_write(
                next_state, instr.dest, value, mode=getattr(instr, "mode", "decimal")
            )
            self._apply_implicit_faults(next_state, instr, enabled=enabled)
            return _ExecutionResult(next_state)

        next_state = state.copy()
        self._apply_unknown_writes(next_state, instr, enabled=enabled)
        self._apply_implicit_faults(next_state, instr, enabled=enabled)
        return _ExecutionResult(next_state)

    def _execute_for_loop(
        self,
        instr: ForLoopInstruction,
        state: _AbstractState,
        *,
        enabled: bool,
    ) -> _ExecutionResult:
        if not enabled:
            current = state.copy()
            for child in instr.instructions:
                result = self._execute_instruction(child, current, enabled=False)
                if result.returned_state is not None:
                    return result
                current = result.continue_state or current
            return _ExecutionResult(current)

        count_abs = self._eval_value(instr.count, state)
        if count_abs.is_const and int(count_abs.const) <= 0:
            return _ExecutionResult(state)

        idx_value = (
            _INPUT_VALUE
            if count_abs.depends_on_inputs
            else _ENTRY_VALUE
            if count_abs.depends_on_entry
            else _UNKNOWN_VALUE
            if count_abs.unknown
            else _RETAINED_VALUE
        )

        entry = state.copy()
        entry.set(instr.idx_tag.name, idx_value)

        current = entry
        returned_state: _AbstractState | None = None
        returned_dep: _AbsValue | None = None
        for child in instr.instructions:
            result = self._execute_instruction(child, current, enabled=True)
            returned_state, returned_dep = _merge_return_paths(
                returned_state,
                returned_dep,
                result.returned_state,
                result.returned_dep,
            )
            current = result.continue_state
            if current is None:
                break

        if count_abs.is_const and int(count_abs.const) == 1:
            return _ExecutionResult(current, returned_state, returned_dep)

        loop_dep = _dep_union(count_abs)
        merged_continue = _merge_states(entry, current, loop_dep)
        return _ExecutionResult(merged_continue, returned_state, returned_dep)

    def _apply_direct_write(self, state: _AbstractState, target: Any, value: _AbsValue) -> None:
        names, selection_dep = self._target_names(target, state)
        if not names:
            return
        if selection_dep is None or len(names) == 1:
            for name in names:
                state.set(name, self._coerce_value(name, value))
            return
        for name in names:
            merged = _merge_values(self._coerce_value(name, value), state.get(name), selection_dep)
            state.set(name, merged)

    def _apply_reset(self, state: _AbstractState, target: Any) -> None:
        names, selection_dep = self._target_names(target, state)
        if not names:
            return
        if selection_dep is None or len(names) == 1:
            for name in names:
                tag = self._graph.tags.get(name)
                default = tag.default if tag is not None else 0
                state.set(name, _AbsValue(const=default))
            return
        for name in names:
            tag = self._graph.tags.get(name)
            default = tag.default if tag is not None else 0
            merged = _merge_values(_AbsValue(const=default), state.get(name), selection_dep)
            state.set(name, merged)

    def _apply_copy_like_write(self, state: _AbstractState, target: Any, value: _AbsValue) -> None:
        names, selection_dep = self._target_names(target, state)
        if not names:
            return
        if selection_dep is None or len(names) == 1:
            for name in names:
                state.set(name, self._coerce_value(name, value))
            return
        for name in names:
            merged = _merge_values(self._coerce_value(name, value), state.get(name), selection_dep)
            state.set(name, merged)

    def _apply_calc_write(
        self, state: _AbstractState, target: Any, value: _AbsValue, mode: str
    ) -> None:
        names, selection_dep = self._target_names(target, state)
        if not names:
            return
        if selection_dep is None or len(names) == 1:
            for name in names:
                state.set(name, self._coerce_math_value(name, value, mode))
            return
        for name in names:
            merged = _merge_values(
                self._coerce_math_value(name, value, mode), state.get(name), selection_dep
            )
            state.set(name, merged)

    def _apply_unknown_writes(self, state: _AbstractState, instr: Any, *, enabled: bool) -> None:
        if not enabled and getattr(instr, "is_inert_when_disabled", lambda: True)():
            return
        for name, _itype in _all_write_targets(instr):
            state.set(name, _UNKNOWN_VALUE)

    def _apply_implicit_faults(self, state: _AbstractState, instr: Any, *, enabled: bool) -> None:
        if not enabled and getattr(instr, "is_inert_when_disabled", lambda: True)():
            return
        for name in _implicit_fault_writes(instr, self._graph.tags):
            state.set(name, _UNKNOWN_VALUE)

    def _observe_read(self, name: str, value: _AbsValue) -> None:
        if name != self._candidate:
            return
        if value.unknown:
            self._saw_unknown_read = True
        elif value.depends_on_entry:
            self._saw_entry_read = True

    def _normalize_source_value(self, name: str, value: _AbsValue) -> _AbsValue:
        if name in self._retained and not value.is_const:
            return _AbsValue(
                depends_on_retained=value.depends_on_retained or value.depends_on_entry,
                depends_on_inputs=value.depends_on_inputs,
                unknown=value.unknown,
            )
        return value

    def _read_tag_value(self, name: str, state: _AbstractState) -> _AbsValue:
        observed = self._normalize_source_value(name, state.get(name))
        self._observe_read(name, observed)
        return observed

    def _read_names(self, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        vid = id(value)
        cached = self._read_names_cache.get(vid)
        if cached is not None:
            return cached
        names = _extract_tag_names(value, self._tag_refs)
        result = tuple(sorted(names))
        self._read_names_cache[vid] = result
        return result

    def _eval_value(self, value: Any, state: _AbstractState) -> _AbsValue:
        raw = value.value if type(value) is ImmediateRef else value
        if isinstance(raw, Tag):
            return self._read_tag_value(raw.name, state)
        if type(raw) is IndirectRef:
            return self._eval_indirect_read(raw.block, raw.pointer, state)
        if type(raw) is IndirectExprRef:
            return self._eval_indirect_read(raw.block, raw.expr, state)
        if type(raw) in _PRIMITIVE_TYPES:
            return _AbsValue(const=raw)

        read_names = self._read_names(raw)
        if not read_names:
            exact = self._try_exact_eval(raw, state)
            if exact is not _NO_CONST:
                return _AbsValue(const=exact)
            return _UNKNOWN_VALUE

        deps = [_ZERO_VALUE]
        for name in read_names:
            deps.append(self._read_tag_value(name, state))
        merged = _dep_union(*deps)

        if (
            merged.depends_on_entry
            or merged.depends_on_inputs
            or merged.depends_on_retained
            or merged.unknown
        ):
            exact = self._try_exact_eval(raw, state)
            if exact is not _NO_CONST:
                return _AbsValue(const=exact)
            return merged

        exact = self._try_exact_eval(raw, state)
        if exact is not _NO_CONST:
            return _AbsValue(const=exact)
        return merged

    def _eval_indirect_read(self, block: Any, addr_expr: Any, state: _AbstractState) -> _AbsValue:
        selector = self._eval_value(addr_expr, state)
        exact_addr = self._try_exact_eval(addr_expr, state)
        if exact_addr is not _NO_CONST:
            try:
                tag = block._get_tag(int(exact_addr))
            except Exception:
                return _dep_union(selector, _UNKNOWN_VALUE)
            return self._read_tag_value(tag.name, state)

        domain = self._domain_for_expr(addr_expr, state)
        if not domain:
            return _dep_union(selector, _UNKNOWN_VALUE)

        merged: _AbsValue | None = None
        for addr in domain:
            try:
                tag = block._get_tag(int(addr))
            except Exception:
                return _dep_union(selector, _UNKNOWN_VALUE)
            observed = self._read_tag_value(tag.name, state)
            if merged is None:
                merged = observed
            else:
                merged = _merge_values(merged, observed, selector)

        if merged is None:
            return _dep_union(selector, _UNKNOWN_VALUE)
        return merged

    def _eval_conditions(
        self, conditions: tuple[Condition, ...] | list[Condition], state: _AbstractState
    ) -> _AbsValue:
        if not conditions:
            return _CONST_TRUE
        result = _CONST_TRUE
        for cond in conditions:
            cond_value = self._eval_condition(cond, state)
            if cond_value.is_const and not bool(cond_value.const):
                return _CONST_FALSE
            if not cond_value.is_const:
                result = _merge_values(result, cond_value)
        exact = self._try_exact_eval_all_conditions(conditions, state)
        if exact is not _NO_CONST:
            return _CONST_TRUE if bool(exact) else _CONST_FALSE
        if result == _CONST_TRUE:
            return _UNKNOWN_VALUE
        return result

    def _eval_condition(self, condition: Condition, state: _AbstractState) -> _AbsValue:
        read_names = self._read_names(condition)
        if not read_names:
            exact = self._try_exact_eval(condition, state)
            if exact is not _NO_CONST:
                return _CONST_TRUE if bool(exact) else _CONST_FALSE
            return _UNKNOWN_VALUE

        deps = [_ZERO_VALUE]
        for name in read_names:
            deps.append(self._read_tag_value(name, state))
        exact = self._try_exact_eval(condition, state)
        if exact is not _NO_CONST:
            return _CONST_TRUE if bool(exact) else _CONST_FALSE
        return _dep_union(*deps)

    def _try_exact_eval_all_conditions(
        self,
        conditions: tuple[Condition, ...] | list[Condition],
        state: _AbstractState,
    ) -> Any:
        values: dict[str, Any] = {}
        for cond in conditions:
            names = self._read_names(cond)
            for name in names:
                current = state.get(name)
                if not current.is_const:
                    return _NO_CONST
                values[name] = current.const
        ctx = cast("ScanContext | ConditionView", _ExactContext(values))
        try:
            return all(cond.evaluate(ctx) for cond in conditions)
        except Exception:
            return _NO_CONST

    def _try_exact_eval(self, value: Any, state: _AbstractState) -> Any:
        raw = value.value if type(value) is ImmediateRef else value
        if isinstance(raw, Tag):
            current = state.get(raw.name)
            return current.const if current.is_const else _NO_CONST
        if type(raw) is IndirectRef:
            exact_addr = self._try_exact_eval(raw.pointer, state)
            if exact_addr is _NO_CONST:
                return _NO_CONST
            try:
                tag = raw.block._get_tag(int(exact_addr))
            except Exception:
                return _NO_CONST
            current = state.get(tag.name)
            return current.const if current.is_const else _NO_CONST
        if type(raw) is IndirectExprRef:
            exact_addr = self._try_exact_eval(raw.expr, state)
            if exact_addr is _NO_CONST:
                return _NO_CONST
            try:
                tag = raw.block._get_tag(int(exact_addr))
            except Exception:
                return _NO_CONST
            current = state.get(tag.name)
            return current.const if current.is_const else _NO_CONST
        if type(raw) in _PRIMITIVE_TYPES:
            return raw

        values: dict[str, Any] = {}
        for name in self._read_names(raw):
            current = state.get(name)
            if not current.is_const:
                return _NO_CONST
            values[name] = current.const
        ctx = _ExactContext(values)
        try:
            if hasattr(raw, "evaluate"):
                return raw.evaluate(ctx)
        except Exception:
            return _NO_CONST
        return _NO_CONST

    def _target_names(
        self, target: Any, state: _AbstractState
    ) -> tuple[tuple[str, ...], _AbsValue | None]:
        raw = target.value if type(target) is ImmediateRef else target
        if isinstance(raw, Tag):
            return (raw.name,), None
        if isinstance(raw, BlockRange):
            return tuple(tag.name for tag in raw.tags()), None
        if type(raw) is IndirectRef:
            ptr_value = self._eval_value(raw.pointer, state)
            exact = self._try_exact_eval(raw.pointer, state)
            if exact is not _NO_CONST:
                try:
                    return (raw.block._get_tag(int(exact)).name,), None
                except Exception:
                    return (), _UNKNOWN_VALUE
            domain = self._domain_for_expr(raw.pointer, state)
            if not domain:
                return (), _dep_union(ptr_value)
            names: list[str] = []
            for addr in domain:
                try:
                    names.append(raw.block._get_tag(int(addr)).name)
                except Exception:
                    continue
            return tuple(sorted(set(names))), _dep_union(ptr_value)
        if type(raw) is IndirectExprRef:
            expr_value = self._eval_value(raw.expr, state)
            exact = self._try_exact_eval(raw.expr, state)
            if exact is not _NO_CONST:
                try:
                    return (raw.block._get_tag(int(exact)).name,), None
                except Exception:
                    return (), _UNKNOWN_VALUE
            domain = self._domain_for_expr(raw.expr, state)
            if not domain:
                return (), _dep_union(expr_value)
            names: list[str] = []
            for addr in domain:
                try:
                    names.append(raw.block._get_tag(int(addr)).name)
                except Exception:
                    continue
            return tuple(sorted(set(names))), _dep_union(expr_value)
        if type(raw) is IndirectBlockRange:
            domain_start = self._domain_for_expr(raw.start_expr, state)
            domain_end = self._domain_for_expr(raw.end_expr, state)
            if not domain_start or not domain_end:
                deps = _dep_union(
                    self._eval_value(raw.start_expr, state),
                    self._eval_value(raw.end_expr, state),
                )
                return (), deps
            names: set[str] = set()
            for start, end in product(domain_start, domain_end):
                try:
                    resolved = raw.block.select(int(start), int(end))
                except Exception:
                    continue
                if isinstance(resolved, BlockRange):
                    names.update(tag.name for tag in resolved.tags())
            deps = _dep_union(
                self._eval_value(raw.start_expr, state),
                self._eval_value(raw.end_expr, state),
            )
            return tuple(sorted(names)), deps
        return (), _UNKNOWN_VALUE

    def _domain_for_expr(
        self, value: Any, state: _AbstractState | None = None
    ) -> tuple[Any, ...] | None:
        raw = value.value if type(value) is ImmediateRef else value
        if type(raw) in _NUMERIC_PRIMITIVE_TYPES:
            return (raw,)
        if isinstance(raw, Tag):
            if state is not None:
                current = state.get(raw.name)
                if current.is_const:
                    return (current.const,)
            return self._known_domains.get(raw.name)
        exact_state = state if state is not None else _AbstractState(self._base, {})
        exact = self._try_exact_eval(raw, exact_state)
        if exact is not _NO_CONST:
            return (exact,)

        read_names = self._read_names(raw)
        if not read_names:
            return None

        domains: list[tuple[Any, ...]] = []
        total = 1
        for name in read_names:
            domain: tuple[Any, ...] | None = None
            if state is not None:
                current = state.get(name)
                if current.is_const:
                    domain = (current.const,)
            if domain is None:
                domain = self._known_domains.get(name)
            if not domain:
                return None
            domains.append(domain)
            total *= len(domain)
            if total > _EXPR_ENUM_LIMIT:
                return None

        values: set[Any] = set()
        for combo in product(*domains):
            ctx = _ExactContext(dict(zip(read_names, combo, strict=True)))
            try:
                if isinstance(raw, Tag):
                    values.add(combo[0])
                elif hasattr(raw, "evaluate"):
                    values.add(raw.evaluate(ctx))
                else:
                    return None
            except Exception:
                return None
            if len(values) > _EXPR_ENUM_LIMIT:
                return None
        return tuple(sorted(values))

    def _coerce_value(self, name: str, value: _AbsValue) -> _AbsValue:
        if not value.is_const:
            return value
        tag = self._graph.tags.get(name)
        if tag is None:
            return value
        from pyrung.core.instruction.conversions import _store_copy_value_to_tag_type

        try:
            return _AbsValue(const=_store_copy_value_to_tag_type(value.const, tag))
        except Exception:
            return _UNKNOWN_VALUE

    def _coerce_math_value(self, name: str, value: _AbsValue, mode: str) -> _AbsValue:
        if not value.is_const:
            return value
        tag = self._graph.tags.get(name)
        if tag is None:
            return value
        from pyrung.core.instruction.conversions import _truncate_to_tag_type

        try:
            return _AbsValue(const=_truncate_to_tag_type(value.const, tag, mode))
        except Exception:
            return _UNKNOWN_VALUE


class _ScanLocalStateElider:
    """Fixed-point driver for abstract scan-local state-key elision."""

    def __init__(
        self,
        program: Program,
        graph: ProgramGraph,
        stateful_dims: Mapping[str, tuple[Any, ...]],
        nondeterministic_dims: Mapping[str, tuple[Any, ...]],
        *,
        progress: Callable[[str], None] | None = None,
        progress_prefix: Callable[[], str] | None = None,
    ) -> None:
        self._program = program
        self._graph = graph
        self._stateful_dims = dict(stateful_dims)
        self._nondeterministic_dims = dict(nondeterministic_dims)
        self._nondeterministic_names = frozenset(nondeterministic_dims)
        self._known_domains = self._build_known_domains()
        self._written_tags = frozenset(graph.writers_of)
        self._progress = progress
        self._progress_prefix = progress_prefix
        self._read_names_cache: dict[int, tuple[str, ...]] = {}
        self._tag_refs = dict(graph.tags)
        self._proof_details: dict[str, tuple[tuple[str, str], ...]] = {}
        self._edge_source_tags = _edge_source_tags(program)

    def _emit(self, message: str) -> None:
        if self._progress is not None:
            self._progress(message)

    def elide(self) -> tuple[dict[str, tuple[Any, ...]], dict[str, _ElidedSummary]]:
        import sys

        retained = set(self._stateful_dims)
        changed = True
        accepted: dict[str, _ElidedSummary] = {}
        dots: list[str] = []
        use_dots = self._progress_prefix is not None

        if use_dots:
            assert self._progress_prefix is not None
            header = f"{self._progress_prefix()}elision | abstract {len(retained)} tags "
            print(header, end="", file=sys.stderr, flush=True)

        while changed:
            changed = False
            accepted = self._compute_nonretained_summaries(frozenset(retained))
            for tag_name in sorted(list(retained)):
                if PENDING in self._stateful_dims.get(tag_name, ()):
                    continue
                if tag_name in self._edge_source_tags:
                    continue
                summary = self._prove_tag(tag_name, frozenset(retained - {tag_name}), accepted)
                if summary is None:
                    if use_dots:
                        print(".", end="", file=sys.stderr, flush=True)
                        dots.append(".")
                    continue
                retained.remove(tag_name)
                accepted[tag_name] = summary
                changed = True
                if use_dots:
                    print("x", end="", file=sys.stderr, flush=True)
                    dots.append("x")

        removed = len(self._stateful_dims) - len(retained)
        if use_dots:
            print(f"  removed={removed}", file=sys.stderr)
        else:
            self._emit(
                f"elision | abstract phase complete"
                f" | removed={removed:,}"
                f" | retained={len(retained):,}"
            )

        return (
            {name: domain for name, domain in self._stateful_dims.items() if name in retained},
            accepted,
        )

    def _compute_nonretained_summaries(
        self,
        retained: frozenset[str],
    ) -> dict[str, _ElidedSummary]:
        accepted: dict[str, _ElidedSummary] = {}
        changed = True
        while changed:
            changed = False
            candidates = self._written_tags - retained - set(accepted) - self._edge_source_tags
            for tag_name in sorted(candidates):
                summary = self._prove_tag(tag_name, retained, accepted)
                if summary is None:
                    continue
                accepted[tag_name] = summary
                changed = True
        return accepted

    def _prove_tag(
        self,
        tag_name: str,
        retained: frozenset[str],
        accepted: Mapping[str, _ElidedSummary],
    ) -> _ElidedSummary | None:
        first = self._run_candidate(tag_name, retained, accepted, _ENTRY_VALUE)
        if first.saw_unknown_read:
            return None
        if first.same_scan_safe:
            self._proof_details[tag_name] = (
                ("abstract_path", "write_before_read"),
                ("exit_value", self._describe_abs_value(first.exit_value)),
            )
            return _ElidedSummary(
                exit_value=first.exit_value,
                entry_summary=first.exit_value.as_entry_summary(),
            )
        return self._prove_tag_from_canonical_entry(tag_name, retained, accepted)

    def _prove_tag_from_canonical_entry(
        self,
        tag_name: str,
        retained: frozenset[str],
        accepted: Mapping[str, _ElidedSummary],
    ) -> _ElidedSummary | None:
        tag = self._graph.tags.get(tag_name)
        default_value = tag.default if tag is not None else 0
        entry_value = _AbsValue(const=default_value)
        seen_entries: set[_AbsValue] = set()

        while entry_value.is_const and entry_value not in seen_entries:
            seen_entries.add(entry_value)
            run = self._run_candidate(tag_name, retained, accepted, entry_value)
            if run.saw_unknown_read or not run.same_scan_safe or not run.exit_value.is_const:
                return None
            if run.exit_value == entry_value:
                self._proof_details[tag_name] = (
                    ("abstract_path", "canonical_convergence"),
                    ("converged_value", repr(run.exit_value.const)),
                    ("iterations", str(len(seen_entries))),
                )
                return _ElidedSummary(
                    exit_value=run.exit_value,
                    entry_summary=_ConstEntry(run.exit_value.const),
                )
            entry_value = run.exit_value
        return None

    @staticmethod
    def _describe_abs_value(v: _AbsValue) -> str:
        if v.is_const:
            return f"const({v.const!r})"
        parts: list[str] = []
        if v.depends_on_retained:
            parts.append("retained")
        if v.depends_on_inputs:
            parts.append("inputs")
        if v.depends_on_entry:
            parts.append("entry")
        if v.unknown:
            parts.append("unknown")
        return "depends(" + ", ".join(parts) + ")" if parts else "zero"

    def _run_candidate(
        self,
        tag_name: str,
        retained: frozenset[str],
        accepted: Mapping[str, _ElidedSummary],
        candidate_entry: _AbsValue,
    ) -> _CandidateRun:
        checker = _TagElisionCheck(
            self._program,
            self._graph,
            self._known_domains,
            self._nondeterministic_names,
            retained,
            accepted,
            tag_name,
            candidate_entry,
            self._read_names_cache,
            self._tag_refs,
        )
        return checker.run()

    def _build_known_domains(self) -> dict[str, tuple[Any, ...]]:
        known = dict(self._stateful_dims)
        known.update(self._nondeterministic_dims)
        for name, tag in self._graph.tags.items():
            if name in known:
                continue
            if tag.type == TagType.BOOL:
                known[name] = (False, True)
                continue
            if tag.choices is not None:
                known[name] = tuple(sorted(tag.choices.keys()))
                continue
            if tag.min is None or tag.max is None:
                continue
            try:
                size = int(tag.max - tag.min + 1)
            except Exception:
                continue
            if 0 < size <= _EXPR_ENUM_LIMIT:
                known[name] = tuple(range(int(tag.min), int(tag.max) + 1))
        return known


# ---------------------------------------------------------------------------
# Elision pass/rule pipeline components
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AbstractRule:
    name: str
    description: str
    fn: Callable[[dict[str, tuple[Any, ...]], _ElisionContext], None]
    enabled: bool = True


def _rule_provenance(abstract_reduced: dict[str, tuple[Any, ...]], ctx: _ElisionContext) -> None:
    removed = set(ctx.stateful_dims) - set(abstract_reduced)
    for tag_name in removed:
        del ctx.stateful_dims[tag_name]
        ctx.elided[tag_name] = "provenance"


def _pass_abstract(ctx: _ElisionContext) -> None:
    elider = _ScanLocalStateElider(
        ctx.program,
        ctx.graph,
        ctx.stateful_dims,
        ctx.nondeterministic_dims,
        progress=ctx.progress,
        progress_prefix=ctx.progress_prefix,
    )
    abstract_reduced, _accepted = elider.elide()
    ctx.proof_details.update(elider._proof_details)
    for rule in _DEFAULT_ABSTRACT_RULES:
        if rule.enabled:
            rule.fn(abstract_reduced, ctx)


_DEFAULT_ABSTRACT_RULES: tuple[_AbstractRule, ...] = (
    _AbstractRule(
        "provenance",
        "Per-tag dependency lattice — WBR, unconditional writes,"
        " deterministic projections, canonical entry convergence",
        _rule_provenance,
    ),
)
