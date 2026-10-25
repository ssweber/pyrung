"""Over-approximate value sets of the tags pointers are built from.

``_ValueSets`` bounds what each tag can hold from its default, its writers and
the indirect writes that may reach its slot, as a least fixpoint, and from
that the addresses an indexed access (``DS[Ptr]``, ``DS[Ptr * 10 + 2991]``)
can resolve to.  It lives below the PDG and the prover so both can use it:
``prove/deadness.py`` for its dead-guard check, the PDG for the elements an
index with no declared bounds reads.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Collection, Iterable, Mapping
from typing import Any

from pyrung.core.condition import Condition
from pyrung.core.expression import Expression
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
from pyrung.core.tag import ImmediateRef, InputTag, Tag

# Largest value set kept per tag, and largest cross product evaluated.
_VALUE_CAP = 4096
# Times one tag's value set may grow before it is taken as unbounded.
_MAX_GROWTH = 16
# Largest block scanned for a tag's slot address.
_MAX_BLOCK_SCAN = 100_000

_INDIRECT = (IndirectRef, IndirectExprRef, IndirectBlockRange)


def _scan(value: Any) -> tuple[set[str], list[Any]]:
    """Tag names a value reads, and the indirect references inside it."""
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

    A tag's set holds its default, its initial value, every value a static
    writer can store and every value an indirect write that may reach its
    slot can store; ``None`` means unbounded.  Computed as a least fixpoint,
    so a pointer whose slot an indirect write may hit picks up what it writes.

    With *follow_indirect_reads*, an indexed read on its own (``copy(DS[Idx],
    T)``) takes the union of the sets of the elements its addresses reach, so
    a state machine stepping through a jump table (``Idx = Requested + 150``,
    ``Requested = T``) stays bounded.  With *declared_inputs*, an input with
    declared ``choices`` or ``min``/``max`` holds those values (and its
    default), as the verifier explores it, instead of any value.

    A tag in *written_first* is written, unconditionally, before anything
    reads it by name (``ProgramGraph.unconditional_write_before_read``), so its
    default never reaches those reads and its set holds only what its writers
    store (``HistIdIdx = HistorianId * 10 + 2991``, never 0 where it indexes).
    An index can read its slot at any point in the scan, so an indexed read
    still takes its default.
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
    ) -> None:
        self._tags = tags
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
        """Addresses in *block* that name *name* (None: block too large to scan)."""
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

    def may_reach(self, ref: Any, name: str) -> bool:
        """Whether an indirect reference may resolve to the tag *name*."""
        slots = self.slots(ref.block, name)
        if slots is None:
            return True
        if not slots:
            return False
        addresses = self.addresses(ref)
        return addresses is None or bool(addresses & slots)

    def addresses(self, ref: Any) -> frozenset[int] | None:
        if isinstance(ref, IndirectRef):
            values = self.values(ref.pointer.name)
            if values is None or any(type(v) is not int for v in values):
                return None
            return values
        if isinstance(ref, IndirectExprRef):
            return self._integers(ref.expr)
        return None

    def window(self, ref: IndirectBlockRange, values=None) -> frozenset[int] | None:
        """Every address ``block.select(start, end)`` can cover: lowest start to highest end."""
        starts = self._integers(ref.start_expr, values)
        ends = self._integers(ref.end_expr, values)
        if not starts or not ends:
            return None
        lo, hi = min(starts), max(ends)
        if hi - lo >= _VALUE_CAP:
            return None
        return frozenset(range(lo, hi + 1))

    def _integers(self, expr: Any, values=None) -> frozenset[int] | None:
        """The integer values *expr* takes, or None when unbounded."""
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
        """Every value *expr* takes over the value sets of the tags it reads."""
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
            except ZeroDivisionError:
                out.add(0)
            except Exception:  # noqa: BLE001 - anything unexpected is unbounded
                return None
        return frozenset(out)

    def _indexed_values(self, ref: Any, lookup: Mapping[str, frozenset[Any] | None]):
        """Every value an indexed read can return, from its elements' sets."""
        addresses = self._addresses_with(ref, lookup)
        if addresses is None:
            return None
        block = ref.block
        out: set[Any] = set()
        for addr in addresses:
            if not block.start <= addr <= block.end:
                continue  # an out-of-range read faults and stores nothing
            tag = block._get_tag(addr)
            if tag.name in lookup:
                domain = lookup[tag.name]
                if domain is not None and tag.name in self._written_first:
                    domain = domain | self._initial_values(tag)
            elif tag.external or isinstance(tag, InputTag):
                domain = None
            else:
                domain = frozenset({tag.default})  # unwritten storage
            if domain is None:
                return None
            out |= domain
        return frozenset(out)

    def _store(self, instr: Any, dest: Tag, values) -> frozenset[Any] | None:
        """Values *instr* stores into *dest*, given the current value sets."""
        if isinstance(instr, CalcInstruction):
            raw = self._evaluate(instr.expression, values)
            if raw is None:
                return None
            out: set[Any] = set()
            for value in raw:
                if isinstance(value, float) and not math.isfinite(value):
                    value = 0
                try:
                    out.add(_truncate_to_tag_type(value, dest, instr.mode))
                except Exception:  # noqa: BLE001
                    return None
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
        if name in self._unbounded or tag.external or isinstance(tag, InputTag):
            return self._declared(tag) if self._declared_inputs else None
        if name in self._written_first:
            return frozenset()
        return self._initial_values(tag)

    def _initial_values(self, tag: Tag) -> frozenset[Any]:
        """The values *tag* holds before anything writes it."""
        if tag.name in self._initial:
            return frozenset({tag.default, self._initial[tag.name]})
        return frozenset({tag.default})

    @staticmethod
    def _declared(tag: Tag) -> frozenset[Any] | None:
        """An input's declared values and its default, or None when undeclared."""
        if tag.choices is not None:
            return frozenset({*tag.choices, tag.default})
        if type(tag.min) is int and type(tag.max) is int and tag.max - tag.min < _VALUE_CAP:
            return frozenset({*range(tag.min, tag.max + 1), tag.default})
        return None

    def _fixpoint(self) -> dict[str, frozenset[Any] | None]:
        names = sorted(self._tags)
        values: dict[str, frozenset[Any] | None] = {name: frozenset() for name in names}
        growth = dict.fromkeys(names, 0)
        # Each tag grows at most _MAX_GROWTH times before it is widened to
        # unbounded (a self-feeding count), so the rounds are bounded.
        for _ in range((_MAX_GROWTH + 2) * (len(names) + 1)):
            changed = False
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
            if isinstance(ref, IndirectBlockRange):
                # Which element gets which source value is positional: unbounded,
                # unless the window cannot reach this tag's slot at all.
                window = self.window(ref, values)
                if window is not None and not (window & slots):
                    continue
                return None
            addresses = self._addresses_with(ref, values)
            if addresses is not None and not (addresses & slots):
                continue
            stored = self._store(instr, tag, values)
            if stored is None:
                return None
            out |= stored
        return frozenset(out)

    def _addresses_with(self, ref: Any, values) -> frozenset[int] | None:
        if isinstance(ref, IndirectRef):
            domain = values.get(ref.pointer.name)
            if domain is None or any(type(v) is not int for v in domain):
                return None
            return domain
        if isinstance(ref, IndirectExprRef):
            return self._integers(ref.expr, values)
        return None


class _Probe:
    """A read-only scan context over one assignment of tag values."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get_tag(self, name: str, default: Any = None) -> Any:
        return self._values.get(name, default)
