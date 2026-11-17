"""Over-approximate value sets of the tags pointers are built from.

``_ValueSets`` bounds what each tag can hold, as a least fixpoint.  It starts from the
tag's default, its writers and the indirect writes that may reach its slot.  From those
sets it works out the addresses that an indexed access (``DS[Ptr]``,
``DS[Ptr * 10 + 2991]``) can resolve to.  It lives below the PDG so that the PDG and the
validators can both use it.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Collection, Iterable, Iterator, Mapping
from typing import Any

from pyrung.core.condition import (
    AllCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    Condition,
)
from pyrung.core.expression import CALC_FAULTS, Expression
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.conversions import _truncate_to_tag_type
from pyrung.core.instruction.data_transfer import CopyInstruction
from pyrung.core.instruction.resolvers import resolve_tag_or_value_ctx
from pyrung.core.memory_block import (
    Block,
    BlockRange,
    IndirectBlockRange,
    IndirectExprRef,
    IndirectRef,
)
from pyrung.core.tag import ImmediateRef, Tag, TagType, tag_written_from_outside

# Largest value set kept per tag, and largest cross product evaluated.
_VALUE_CAP = 4096
# Times one tag's value set may grow before it is taken as unbounded.
_MAX_GROWTH = 16
# Largest block scanned for a tag's slot address.
_MAX_BLOCK_SCAN = 100_000

_INDIRECT = (IndirectRef, IndirectExprRef, IndirectBlockRange)


def _scan(value: Any) -> tuple[set[str], list[Any]]:
    """Return the tag names a value reads, and the indirect references inside it."""
    from pyrung.core.validation.walker import _condition_children

    names: set[str] = set()
    indirect: list[Any] = []
    seen: set[int] = set()

    def walk(current: Any) -> None:
        if current is None or isinstance(current, (bool, int, float, str, bytes, bytearray)):
            return
        if id(current) in seen:
            return
        seen.add(id(current))
        if isinstance(current, ImmediateRef):
            walk(current.value)
        elif isinstance(current, Tag):
            names.add(current.name)
        elif isinstance(current, BlockRange):
            names.update(tag.name for tag in current.tags())
        elif isinstance(current, IndirectBlockRange):
            indirect.append(current)
            walk(current.start_expr)
            walk(current.end_expr)
        elif isinstance(current, IndirectRef):
            indirect.append(current)
            walk(current.pointer)
        elif isinstance(current, IndirectExprRef):
            indirect.append(current)
            walk(current.expr)
        elif isinstance(current, Condition):
            for _, child in _condition_children(current):
                walk(child)
        elif isinstance(current, Expression):
            for key, item in vars(current).items():
                if not key.startswith("_"):
                    walk(item)
        elif isinstance(current, dict):
            for item in current.values():
                walk(item)
        elif isinstance(current, (list, tuple, set, frozenset)):
            for item in current:
                walk(item)
        elif hasattr(current, "__dict__"):
            for key, item in vars(current).items():
                if not key.startswith("_"):
                    walk(item)

    walk(value)
    return names, indirect


def _targets(instr: Any) -> list[Any]:
    from pyrung.core.analysis.write_sites import instruction_write_targets

    out = []
    for target in instruction_write_targets(instr):
        while isinstance(target, ImmediateRef):
            target = target.value
        out.append(target)
    return out


def _is_plain_copy(instr: Any) -> bool:
    return isinstance(instr, CopyInstruction) and instr.convert is None


class _ValueSets:
    """Over-approximate value sets of the tags pointers are built from.

    A tag's set holds its default, its initial value, every value a static writer can
    store, and every value that an indirect write that may reach its slot can store.  ``None``
    means unbounded.  The sets are a least fixpoint.  So a pointer whose slot an indirect
    write may hit picks up the values that the write stores.

    With *follow_indirect_reads*, an indexed read on its own (``copy(DS[Idx], T)``)
    takes the union of the sets of the elements that its addresses reach.  A state machine
    that steps through a jump table (``Idx = Requested + 150``, ``Requested = T``) then
    stays bounded.  With *declared_inputs*, an input with declared ``choices`` or
    ``min``/``max`` holds those values and its default, as the verifier explores it.
    Without *declared_inputs* it holds any value.

    With *declared_if_unwritten* too, only an input that no instruction writes holds its
    declared values.  A ladder write past a declared bound stores, as on a CLICK.  So an
    input that the ladder also writes holds any value.

    *listed_reach* gives the addresses of some writes through a pointer, by ``id`` of
    the destination.  The PDG lists those addresses from the pointer's declared bounds,
    so these value sets reach the same elements.

    *guards* (from :func:`rung_guards`) bound the tags that an instruction reads, using
    its rung's comparisons.  ``math(Cmd * 10 + 2999, Idx)`` under ``Cmd >= 1, Cmd <= 100``
    stores only 3009..3999, even when ``Cmd`` alone could hold anything.

    A tag in *written_first* is written, unconditionally, before anything reads it by name
    (``ProgramGraph.unconditional_write_before_read``).  Its default never reaches those
    reads, so its set holds only what its writers store (``HistIdIdx = HistorianId * 10 +
    2991``, never 0 where it indexes).  An index can read its slot at any point in the
    scan, so an indexed read still takes its default.
    """

    def __init__(
        self,
        instructions: Iterable[Any],
        tags: Mapping[str, Tag],
        unbounded: Collection[str],
        initial_state: Mapping[str, Any] | None,
        *,
        follow_indirect_reads: bool = False,
        declared_inputs: bool = False,
        written_first: Collection[str] = (),
        guards: Mapping[int, Mapping[str, _Bound]] | None = None,
        declared_if_unwritten: bool = False,
        listed_reach: Mapping[int, frozenset[int]] | None = None,
    ) -> None:
        self._listed_reach = listed_reach or {}
        self._tags = tags
        self._guards = guards or {}
        self._declared_if_unwritten = declared_if_unwritten
        self._ladder_written: dict[str, bool] = {}
        self._written_first = written_first
        self._follow_indirect_reads = follow_indirect_reads
        self._declared_inputs = declared_inputs
        self._unbounded = unbounded
        self._initial = initial_state or {}
        self._static: dict[str, list[tuple[Any, Any]]] = {}
        self._indirect: list[tuple[Any, Any]] = []
        self._slot_index: dict[int, dict[str, frozenset[int]] | None] = {}
        for instr in instructions:
            for target in _targets(instr):
                if isinstance(target, Tag):
                    self._static.setdefault(target.name, []).append((instr, target))
                elif isinstance(target, BlockRange):
                    for tag in target.tags():
                        self._static.setdefault(tag.name, []).append((instr, None))
                elif isinstance(target, _INDIRECT):
                    self._indirect.append((instr, target))
        self._values: dict[str, frozenset[Any] | None] | None = None

    def slots(self, block: Block, name: str) -> frozenset[int] | None:
        """Return the addresses in *block* that name *name*, or None if the block is too large."""
        key = id(block)
        if key not in self._slot_index:
            index: dict[str, set[int]] | None = None
            if block.end - block.start + 1 <= _MAX_BLOCK_SCAN:
                index = {}
                for addr in range(block.start, block.end + 1):
                    index.setdefault(block._effective_slot_name(addr), set()).add(addr)
            self._slot_index[key] = (
                None if index is None else {k: frozenset(v) for k, v in index.items()}
            )
        found = self._slot_index[key]
        if found is None:
            return None
        return found.get(name, frozenset())

    def addresses(self, ref: Any, values=None) -> frozenset[int] | None:
        """Return every address a pointer or index expression can use, or None if unbounded.

        With *values*, the sets come from that mapping of tag names, not the fixpoint.
        """
        if isinstance(ref, IndirectRef):
            domain = (
                self.values(ref.pointer.name) if values is None else values.get(ref.pointer.name)
            )
            if domain is None or any(type(v) is not int for v in domain):
                return None
            return domain
        if isinstance(ref, IndirectExprRef):
            return self.integers(ref.expr, values)
        return None

    def write_addresses(self, instr: Any, ref: Any) -> frozenset[int] | None:
        """Return every address *instr* can write through *ref*, under its rung's guards."""
        if self._values is None:
            self._values = self._fixpoint()
        values = self._guarded(instr, self._values)
        if isinstance(ref, IndirectBlockRange):
            return self.window(ref, values)
        return self.addresses(ref, values)

    def window(self, ref: IndirectBlockRange, values=None) -> frozenset[int] | None:
        """Return every address ``block.select(start, end)`` can cover.

        That is the range from the lowest start to the highest end.
        """
        starts = self.integers(ref.start_expr, values)
        ends = self.integers(ref.end_expr, values)
        if starts is None or ends is None:
            return None
        if not starts or not ends:
            return frozenset()  # no value yet, so no address yet
        lo, hi = min(starts), max(ends)
        if hi - lo >= _VALUE_CAP:
            return None
        return frozenset(range(lo, hi + 1))

    def integers(self, expr: Any, values=None) -> frozenset[int] | None:
        """Return the integer values *expr* takes, or None if unbounded."""
        if values is None and self._values is None:
            self._values = self._fixpoint()
        out = self._evaluate(expr, values)
        if out is None:
            return None
        try:
            return frozenset(int(v) for v in out)
        except (TypeError, ValueError, OverflowError):
            return None

    def values(self, name: str) -> frozenset[Any] | None:
        if self._values is None:
            self._values = self._fixpoint()
        return self._values.get(name)

    def _evaluate(self, expr: Any, values: Mapping[str, frozenset[Any] | None] | None = None):
        """Return every value *expr* takes over the value sets of the tags it reads."""
        lookup = self._values if values is None else values
        if lookup is None:
            return None
        if self._follow_indirect_reads and isinstance(expr, (IndirectRef, IndirectExprRef)):
            return self._indexed_values(expr, lookup)
        names, indirect = _scan(expr)
        if indirect:
            return None
        ordered = sorted(names)
        domains: list[frozenset[Any]] = []
        size = 1
        for name in ordered:
            domain = lookup.get(name)
            if domain is None:
                return None
            size *= max(len(domain), 1)
            if size > _VALUE_CAP:
                return None
            domains.append(domain)
        out: set[Any] = set()
        for combo in itertools.product(*domains):
            probe = _Probe(dict(zip(ordered, combo, strict=True)))
            try:
                out.add(resolve_tag_or_value_ctx(expr, probe))  # ty: ignore[invalid-argument-type]
            except Exception:  # noqa: BLE001 - anything unexpected is unbounded
                return None
        return frozenset(out)

    def _indexed_values(self, ref: Any, lookup: Mapping[str, frozenset[Any] | None]):
        """Return every value an indexed read can return, from its elements' sets."""
        addresses = self.addresses(ref, lookup)
        if addresses is None:
            return None
        block = ref.block
        out: set[Any] = set()
        for addr in addresses:
            if addr > block.end:
                out.add(block._type_default())  # a read past the block reads zero
                continue
            if addr < block.start:
                continue  # a read below the block faults and stores nothing
            tag = block._get_tag(addr)
            if tag.name in lookup:
                domain = lookup[tag.name]
                if domain is not None and tag.name in self._written_first:
                    domain = domain | self._initial_values(tag)
            elif tag_written_from_outside(tag):
                domain = None
            else:
                domain = frozenset({tag.default})  # unwritten storage
            if domain is None:
                return None
            out |= domain
        return frozenset(out)

    def _guarded(self, instr: Any, values):
        """Return *values* as *instr* reads them, narrowed by its rung's guards."""
        bounds = self._guards.get(id(instr))
        if not bounds or values is None:
            return values
        return _Guarded(values, bounds)

    def _store(self, instr: Any, dest: Tag, values) -> frozenset[Any] | None:
        """Return the values *instr* stores into *dest*, given the current value sets."""
        values = self._guarded(instr, values)
        if isinstance(instr, CalcInstruction):
            # This follows CalcInstruction.execute.  A failing operation gives 0 and the
            # formula carries on.  Its flag is noted, and dropped here.  An Inf result
            # stays Inf in a REAL and becomes 0 in an integer register.
            token = CALC_FAULTS.set([])
            try:
                raw = self._evaluate(instr.expression, values)
            finally:
                CALC_FAULTS.reset(token)
            if raw is None:
                return None
            out: set[Any] = set()
            for value in raw:
                try:
                    stored = _truncate_to_tag_type(value, dest, instr.mode)
                except Exception:  # noqa: BLE001
                    return None
                if isinstance(stored, float) and math.isnan(stored):
                    return None  # NaN equals nothing: no set holds it
                out.add(stored)
            return frozenset(out)
        if _is_plain_copy(instr):
            raw = self._evaluate(instr.source, values)
            if raw is None:
                return None
            for value in raw:
                # Copy clamps; only an in-range value is stored unchanged.
                try:
                    if _truncate_to_tag_type(value, dest) != value:
                        return None
                except Exception:  # noqa: BLE001
                    return None
            return raw
        return None

    def _base(self, name: str) -> frozenset[Any] | None:
        tag = self._tags.get(name)
        if tag is None:
            return None
        if name in self._unbounded or tag_written_from_outside(tag):
            if not self._declared_inputs:
                return None
            if self._declared_if_unwritten and self._written_by_ladder(name):
                return None
            return self.declared(tag)
        if name in self._written_first:
            return frozenset()
        return self._initial_values(tag)

    def _written_by_ladder(self, name: str) -> bool:
        """Return whether an instruction may write *name*, by name or through a pointer."""
        if name not in self._ladder_written:
            self._ladder_written[name] = name in self._static or any(
                self.slots(ref.block, name) != frozenset() for _instr, ref in self._indirect
            )
        return self._ladder_written[name]

    def _initial_values(self, tag: Tag) -> frozenset[Any]:
        """Return the values *tag* holds before anything writes it."""
        if tag.name in self._initial:
            return frozenset({tag.default, self._initial[tag.name]})
        return frozenset({tag.default})

    @staticmethod
    def declared(tag: Tag) -> frozenset[Any] | None:
        """Return an input's declared values and its default, or None if undeclared."""
        if tag.choices is not None:
            return frozenset({*tag.choices, tag.default})
        if type(tag.min) is int and type(tag.max) is int and tag.max - tag.min < _VALUE_CAP:
            return frozenset({*range(tag.min, tag.max + 1), tag.default})
        return None

    def _fixpoint(self) -> dict[str, frozenset[Any] | None]:
        names = sorted(self._tags)
        values: dict[str, frozenset[Any] | None] = {name: frozenset() for name in names}
        growth = dict.fromkeys(names, 0)
        # Each tag grows at most _MAX_GROWTH times before it widens to unbounded (a
        # self-feeding count).  So the number of rounds is bounded.
        for _ in range((_MAX_GROWTH + 2) * (len(names) + 1)):
            changed = False
            # Work out each indirect write's addresses once a round.  The last round changes
            # nothing, so its addresses are the addresses of the result.
            self._round_reach: dict[tuple[int, int], frozenset[int] | None] = {}
            for name in names:
                current = values[name]
                if current is None:
                    continue
                new = self._round(name, values)
                if new is not None and len(new) > _VALUE_CAP:
                    new = None
                if new != current:
                    growth[name] += 1
                    values[name] = None if growth[name] > _MAX_GROWTH else new
                    changed = True
            if not changed:
                return values
        return {}

    def _round(self, name: str, values) -> frozenset[Any] | None:
        base = self._base(name)
        if base is None:
            return None
        out = set(base)
        tag = self._tags[name]
        for instr, dest in self._static.get(name, ()):
            if dest is None:
                return None
            stored = self._store(instr, dest, values)
            if stored is None:
                return None
            out |= stored
        for instr, ref in self._indirect:
            slots = self.slots(ref.block, name)
            if slots is None:
                return None
            if not slots:
                continue
            key = (id(instr), id(ref))
            if key not in self._round_reach:
                guarded = self._guarded(instr, values)
                self._round_reach[key] = (
                    self._listed_reach[id(ref)]
                    if id(ref) in self._listed_reach
                    else self.window(ref, guarded)
                    if isinstance(ref, IndirectBlockRange)
                    else self.addresses(ref, guarded)
                )
            addresses = self._round_reach[key]
            if isinstance(ref, IndirectBlockRange):
                # Which element gets which source value depends on position.  So the set is
                # unbounded, unless the window cannot reach this tag's slot at all.
                if addresses is not None and not (addresses & slots):
                    continue
                return None
            if addresses is not None and not (addresses & slots):
                continue
            stored = self._store(instr, tag, values)
            if stored is None:
                return None
            out |= stored
        return frozenset(out)


#: A guard's bounds on one tag, as its lowest and highest value.  Either may be open.
_Bound = tuple[int | None, int | None]
_GUARDED_TYPES = frozenset({TagType.INT, TagType.DINT, TagType.WORD})
_CALLS = frozenset(
    {
        "CallInstruction",
        "ForLoopInstruction",
        "FunctionCallInstruction",
        "EnabledFunctionCallInstruction",
    }
)


class _Guarded(Mapping[str, frozenset[Any] | None]):
    """Value sets as one instruction reads them, each guarded tag inside its bounds."""

    def __init__(self, values: Mapping[str, frozenset[Any] | None], bounds: Mapping[str, _Bound]):
        self._values = values
        self._bounds = bounds

    def __getitem__(self, name: str) -> frozenset[Any] | None:
        bound = self._bounds.get(name)
        if bound is None:
            return self._values[name]
        return _within(self._values.get(name), bound)

    def __contains__(self, name: object) -> bool:
        return name in self._values or name in self._bounds

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def _within(domain: frozenset[Any] | None, bound: _Bound) -> frozenset[Any] | None:
    """Return every value of a closed *bound*, else the values of *domain* inside it.

    A closed bound gives its whole range, so the read stays the same set from round to
    round.  Otherwise a count that grows by one a round (a loop index) would carry each tag
    it feeds to the growth limit.
    """
    lo, hi = bound
    if lo is not None and hi is not None and hi - lo < _VALUE_CAP:
        return frozenset(range(lo, hi + 1))
    if domain is None:
        return None
    return frozenset(
        v
        for v in domain
        if not isinstance(v, (int, float)) or ((lo is None or v >= lo) and (hi is None or v <= hi))
    )


def rung_guards(program: Any) -> dict[int, dict[str, _Bound]]:
    """Return the bounds each instruction's rung puts on the tags it reads, by ``id``.

    An instruction that does nothing while its row has no power
    (``is_inert_when_disabled()``) writes only when the row's conditions hold.  So an
    And-ed comparison of an integer tag with a constant (``Cmd >= 1``) holds when it
    writes.  An instruction that writes on a false rung too, such as
    ``run_enabled_function()`` or a timer, gets no bound.  An earlier write in the same
    rung drops the bound on the tag it writes.  A call, or a write through a pointer,
    drops every bound after it.  An Or bounds nothing, and neither does a For loop.
    """
    out: dict[int, dict[str, _Bound]] = {}
    for rungs in (program.rungs, *program.subroutines.values()):
        for rung in rungs:
            _visit_row(rung, out, set(), [False])
    return out


def _visit_row(
    row: Any, out: dict[int, dict[str, _Bound]], written: set[str], unknown: list[bool]
) -> None:
    """Record the guards of each instruction in *row* and its branches, in run order.

    A branch's conditions hold its parents' conditions too.  *written* and *unknown* carry
    across the whole rung, because a write in one branch comes before the instructions
    after it.
    """
    from pyrung.core.rung import Rung

    bounds = _condition_bounds(row._conditions)
    for item in row._execution_items:
        if isinstance(item, Rung):
            _visit_row(item, out, written, unknown)
            continue
        inert = getattr(item, "is_inert_when_disabled", None)
        if not unknown[0] and callable(inert) and inert():
            live = {name: b for name, b in bounds.items() if name not in written}
            if live:
                out[id(item)] = live
        names = _written_by(item)
        if names is None:
            unknown[0] = True
        else:
            written.update(names)


def _written_by(instr: Any) -> frozenset[str] | None:
    """Return the tags *instr* writes, or None if it may write any of them."""
    from pyrung.core.analysis.write_sites import static_write_target_names

    if type(instr).__name__ in _CALLS:
        return None
    names: set[str] = set()
    for target in _targets(instr):
        found = static_write_target_names(target)
        if not found:
            return None
        names |= found
    return frozenset(names)


def _condition_bounds(conditions: Iterable[Any]) -> dict[str, _Bound]:
    """Return the bounds that And-ed comparisons of a tag with a constant put on that tag."""
    out: dict[str, _Bound] = {}
    for cond in conditions:
        if isinstance(cond, AllCondition):
            found = _condition_bounds(cond.conditions)
        else:
            one = _compare_bound(cond)
            found = {} if one is None else {one[0]: one[1]}
        for name, bound in found.items():
            out[name] = _meet(out.get(name), bound)
    return out


def _compare_bound(cond: Any) -> tuple[str, _Bound] | None:
    if not isinstance(cond, (CompareEq, CompareLt, CompareLe, CompareGt, CompareGe)):
        return None
    tag, value = cond.tag, cond.value
    if not isinstance(tag, Tag) or tag.type not in _GUARDED_TYPES:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if isinstance(cond, CompareEq):
        bound: _Bound = (math.ceil(value), math.floor(value))
    elif isinstance(cond, CompareGe):
        bound = (math.ceil(value), None)
    elif isinstance(cond, CompareGt):
        bound = (math.floor(value) + 1, None)
    elif isinstance(cond, CompareLe):
        bound = (None, math.floor(value))
    else:
        bound = (None, math.ceil(value) - 1)
    return tag.name, bound


def _meet(left: _Bound | None, right: _Bound) -> _Bound:
    if left is None:
        return right
    los = [b for b in (left[0], right[0]) if b is not None]
    his = [b for b in (left[1], right[1]) if b is not None]
    return (max(los) if los else None, min(his) if his else None)


class _Probe:
    """A read-only scan context over one assignment of tag values."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get_tag(self, name: str, default: Any = None) -> Any:
        return self._values.get(name, default)
