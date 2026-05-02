"""Two-phase state-key elision: abstract pre-filter then concrete kernel proofs.

Phase 1 (abstract): A fast O(program-size) provenance analysis that tracks
whether each tag's exit value depends on its entry value, retained state,
inputs, or is unknown.  Handles most cases instantly.

Phase 2 (concrete): For tags the abstract pass could not resolve, uses the
compiled replay kernel to enumerate domain combinations and prove elision.
Strictly more powerful but combinatorially bounded.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import product
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import ProgramGraph, _extract_tag_names, _implicit_fault_writes
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.coils import LatchInstruction, OutInstruction, ResetInstruction
from pyrung.core.instruction.control import CallInstruction, ForLoopInstruction, ReturnInstruction
from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction
from pyrung.core.kernel import CompiledKernel
from pyrung.core.memory_block import BlockRange, IndirectBlockRange, IndirectExprRef, IndirectRef
from pyrung.core.tag import ImmediateRef, Tag, TagType

from . import PENDING
from .absorb import _all_write_targets
from .inputs import _detect_exclusive_input_groups, _exclusive_input_group_membership
from .kernel import _step_compiled_kernel

if TYPE_CHECKING:
    from pyrung.core.condition import Condition
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung

_ELISION_ENUM_LIMIT = 200_000
_EXPR_ENUM_LIMIT = 128
_FORCED_TRUE_COMBO_LIMIT = 4_096
_DEFAULT_DT = 0.010
_MEMORY_EXCLUDED_PREFIXES = ("_dt", "_frac:")
# Batch removal proves candidates against one retained snapshot per round.
# Lower _ELISION_PROOF_BUDGET to skip medium-cost proofs when tuning startup time.
_ELISION_BATCH_REMOVE = True
_ELISION_PROOF_BUDGET = _ELISION_ENUM_LIMIT


_NO_CONST = object()


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

    def as_entry_summary(self) -> _AbsValue:
        """Summary to use as the next scan's entry value for accepted tags."""
        if self.is_const:
            return self
        if self.is_canonical:
            return _RETAINED_VALUE
        return _ENTRY_VALUE


_CONST_FALSE = _AbsValue(const=False)
_CONST_TRUE = _AbsValue(const=True)
_RETAINED_VALUE = _AbsValue(depends_on_retained=True)
_INPUT_VALUE = _AbsValue(depends_on_inputs=True)
_ENTRY_VALUE = _AbsValue(depends_on_entry=True)
_UNKNOWN_VALUE = _AbsValue(unknown=True)
_ZERO_VALUE = _AbsValue(const=0)


def _dep_union(*values: _AbsValue) -> _AbsValue:
    return _AbsValue(
        depends_on_retained=any(v.depends_on_retained for v in values),
        depends_on_inputs=any(v.depends_on_inputs for v in values),
        depends_on_entry=any(v.depends_on_entry for v in values),
        unknown=any(v.unknown for v in values),
    )


def _merge_values(a: _AbsValue, b: _AbsValue, guard_dep: _AbsValue | None = None) -> _AbsValue:
    if a == b:
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
        return self.overrides.get(name, self.base.get(name, _UNKNOWN_VALUE))

    def set(self, name: str, value: _AbsValue) -> None:
        base_value = self.base.get(name, _UNKNOWN_VALUE)
        if value == base_value:
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
    entry_value: _AbsValue


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
                base[name] = self._accepted[name].entry_value
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
        if isinstance(instr, CallInstruction):
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

        if isinstance(instr, ReturnInstruction):
            if enabled:
                return _ExecutionResult(None, state.copy(), _CONST_TRUE)
            return _ExecutionResult(state)

        if isinstance(instr, ForLoopInstruction):
            return self._execute_for_loop(instr, state, enabled=enabled)

        if not enabled and getattr(instr, "is_inert_when_disabled", lambda: True)():
            return _ExecutionResult(state)

        if getattr(instr, "oneshot", False):
            next_state = state.copy()
            self._apply_unknown_writes(next_state, instr, enabled=enabled)
            self._apply_implicit_faults(next_state, instr, enabled=enabled)
            return _ExecutionResult(next_state)

        if isinstance(instr, OutInstruction):
            value = _CONST_TRUE if enabled else _CONST_FALSE
            next_state = state.copy()
            self._apply_direct_write(next_state, instr.target, value)
            return _ExecutionResult(next_state)

        if isinstance(instr, LatchInstruction):
            if not enabled:
                return _ExecutionResult(state)
            next_state = state.copy()
            self._apply_direct_write(next_state, instr.target, _CONST_TRUE)
            return _ExecutionResult(next_state)

        if isinstance(instr, ResetInstruction):
            if not enabled:
                return _ExecutionResult(state)
            next_state = state.copy()
            self._apply_reset(next_state, instr.target)
            return _ExecutionResult(next_state)

        if isinstance(instr, CopyInstruction):
            next_state = state.copy()
            value = (
                _UNKNOWN_VALUE
                if instr.convert is not None
                else self._eval_value(instr.source, state)
            )
            self._apply_copy_like_write(next_state, instr.dest, value)
            self._apply_implicit_faults(next_state, instr, enabled=enabled)
            return _ExecutionResult(next_state)

        if isinstance(instr, FillInstruction):
            next_state = state.copy()
            value = self._eval_value(instr.value, state)
            self._apply_copy_like_write(next_state, instr.dest, value)
            return _ExecutionResult(next_state)

        if isinstance(instr, CalcInstruction):
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
        names = _extract_tag_names(value, dict(self._graph.tags))
        return tuple(sorted(names))

    def _eval_value(self, value: Any, state: _AbstractState) -> _AbsValue:
        raw = value.value if isinstance(value, ImmediateRef) else value
        if isinstance(raw, Tag):
            return self._read_tag_value(raw.name, state)
        if isinstance(raw, IndirectRef):
            return self._eval_indirect_read(raw.block, raw.pointer, state)
        if isinstance(raw, IndirectExprRef):
            return self._eval_indirect_read(raw.block, raw.expr, state)
        if isinstance(raw, (bool, int, float, str, bytes, bytearray)):
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
        ctx = _ExactContext(values)
        try:
            return all(cond.evaluate(ctx) for cond in conditions)
        except Exception:
            return _NO_CONST

    def _try_exact_eval(self, value: Any, state: _AbstractState) -> Any:
        raw = value.value if isinstance(value, ImmediateRef) else value
        if isinstance(raw, Tag):
            current = state.get(raw.name)
            return current.const if current.is_const else _NO_CONST
        if isinstance(raw, IndirectRef):
            exact_addr = self._try_exact_eval(raw.pointer, state)
            if exact_addr is _NO_CONST:
                return _NO_CONST
            try:
                tag = raw.block._get_tag(int(exact_addr))
            except Exception:
                return _NO_CONST
            current = state.get(tag.name)
            return current.const if current.is_const else _NO_CONST
        if isinstance(raw, IndirectExprRef):
            exact_addr = self._try_exact_eval(raw.expr, state)
            if exact_addr is _NO_CONST:
                return _NO_CONST
            try:
                tag = raw.block._get_tag(int(exact_addr))
            except Exception:
                return _NO_CONST
            current = state.get(tag.name)
            return current.const if current.is_const else _NO_CONST
        if isinstance(raw, (bool, int, float, str, bytes, bytearray)):
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
        raw = target.value if isinstance(target, ImmediateRef) else target
        if isinstance(raw, Tag):
            return (raw.name,), None
        if isinstance(raw, BlockRange):
            return tuple(tag.name for tag in raw.tags()), None
        if isinstance(raw, IndirectRef):
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
        if isinstance(raw, IndirectExprRef):
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
        if isinstance(raw, IndirectBlockRange):
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
        raw = value.value if isinstance(value, ImmediateRef) else value
        if isinstance(raw, bool | int | float):
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
    ) -> None:
        self._program = program
        self._graph = graph
        self._stateful_dims = dict(stateful_dims)
        self._nondeterministic_dims = dict(nondeterministic_dims)
        self._nondeterministic_names = frozenset(nondeterministic_dims)
        self._known_domains = self._build_known_domains()
        self._written_tags = frozenset(graph.writers_of)

    def elide(self) -> tuple[dict[str, tuple[Any, ...]], dict[str, _ElidedSummary]]:
        retained = set(self._stateful_dims)
        changed = True
        accepted: dict[str, _ElidedSummary] = {}

        while changed:
            changed = False
            accepted = self._compute_nonretained_summaries(frozenset(retained))
            for tag_name in sorted(list(retained)):
                summary = self._prove_tag(tag_name, frozenset(retained - {tag_name}), accepted)
                if summary is None:
                    continue
                retained.remove(tag_name)
                accepted[tag_name] = summary
                changed = True

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
            for tag_name in sorted(self._written_tags - retained - set(accepted)):
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
            return _ElidedSummary(
                exit_value=first.exit_value,
                entry_value=first.exit_value.as_entry_summary(),
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

        while entry_value.is_canonical and entry_value not in seen_entries:
            seen_entries.add(entry_value)
            run = self._run_candidate(tag_name, retained, accepted, entry_value)
            if run.saw_unknown_read or not run.same_scan_safe or not run.exit_value.is_canonical:
                return None
            next_entry = run.exit_value.as_entry_summary()
            if next_entry == entry_value:
                return _ElidedSummary(exit_value=run.exit_value, entry_value=next_entry)
            entry_value = next_entry
        return None

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
# Phase 2: Concrete kernel elision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ForcedTrueCoverage:
    written_tags: frozenset[str]
    varied_tags: tuple[str, ...]
    truncated: bool = False


def _domain_from_tag_metadata(tag: Tag) -> tuple[Any, ...] | None:
    if tag.type == TagType.BOOL:
        return (False, True)
    if tag.choices is not None:
        return tuple(sorted(tag.choices.keys()))
    if tag.min is None or tag.max is None:
        return None
    try:
        size = int(tag.max - tag.min + 1)
    except Exception:
        return None
    if 0 < size <= 256:
        return tuple(range(int(tag.min), int(tag.max) + 1))
    return None


def _product_size(domains: tuple[tuple[Any, ...], ...]) -> int:
    total = 1
    for domain in domains:
        total *= len(domain)
    return total


def _is_fault_tag(tag_name: str) -> bool:
    return tag_name.startswith("fault.")


def _alternate_seed_value(tag: Tag) -> Any:
    default = tag.default
    if tag.type == TagType.BOOL:
        return not bool(default)
    if tag.choices is not None:
        for value in sorted(tag.choices.keys()):
            if value != default:
                return value
    if tag.min is not None and tag.max is not None:
        try:
            min_value = int(tag.min)
            max_value = int(tag.max)
            if default != min_value:
                return min_value
            if default != max_value:
                return max_value
        except Exception:
            pass
    if tag.type in {TagType.INT, TagType.DINT, TagType.WORD}:
        return 1 if default != 1 else 0
    if tag.type == TagType.REAL:
        return 1.0 if default != 1.0 else 0.0
    if tag.type == TagType.CHAR:
        return "__alt__" if default != "__alt__" else ""
    return default


def _seed_profile(compiled: CompiledKernel, *, alternate: bool) -> dict[str, Any]:
    seeded: dict[str, Any] = {}
    for name, tag in compiled.referenced_tags.items():
        seeded[name] = _alternate_seed_value(tag) if alternate else tag.default
    return seeded


def _coverage_domain_items(
    graph: ProgramGraph,
    stateful_dims: Mapping[str, tuple[Any, ...]],
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    *,
    combo_limit: int,
) -> tuple[tuple[str, tuple[Any, ...]], ...]:
    known_domains = dict(stateful_dims)
    known_domains.update(nondeterministic_dims)
    if not known_domains:
        return ()

    all_items = tuple(sorted(known_domains.items()))
    if _product_size(tuple(domain for _name, domain in all_items)) <= combo_limit:
        return all_items

    selected: set[str] = set(nondeterministic_dims)
    for pointer_name in graph.pointer_tags:
        if pointer_name in known_domains:
            selected.add(pointer_name)
        selected.update(graph.upstream_slice(pointer_name) & set(known_domains))

    if not selected:
        return ()

    items = tuple(sorted((name, known_domains[name]) for name in selected))
    if _product_size(tuple(domain for _name, domain in items)) <= combo_limit:
        return items

    reduced = tuple(sorted((name, known_domains[name]) for name in nondeterministic_dims))
    return reduced


def _collect_forced_true_coverage(
    program: Program,
    graph: ProgramGraph,
    stateful_dims: Mapping[str, tuple[Any, ...]],
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    *,
    compiled: CompiledKernel | None = None,
    combo_limit: int = _FORCED_TRUE_COMBO_LIMIT,
) -> _ForcedTrueCoverage:
    from pyrung.circuitpy.codegen import compile_kernel

    forced_compiled = compiled or compile_kernel(
        program,
        force_rung_enable=True,
        blockless=True,
    )
    domain_items = _coverage_domain_items(
        graph,
        stateful_dims,
        nondeterministic_dims,
        combo_limit=combo_limit,
    )
    varied_tags = tuple(name for name, _domain in domain_items)
    combo_space = (
        _product_size(tuple(domain for _name, domain in domain_items)) if domain_items else 1
    )
    truncated = (combo_space * 2) > combo_limit

    written_tags: set[str] = set()
    domain_values = [domain for _name, domain in domain_items]
    seed_profiles = (False, True)
    remaining_budget = combo_limit
    for seed_index, alternate in enumerate(seed_profiles):
        if remaining_budget <= 0:
            break
        seeds_left = len(seed_profiles) - seed_index
        combo_budget = (
            remaining_budget
            if combo_space <= remaining_budget
            else max(1, (remaining_budget + seeds_left - 1) // seeds_left)
        )
        seed = _seed_profile(forced_compiled, alternate=alternate)
        processed = 0
        combo_iter = product(*domain_values) if domain_values else [()]
        for combo in combo_iter:
            if processed >= combo_budget or remaining_budget <= 0:
                break
            kernel = forced_compiled.create_kernel()
            kernel.tags.update(seed)
            entry_values = dict(zip(varied_tags, combo, strict=True))
            kernel.tags.update(entry_values)
            before = {name: kernel.tags.get(name) for name in forced_compiled.referenced_tags}
            _step_compiled_kernel(forced_compiled, kernel, dt=_DEFAULT_DT)
            for name in forced_compiled.referenced_tags:
                if kernel.tags.get(name) != before.get(name):
                    written_tags.add(name)
            processed += 1
            remaining_budget -= 1

    return _ForcedTrueCoverage(
        written_tags=frozenset(written_tags),
        varied_tags=varied_tags,
        truncated=truncated,
    )


class _ConcreteStateElider:
    def __init__(
        self,
        program: Program,
        graph: ProgramGraph,
        stateful_dims: Mapping[str, tuple[Any, ...]],
        nondeterministic_dims: Mapping[str, tuple[Any, ...]],
        *,
        state_basis: frozenset[str] | None = None,
        compiled: CompiledKernel | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        from pyrung.circuitpy.codegen import compile_kernel

        self._program = program
        self._graph = graph
        self._stateful_dims = dict(stateful_dims)
        self._state_basis = (
            frozenset(self._stateful_dims)
            if state_basis is None
            else frozenset(state_basis) & frozenset(self._stateful_dims)
        )
        self._nondeterministic_dims = dict(nondeterministic_dims)
        self._progress = progress
        self._compiled = compiled or compile_kernel(program, blockless=True)
        self._entry_sensitive_cache: dict[tuple[str, frozenset[str]], bool] = {}
        self._coverage = _collect_forced_true_coverage(
            program,
            graph,
            stateful_dims,
            nondeterministic_dims,
        )
        self._exclusive_input_groups = _detect_exclusive_input_groups(
            program,
            graph,
            self._nondeterministic_dims,
            project=tuple(self._stateful_dims),
            extra_exprs=None,
        )
        self._exclusive_input_group_by_member = _exclusive_input_group_membership(
            self._exclusive_input_groups
        )
        static_writers = set(graph.writers_of) & set(self._stateful_dims)
        dynamic_writers = set(self._coverage.written_tags) & set(self._stateful_dims)
        self._written_tags = frozenset(static_writers | dynamic_writers)
        self._continued_source_tags = self._find_continued_source_tags()

        # Discover kernel memory keys via pilot scans.  Instructions like
        # oneshot OTE store hidden state in kernel.memory that fresh kernels
        # do not reproduce.  We sweep ND input combinations (using both
        # default and alternate seeds) to find a memory snapshot where at
        # least one key differs from the fresh-kernel default.
        self._warm_memory: dict[str, Any] | None = None
        mem_keys: list[str] = []
        nd_names = sorted(self._nondeterministic_dims)
        nd_domains = [self._nondeterministic_dims[n] for n in nd_names]
        combo_iter = product(*nd_domains) if nd_domains else [()]
        fresh_memory: dict[str, Any] = {}
        warm_found = False
        pilot_budget = _FORCED_TRUE_COMBO_LIMIT
        pilots_run = 0
        for combo in combo_iter:
            if pilots_run >= pilot_budget:
                break
            for alt in (False, True):
                pilot = self._compiled.create_kernel()
                if alt:
                    pilot.tags.update(_seed_profile(self._compiled, alternate=True))
                for name, val in zip(nd_names, combo, strict=True):
                    pilot.tags[name] = val
                _step_compiled_kernel(self._compiled, pilot, dt=_DEFAULT_DT)
                pilots_run += 1
                found = [
                    k
                    for k in pilot.memory
                    if not any(k.startswith(p) for p in _MEMORY_EXCLUDED_PREFIXES)
                ]
                if found and not mem_keys:
                    mem_keys = found
                    fresh_memory = {k: pilot.memory.get(k) for k in found}
                if found and not warm_found:
                    for k in found:
                        if pilot.memory.get(k) != fresh_memory.get(k):
                            self._warm_memory = dict(pilot.memory)
                            warm_found = True
                            break
            if warm_found:
                break

        self._emit(
            "elision | setup complete"
            f" | stateful={len(self._stateful_dims):,}"
            f" | candidates={len(self._candidate_names()):,}"
            f" | forced-true={'truncated' if self._coverage.truncated else 'complete'}"
            f" | input-groups={len(self._exclusive_input_groups)}"
            f" | memory_keys={len(mem_keys)}"
        )

    def elide(self) -> dict[str, tuple[Any, ...]]:
        retained = set(self._stateful_dims)
        changed = True
        round_num = 0
        while changed:
            changed = False
            round_num += 1
            snapshot = set(retained)
            candidates = self._ordered_candidates(snapshot)
            self._emit(f"elision round {round_num} | checking {len(candidates):,} candidate tag(s)")
            removable: list[str] = []
            for index, tag_name in enumerate(candidates, start=1):
                self._emit(f"elision | checking {tag_name} ({index}/{len(candidates)})")
                compare_retained = frozenset(snapshot - {tag_name})
                if not self._can_elide(tag_name, compare_retained):
                    self._emit(f"elision | keeping {tag_name}")
                    continue
                if _ELISION_BATCH_REMOVE:
                    removable.append(tag_name)
                    self._emit(f"elision | can drop {tag_name}")
                    continue
                retained.remove(tag_name)
                changed = True
                self._emit(f"elision | dropped {tag_name}")
            if _ELISION_BATCH_REMOVE and removable:
                retained.difference_update(removable)
                changed = True
        self._emit(
            "elision complete"
            f" | removed={len(self._stateful_dims) - len(retained):,}"
            f" | retained={len(retained):,}"
        )
        return {name: domain for name, domain in self._stateful_dims.items() if name in retained}

    def _find_continued_source_tags(self) -> frozenset[str]:
        """Tags read via continued() rungs — their cross-scan value is observable."""
        sources: set[str] = set()
        for node in self._graph.rung_nodes:
            if node.scope == "main":
                rung = self._program.rungs[node.rung_index]
            else:
                assert node.subroutine is not None
                rung = self._program.subroutines[node.subroutine][node.rung_index]
            if not getattr(rung, "_use_prior_snapshot", False):
                continue
            for read_tag in node.condition_reads | node.data_reads:
                if read_tag in self._stateful_dims:
                    sources.add(read_tag)
        return frozenset(sources)

    def _is_concrete_candidate(self, name: str) -> bool:
        """True when the tag is eligible for concrete elision proofs."""
        if name not in self._state_basis:
            return False
        if name not in self._written_tags:
            return False
        if getattr(self._graph.tags.get(name), "lock", False):
            return False
        # Tags with PENDING in their domain use abstract three-valued logic
        # (timer/counter Done bits).  The concrete kernel cannot execute with
        # the PENDING sentinel, so these tags must be retained unconditionally.
        if PENDING in self._stateful_dims.get(name, ()):
            return False
        # Tags read via continued() have observable cross-scan state — their
        # previous-scan value flows into the current scan's outputs.  The
        # concrete frontier traversal misses combinational observers, so
        # retain unconditionally.
        if name in self._continued_source_tags:
            return False
        return True

    def _candidate_names(self) -> tuple[str, ...]:
        return tuple(
            sorted(name for name in self._stateful_dims if self._is_concrete_candidate(name))
        )

    def _ordered_candidates(self, retained: set[str]) -> tuple[str, ...]:
        return tuple(
            sorted(
                (name for name in retained if self._is_concrete_candidate(name)),
                key=lambda name: (
                    len(self._graph.downstream_slice(name) & retained),
                    len(self._stateful_dims[name]),
                    name,
                ),
            )
        )

    def _emit(self, message: str) -> None:
        if self._progress is not None:
            self._progress(message)

    def _input_assignment_dimensions(
        self,
        input_names: tuple[str, ...],
    ) -> tuple[tuple[tuple[tuple[str, Any], ...], ...], ...]:
        dimensions: list[tuple[tuple[tuple[str, Any], ...], ...]] = []
        live_inputs = set(input_names)
        seen_groups: set[int] = set()

        for name in sorted(input_names):
            group_index = self._exclusive_input_group_by_member.get(name)
            if group_index is not None:
                if group_index in seen_groups:
                    continue
                seen_groups.add(group_index)
                group = self._exclusive_input_groups[group_index]
                options: list[tuple[tuple[str, Any], ...]] = []
                seen_options: set[tuple[tuple[str, Any], ...]] = set()
                for canonical in group.canonical_assignments:
                    filtered = tuple(
                        (member, value) for member, value in canonical if member in live_inputs
                    )
                    if filtered in seen_options:
                        continue
                    seen_options.add(filtered)
                    options.append(filtered)
                if options:
                    dimensions.append(tuple(options))
                continue

            dimensions.append(
                tuple(((name, value),) for value in self._nondeterministic_dims[name])
            )

        return tuple(dimensions)

    def _can_elide(self, candidate: str, retained: frozenset[str]) -> bool:
        observed, fallback_hidden = self._reachable_stateful_frontier(candidate, retained)
        if not observed:
            sticky_hidden = tuple(
                name for name in fallback_hidden if self._hidden_entry_matters(name, retained)
            )
            if not sticky_hidden:
                return True
            observed = sticky_hidden

        retained_names, input_names, hidden_stateful = self._scoped_dependencies(
            candidate,
            observed,
            retained,
        )

        retained_domains = tuple(self._stateful_dims[name] for name in retained_names)
        input_assignment_dimensions = self._input_assignment_dimensions(input_names)
        input_combo_count = 1
        for dimension in input_assignment_dimensions:
            input_combo_count *= len(dimension)
        group_product = _product_size(retained_domains) * input_combo_count
        vary_names = hidden_stateful + (candidate,)
        vary_domains = tuple(self._stateful_dims[name] for name in hidden_stateful) + (
            self._stateful_dims[candidate],
        )
        proof_limit = min(_ELISION_ENUM_LIMIT, _ELISION_PROOF_BUDGET)
        if group_product * _product_size(vary_domains) > proof_limit:
            return False

        retained_iter = product(*retained_domains) if retained_domains else [()]
        for retained_values in retained_iter:
            retained_entry = dict(zip(retained_names, retained_values, strict=True))
            input_iter = (
                product(*input_assignment_dimensions) if input_assignment_dimensions else [()]
            )
            for input_assignments in input_iter:
                entry_values = dict(retained_entry)
                for partial_assignment in input_assignments:
                    entry_values.update(partial_assignment)
                expected: tuple[Any, ...] | None = None
                vary_iter_values = product(*vary_domains) if vary_domains else [()]
                for vary_values in vary_iter_values:
                    full_entry = dict(entry_values)
                    full_entry.update(dict(zip(vary_names, vary_values, strict=True)))
                    outcome = self._scan(full_entry, observed)
                    if outcome is None:
                        return False
                    if expected is None:
                        expected = outcome
                        continue
                    if outcome != expected:
                        return False
        return True

    def _reachable_stateful_frontier(
        self,
        candidate: str,
        retained: frozenset[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        reachable_retained: set[str] = set()
        reachable_hidden: set[str] = set()
        retained_set = set(retained)
        stateful_names = set(self._stateful_dims)
        visited: set[str] = {candidate}
        queue: deque[str] = deque([candidate])

        while queue:
            current = queue.popleft()
            if _is_fault_tag(current):
                continue
            for rung_idx in self._graph.readers_of.get(current, frozenset()):
                node = self._graph.rung_nodes[rung_idx]
                for written_tag in node.writes:
                    if _is_fault_tag(written_tag):
                        continue
                    if written_tag in retained_set:
                        reachable_retained.add(written_tag)
                        continue
                    if written_tag in stateful_names and written_tag != candidate:
                        reachable_hidden.add(written_tag)
                    if written_tag in visited:
                        continue
                    visited.add(written_tag)
                    queue.append(written_tag)

        return tuple(sorted(reachable_retained)), tuple(sorted(reachable_hidden))

    def _hidden_entry_matters(self, tag_name: str, retained: frozenset[str]) -> bool:
        cache_key = (tag_name, retained)
        cached = self._entry_sensitive_cache.get(cache_key)
        if cached is not None:
            return cached

        upstream = set(self._graph.upstream_slice(tag_name))
        cone = upstream | {tag_name}
        retained_names = tuple(sorted(set(retained) & upstream))
        input_names = tuple(sorted(upstream & set(self._nondeterministic_dims)))
        hidden_names = tuple(
            sorted((cone & set(self._stateful_dims)) - set(retained_names) - {tag_name})
        )

        fixed_domains = (
            tuple(self._stateful_dims[name] for name in retained_names)
            + tuple(self._nondeterministic_dims[name] for name in input_names)
            + tuple(self._stateful_dims[name] for name in hidden_names)
        )
        tag_domain = self._stateful_dims[tag_name]
        if _product_size(fixed_domains + (tag_domain,)) > _ELISION_ENUM_LIMIT:
            self._entry_sensitive_cache[cache_key] = True
            return True

        fixed_names = retained_names + input_names + hidden_names
        fixed_iter = product(*fixed_domains) if fixed_domains else [()]
        for fixed_values in fixed_iter:
            base_entry = dict(zip(fixed_names, fixed_values, strict=True))
            expected: tuple[Any, ...] | None = None
            for tag_value in tag_domain:
                full_entry = dict(base_entry)
                full_entry[tag_name] = tag_value
                outcome = self._scan(full_entry, (tag_name,))
                if outcome is None:
                    self._entry_sensitive_cache[cache_key] = True
                    return True
                if expected is None:
                    expected = outcome
                    continue
                if outcome != expected:
                    self._entry_sensitive_cache[cache_key] = True
                    return True

        self._entry_sensitive_cache[cache_key] = False
        return False

    def _is_retained_anchor(self, tag_name: str) -> bool:
        tag = self._graph.tags.get(tag_name)
        if tag is None:
            return False
        if tag_name not in self._written_tags:
            return True
        return bool(tag.external or tag.final)

    def _scoped_dependencies(
        self,
        candidate: str,
        observed: tuple[str, ...],
        retained: frozenset[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        retained_set = set(retained)
        observed_set = set(observed)
        retained_names = set(observed_set & retained_set)
        input_names: set[str] = set()
        hidden_stateful = set(observed_set - retained_set)
        queue: deque[str] = deque([candidate, *observed])
        visited: set[str] = set(queue)

        while queue:
            current = queue.popleft()
            if _is_fault_tag(current):
                continue
            for rung_idx in self._graph.writers_of.get(current, frozenset()):
                node = self._graph.rung_nodes[rung_idx]
                for src in node.condition_reads | node.data_reads:
                    if _is_fault_tag(src):
                        continue
                    if src in self._nondeterministic_dims:
                        input_names.add(src)
                        continue
                    if src in retained_set and self._is_retained_anchor(src):
                        retained_names.add(src)
                        continue
                    if src in self._state_basis and src not in retained_set and src != candidate:
                        hidden_stateful.add(src)
                    if src in visited:
                        continue
                    visited.add(src)
                    queue.append(src)

        return (
            tuple(sorted(retained_names)),
            tuple(sorted(input_names)),
            tuple(sorted(hidden_stateful)),
        )

    def _scan(
        self,
        entry_values: Mapping[str, Any],
        observed: tuple[str, ...],
    ) -> tuple[Any, ...] | None:
        kernel = self._compiled.create_kernel()
        kernel.tags.update(entry_values)
        _step_compiled_kernel(self._compiled, kernel, dt=_DEFAULT_DT)
        result = tuple(kernel.tags.get(name) for name in observed)

        if self._warm_memory is not None:
            # Re-run with warm memory to detect hidden memory-dependent
            # behaviour (e.g. oneshot instructions).  If outcomes differ
            # the caller must treat the configuration as non-elidable.
            warm_kernel = self._compiled.create_kernel()
            warm_kernel.tags.update(entry_values)
            warm_kernel.memory.update(self._warm_memory)
            _step_compiled_kernel(self._compiled, warm_kernel, dt=_DEFAULT_DT)
            warm_result = tuple(warm_kernel.tags.get(name) for name in observed)
            if warm_result != result:
                return None

        return result


def _elide_scan_local_stateful_dims(
    program: Program,
    graph: ProgramGraph,
    stateful_dims: Mapping[str, tuple[Any, ...]],
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    *,
    compiled: CompiledKernel | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, tuple[Any, ...]]:
    """Return a reduced stateful-dimension map after conservative elision.

    Two-phase hybrid: abstract provenance analysis first (fast, handles most
    cases), then concrete kernel proofs on whatever abstract retained.
    """
    if not stateful_dims:
        return {}

    def _emit(msg: str) -> None:
        if progress is not None:
            progress(msg)

    _emit(
        "elision | starting scan-local state elision"
        f" | stateful={len(stateful_dims):,}"
        f" | inputs={len(nondeterministic_dims):,}"
    )

    # Phase 1: abstract provenance analysis
    abstract_elider = _ScanLocalStateElider(program, graph, stateful_dims, nondeterministic_dims)
    abstract_reduced, _accepted = abstract_elider.elide()
    abstract_removed = len(stateful_dims) - len(abstract_reduced)
    _emit(
        f"elision | abstract phase complete"
        f" | removed={abstract_removed:,}"
        f" | retained={len(abstract_reduced):,}"
    )

    if not abstract_reduced:
        _emit(f"elision complete | removed={len(stateful_dims):,} | retained=0")
        return {}

    # Phase 2: concrete kernel proofs on what abstract couldn't resolve.
    # Abstract-removed tags can still act as same-scan observers, so keep the
    # full observer set.  Only the abstract-retained tags remain as concrete
    # state dimensions that the proof may need to vary.
    concrete_elider = _ConcreteStateElider(
        program,
        graph,
        stateful_dims,
        nondeterministic_dims,
        state_basis=frozenset(abstract_reduced),
        compiled=compiled,
        progress=progress,
    )
    abstract_retained_names = frozenset(abstract_reduced)
    concrete_retained = set(abstract_retained_names)
    changed = True
    while changed:
        changed = False
        snapshot = set(concrete_retained)
        for tag_name in sorted(snapshot):
            if not concrete_elider._is_concrete_candidate(tag_name):
                continue
            if tag_name not in abstract_retained_names:
                continue
            compare_retained = frozenset(snapshot - {tag_name})
            if concrete_elider._can_elide(tag_name, compare_retained):
                concrete_retained.discard(tag_name)
                changed = True
    concrete_elider._emit(
        "elision complete"
        f" | removed={len(stateful_dims) - len(concrete_retained):,}"
        f" | retained={len(concrete_retained):,}"
    )
    return {name: domain for name, domain in stateful_dims.items() if name in concrete_retained}
