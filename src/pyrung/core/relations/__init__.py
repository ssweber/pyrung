"""What an instruction's generated code reads, writes and compares, read from the code itself.

The kernel codegen records each piece it writes (``records``) when a recorder is set.
:func:`relations_for` renders a stand-in copy of an instruction with the recorder on,
finds the tests that decide whether each record runs (``_guards``), and answers questions
about the instruction from those records.  The analysis reads these answers in place of
facts kept by hand beside each instruction class, so the facts cannot drift from the code
that runs.

An instruction's shape is everything about it that changes its generated code.  That is
its class, which of its fields are set, the type of each tag, and the kind of each
literal.  Two instructions of one shape get the same code apart from their tag names and
numbers, so one render answers for both.

The code may hold a read, a write or a compare that no record covers.  Then the relations
are opaque.  Each query then gives its most cautious answer, or the answer pyrung kept by
hand before.  The same goes for an instruction that cannot be rendered as a stand-in.

Only timers and counters record so far.  :func:`relations_for` returns None for any other
instruction, and each reader keeps its old answer for those.

A snapshot of tag values holds no instruction memory.  So the off delay's first step
(:func:`~pyrung.core.instruction.advance.recorded_prepare`) cannot read whether the timer
has been enabled before.  It asks for that step whenever Done and Acc cannot show it.
"""

from __future__ import annotations

import functools
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pyrung.core.relations._guards import (
    AllOf,
    AnyOf,
    Atom,
    Not,
    Placed,
    Test,
    constant,
    read_field,
)
from pyrung.core.relations.records import (
    Compare,
    Const,
    Memory,
    Read,
    Record,
    Rise,
    Setting,
    Step,
    Store,
    TagValue,
)

__all__ = ["Gate", "Relations", "relations_for", "term_value"]

Levels = tuple[tuple[str, bool], ...]


@dataclass(frozen=True)
class Gate:
    """A memory test Acc's step waits on, and a constant store that makes the test pass from tag values alone.

    The off delay times only while ``armed or Done or Acc != 0`` holds.  Its enabled
    branch stores Done True, which passes that test without the armed cell.
    """

    test: Test
    value: bool
    # The tag field the store writes, and the constant it writes.
    setter: str
    setter_value: Any
    # The condition fields, with their values, that run the store.
    holds: Levels


@dataclass(frozen=True)
class Relations:
    """The records of one instruction shape, with the field each stand-in tag stands for."""

    placed: tuple[Placed, ...]
    # Stand-in tag name -> field name, such as ``pb_reset_condition`` -> ``reset_condition``.
    fields: Mapping[str, str]
    # The condition field that carries the rung's power.
    power: str | None
    # The condition fields this shape sets.  Opaque relations answer from these alone.
    conditions: tuple[str, ...] = ()
    # True when some code is not covered by a record, so no record can be trusted alone.
    opaque: bool = False

    def field(self, name: str) -> str | None:
        return self.fields.get(name)

    def levels(self, placed: Placed) -> Levels | None:
        """*placed*'s guard as condition fields and the value each must have.

        None when a test in the guard is anything but a condition field read as a level,
        or when the guard is only necessary.
        """
        if not placed.exact:
            return None
        result: list[tuple[str, bool]] = []
        for test, value in placed.guard:
            fixed = constant(test)
            if fixed is not None:
                if fixed is value:
                    continue
                return None
            level = _level(test, self.fields)
            if level is None:
                return None
            result.append((level[0], level[1] is value))
        return tuple(result)

    def _same_levels(self, places: tuple[Placed, ...]) -> Levels | None:
        """The levels of a record pasted in *places*, when every place has the same ones.

        The record runs when the guard of any place holds.  One list of levels that must
        all hold can stand for that only when the places agree.
        """
        found = {self.levels(p) for p in places}
        return found.pop() if len(found) == 1 else None

    def _places(self, record: Record) -> tuple[Placed, ...]:
        return tuple(p for p in self.placed if p.record is record)

    def stores(self, field: str) -> tuple[tuple[Placed, Store], ...]:
        name = _standin_name(field)
        return tuple(
            (p, p.record)
            for p in self.placed
            if isinstance(p.record, Store) and p.record.dst == name
        )

    def done_compares(self) -> tuple[tuple[Placed, Compare], ...]:
        """The stores that set Done from a compare of the updated Acc with the preset."""
        return tuple(
            (p, store.value)
            for p, store in self.stores("done_bit")
            if isinstance(store.value, Compare)
        )

    @functools.cached_property
    def completion_levels(self) -> Levels | None:
        """The condition fields, and their values, that run the store of Done's compare.

        None unless the code has one such store, and every place of it has the same plain
        levels.
        """
        places = tuple(p for p, _compare in self.done_compares())
        if not places or len({id(p.record) for p in places}) != 1:
            return None
        return self._same_levels(places)

    def steps(self) -> tuple[tuple[Placed, Step], ...]:
        return tuple((p, p.record) for p in self.placed if isinstance(p.record, Step))

    @functools.cached_property
    def edge_fields(self) -> frozenset[str]:
        """The condition fields the code reads through edge memory, on their rise."""
        if self.opaque:
            return frozenset(self.conditions)
        found: set[str] = set()
        for p in self.placed:
            operand = p.operand
            if isinstance(p.record, Rise) and operand is not None:
                field = read_field(operand, self.fields)
                if field is not None:
                    found.add(field)
        return frozenset(found)

    @functools.cached_property
    def counts_on_rise(self) -> bool:
        """True when a step of Acc runs only on the scan an input rises.

        A counter on a rung with no condition counts too, on the rise of a constant input.
        Opaque relations answer by kind, so a counter gives True and a timer gives False.
        """
        if self.opaque:
            return any(f in ("up_condition", "down_condition") for f in self.conditions)
        return any(
            step.delta is not None and any(value and _is_rise(test) for test, value in p.guard)
            for p, step in self.steps()
        )

    @functools.cached_property
    def snapshot_fields(self) -> frozenset[str]:
        """The condition fields the instruction reads itself, besides the rung's power."""
        if self.opaque:
            return frozenset(self.conditions) - {self.power}
        return frozenset(
            field
            for p in self.placed
            if isinstance(p.record, Read)
            and isinstance(p.record.term, TagValue)
            and (field := self.field(p.record.term.name)) is not None
            and field != self.power
            and field.endswith("_condition")
        )

    @functools.cached_property
    def zeroing_fields(self) -> frozenset[str]:
        """The condition fields, besides the rung's power, whose value decides a store of 0 in Acc.

        A field counts when a guard of the store reads it, on or off, so a guard that is
        only necessary is enough.  A test that is not a plain level counts every field it
        reads.
        """
        if self.opaque:
            return frozenset(self.conditions) - {self.power}
        found: set[str] = set()
        for p, store in self.stores("accumulator"):
            if store.value != Const(0):
                continue
            for test, _value in p.guard:
                found.update(_fields_read(test, self.fields))
        return frozenset(found) - {self.power}

    def count_holds(self, pulsed: str) -> Levels | None:
        """The level fields that must hold for a count on *pulsed*'s rise to change Acc.

        None when no step counts on *pulsed*'s rise, or a guard of one is not plain.  Opaque
        relations give the answer pyrung kept by hand before, a reset held off.
        """
        if self.opaque:
            return tuple(
                (f, False) for f in self.conditions if f == "reset_condition" and f != pulsed
            )
        holds: list[tuple[str, bool]] = []
        found = False
        for record in dict.fromkeys(step for _p, step in self.steps()):
            places = self._places(record)
            edges = {self._edge_field(p) for p in places}
            if record.delta is None or pulsed not in edges:
                continue
            if len(edges) != 1:
                return None
            found = True
            per_place = {self._count_holds_at(p) for p in places}
            if len(per_place) != 1 or None in per_place:
                return None
            holds.extend(per_place.pop() or ())
        return tuple(dict.fromkeys(holds)) if found else None

    def _count_holds_at(self, placed: Placed) -> Levels | None:
        if not placed.exact:
            return None
        holds: list[tuple[str, bool]] = []
        for test, value in placed.guard:
            if constant(test) is value:
                continue
            level = _level(test, self.fields)
            if level is not None:
                holds.append((level[0], level[1] is value))
            elif _rise_field(test, self) is None:
                return None
        return tuple(holds)

    @functools.cached_property
    def opposing_count_field(self) -> str | None:
        """The input whose rise steps Acc the other way from a rise of the rung's power.

        Opaque relations give the answer pyrung kept by hand before, any input that is
        neither the power nor the reset.
        """
        if self.opaque:
            return next(
                (f for f in self.conditions if f not in (self.power, "reset_condition")), None
            )
        power_signs = {
            step.delta
            for p, step in self.steps()
            if step.delta is not None and self._edge_field(p) == self.power
        }
        for p, step in self.steps():
            delta = step.delta
            field = self._edge_field(p)
            if delta is None or field is None or field == self.power:
                continue
            if any(sign * delta < 0 for sign in power_signs):
                return field
        return None

    @functools.cached_property
    def motion_gates(self) -> tuple[Gate, ...]:
        """Each memory test Acc's step waits on, with a constant store that makes it pass from tag values alone."""
        gates: list[Gate] = []
        for placed, _step in self.steps():
            for test, value in placed.guard:
                if not reads_memory(test) or any(g.test == test for g in gates):
                    continue
                gate = self._settle(test, value)
                if gate is None:
                    return ()
                gates.append(gate)
        return tuple(gates)

    def _settle(self, test: Test, value: bool) -> Gate | None:
        for p in self.placed:
            record = p.record
            if not (
                isinstance(record, Store)
                and isinstance(record.dst, str)
                and isinstance(record.value, Const)
            ):
                continue
            field = self.field(record.dst)
            levels = self._same_levels(self._places(record))
            if field is None or levels is None:
                continue
            if term_value(test, {field: record.value.value}, self) is value:
                return Gate(test, value, field, record.value.value, levels)
        return None

    def _edge_field(self, placed: Placed) -> str | None:
        for test, value in placed.guard:
            field = _rise_field(test, self)
            if field is not None and value:
                return field
        return None


def _standin_name(field: str) -> str:
    from pyrung.core.relations._render import PREFIX

    return PREFIX + field


def _level(test: Test, fields: Mapping[str, str]) -> tuple[str, bool] | None:
    """A test that reads one condition field as a level, and the value it reads it as."""
    if isinstance(test, Not):
        inner = _level(test.test, fields)
        return None if inner is None else (inner[0], not inner[1])
    field = read_field(test, fields)
    if field is None or not field.endswith("_condition"):
        return None
    return field, True


def _fields_read(test: Test, fields: Mapping[str, str]) -> set[str]:
    """Every condition field *test* reads as a level, at any depth."""
    if isinstance(test, Not):
        return _fields_read(test.test, fields)
    if isinstance(test, (AllOf, AnyOf)):
        return set().union(*(_fields_read(t, fields) for t in test.tests))
    field = read_field(test, fields)
    return {field} if field is not None and field.endswith("_condition") else set()


def _is_rise(test: Test) -> bool:
    return isinstance(test, Atom) and isinstance(test.record, Rise)


def _rise_field(test: Test, relations: Relations) -> str | None:
    if not isinstance(test, Atom) or not isinstance(test.record, Rise):
        return None
    for p in relations.placed:
        operand = p.operand
        if p.record == test.record and operand is not None:
            return read_field(operand, relations.fields)
    return None


def term_value(test: Test, values: Mapping[str, Any], relations: Relations) -> bool | None:
    """*test* on tag *values* keyed by field, or None when tag values cannot decide it.

    A read of instruction memory is never decided here, since the values hold no memory.
    """
    if isinstance(test, Not):
        inner = term_value(test.test, values, relations)
        return None if inner is None else not inner
    if isinstance(test, (AllOf, AnyOf)):
        results = [term_value(t, values, relations) for t in test.tests]
        if isinstance(test, AllOf):
            if False in results:
                return False
            return True if all(r is True for r in results) else None
        if True in results:
            return True
        return False if all(r is False for r in results) else None
    if not isinstance(test, Atom):
        return None
    record = test.record
    if isinstance(record, Read) and isinstance(record.term, TagValue):
        field = relations.field(record.term.name)
        value = values.get(field) if field is not None else None
        return None if value is None else bool(value)
    if isinstance(record, Compare):
        left = _term(record.left, values, relations)
        right = _term(record.right, values, relations)
        if left is None or right is None:
            return None
        try:
            return {
                "==": left == right,
                "!=": left != right,
                "<": left < right,
                "<=": left <= right,
                ">": left > right,
                ">=": left >= right,
            }[record.op]
        except (KeyError, TypeError):
            return None
    return None


def _term(term: Any, values: Mapping[str, Any], relations: Relations) -> Any:
    if isinstance(term, Const):
        return term.value
    if isinstance(term, Setting):
        return values.get(term.field)
    if isinstance(term, TagValue):
        field = relations.field(term.name)
        return values.get(field) if field is not None else None
    return None


def reads_memory(test: Test) -> bool:
    if isinstance(test, Not):
        return reads_memory(test.test)
    if isinstance(test, (AllOf, AnyOf)):
        return any(reads_memory(t) for t in test.tests)
    return (
        isinstance(test, Atom)
        and isinstance(test.record, Read)
        and isinstance(test.record.term, Memory)
    )


def _opaque(instr: Any) -> Relations:
    from pyrung.core.relations._render import power_field

    conditions = tuple(
        field for field in type(instr)._conditions if getattr(instr, field, None) is not None
    )
    return Relations((), {}, power_field(instr), conditions, opaque=True)


# Shapes are few in a real program, but a generated one can have many.
_CACHE_SIZE = 256
_CACHE: OrderedDict[tuple[Any, ...], Relations] = OrderedDict()


def relations_for(instr: Any) -> Relations | None:
    """The relations of *instr*'s shape, or None when its class does not record yet."""
    from pyrung.core.relations._guards import place, uncovered
    from pyrung.core.relations._render import RelationsError, _supported, render, shape_key

    if type(instr) not in _supported():
        return None
    key = shape_key(instr)
    if key is None:
        return _opaque(instr)
    cached = _CACHE.get(key)
    if cached is not None:
        _CACHE.move_to_end(key)
        return cached
    try:
        rendered = render(instr)
    except RelationsError:
        result = _opaque(instr)
    else:
        if uncovered(rendered.source, rendered.records, rendered.symbols):
            result = _opaque(instr)
        else:
            standins = dict(rendered.fields)
            conditions = tuple(f for f in standins.values() if f.endswith("_condition"))
            placed = place(rendered.source, rendered.records)
            result = Relations(placed, standins, rendered.power, conditions)
    _CACHE[key] = result
    if len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)
    return result
