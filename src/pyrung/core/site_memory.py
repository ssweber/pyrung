"""Site identity: every place in a program that keeps memory across scans.

A CLICK keeps one memory cell per *place* in the program: a one-shot coil, a
drum's jump leg, an edge contact.  pyrung names those places here, once per
program, so every consumer (interpreter, compiled kernel, prover, hot reload)
uses the same names.

:func:`bind_sites` walks a program in solve order and returns a
:class:`SiteTable`:

- **Instruction sites** keep the ``i{N}`` keys ``Program`` has always assigned
  (walk order: each rung's instructions, then its branches; a For loop's
  flattened body follows the loop).  ``Instruction.memory_key(prefix)`` gives
  ``prefix:i{N}``.
- **Edge sites** (``rise()`` / ``fall()`` contacts) get ``e{N}`` in solve
  order: a rung's contacts, then its items in source order (a branch's local
  contacts and items, an instruction's helper legs such as a counter's reset or
  a drum's jump, a For loop's body rows).  Each keeps one memory cell,
  ``_edge:e{N}``: the contact's bit as that contact last saw it.  An edge
  condition object met at a second site is copied, so each edge object stands
  for exactly one site.

Overlays get their own namespace, so their keys never collide with the user
program's: plant rungs are ``plant:i{N}`` / ``plant:e{N}``, PILOT holds are
``pilot:i{N}`` / ``pilot:e{N}``.  An instruction that was never bound has no
key, and asking for one raises (there is no ``id()`` fallback).

Every site records a fingerprint ``(scope, source line, kind, operands)`` and
its contacts (an edge also its row as source text), so hot reload can carry
memory to the matching site of the new program, and drop it where no site
matches unambiguously (:func:`remap_site_memory`).  An edge site with no match
in the old program (added or edited) starts from its tag's current value
(:func:`seed_new_edge_memory`).

Keys are stamped on the objects.  A bound object is never renamed: a bare rung
list whose objects would clash (rungs of two programs, or of two lists) is
bound as a copy (:func:`bind_logic_list`), so a program, and every runner
already scanning its rungs, keeps its keys.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.instruction.base import Instruction
    from pyrung.core.rung import Rung

USER_NAMESPACE = ""
PLANT_NAMESPACE = "plant"
PILOT_NAMESPACE = "pilot"

EDGE_MEMORY = "_edge"
"""Memory prefix of an edge contact's site: ``_edge:e{N}`` holds the bit as the
contact last saw it (off at RUN)."""

SITE_INSTRUCTION = "instruction"
SITE_EDGE = "edge"

# Instruction fields that carry the rung's own power (the conjunction of the
# rung's contacts, captured when the instruction was built).  Their edges are
# the rung's contacts, not sites of their own.  Every other ``_conditions``
# field is a helper leg (reset, jump, jog, clock, events, a counter's down leg).
_POWER_FIELDS = frozenset(
    {"enable_condition", "_enable_condition", "up_condition", "data_condition", "auto_condition"}
)


def _is_power_field(instr: Any, name: str) -> bool:
    if name in _POWER_FIELDS:
        return True
    # A down counter's count input is its rung; an up counter's ``down_condition``
    # is the ``.down()`` helper leg.
    from pyrung.core.instruction.counters import CountDownInstruction

    return name == "down_condition" and isinstance(instr, CountDownInstruction)


@dataclass(frozen=True)
class Site:
    """One place in a program that keeps memory across scans."""

    key: str
    """``i{N}`` / ``e{N}``, prefixed ``namespace:`` outside the user program."""
    site: str
    """:data:`SITE_INSTRUCTION` or :data:`SITE_EDGE`."""
    kind: str
    """Instruction class name, or ``rise`` / ``fall`` for an edge contact."""
    tag: str | None
    """The edge's tag; an instruction's first operand, when it names one."""
    scope: str
    """``main``, the subroutine name, or the overlay namespace."""
    row: int
    """Index of the top-level rung within *scope*."""
    leg: str
    """Where the site sits in its rung: ``rung``, ``branch``, ``body``, or an
    instruction leg such as ``i7.reset_condition`` / ``i9.events[2]``."""
    source_line: int | None
    fingerprint: tuple[Any, ...]
    memory: tuple[tuple[str, Any], ...] = ()
    """``(memory key, default)`` for every memory cell the site owns."""
    contacts: tuple[str, ...] = ()
    """An instruction's contacts (rung, branch, body row) as source text; an
    edge's leg.  Reload uses it, with the fingerprint less its line, to match a
    site whose line moved."""
    context: tuple[str, ...] = ()
    """An edge's row as source text: the contacts and instructions of its
    top-level rung.  Reload matches on it too, so a rung inserted with the same
    contact never takes this one's memory."""


@dataclass(frozen=True)
class SiteTable:
    """Every memory-keeping site of one program, in solve order."""

    namespace: str
    sites: tuple[Site, ...]
    _by_key: dict[str, Site] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._by_key.update({s.key: s for s in self.sites})

    def site(self, key: str) -> Site:
        """The site named *key*; raises ``KeyError`` for an unknown key."""
        return self._by_key[key]

    @property
    def instruction_sites(self) -> tuple[Site, ...]:
        return tuple(s for s in self.sites if s.site == SITE_INSTRUCTION)

    @property
    def edge_sites(self) -> tuple[Site, ...]:
        return tuple(s for s in self.sites if s.site == SITE_EDGE)

    def sites_reading(self, tag_name: str) -> tuple[Site, ...]:
        """Edge sites whose contact reads *tag_name*."""
        return tuple(s for s in self.edge_sites if s.tag == tag_name)

    def memory_defaults(self) -> dict[str, Any]:
        """Every memory key the sites own, with the value an unwritten key reads as."""
        return {name: default for s in self.sites for name, default in s.memory}


def namespaced(namespace: str, local_key: str) -> str:
    return f"{namespace}:{local_key}" if namespace else local_key


def instruction_memory(instr: Instruction) -> tuple[tuple[str, Any], ...]:
    """``(prefix, default)`` for each memory cell *instr* keeps under its site key.

    The default is the value the interpreter and the compiled kernel read for a
    key that was never written (``get(key, default)``), so an absent key and a
    key holding its default are the same state.
    """
    from pyrung.core.instruction.advanced import ShiftInstruction
    from pyrung.core.instruction.counters import (
        COUNT_DOWN_MEMORY,
        COUNT_UP_MEMORY,
        CountDownInstruction,
        CountUpInstruction,
    )
    from pyrung.core.instruction.drums import EventDrumInstruction, _DrumBaseInstruction
    from pyrung.core.instruction.out_variants import FlasherInstruction, ToggleInstruction

    slots: list[tuple[str, Any]] = []
    if isinstance(instr, CountUpInstruction):
        slots.append((COUNT_UP_MEMORY, False))
        if instr.down_condition is not None:
            slots.append((COUNT_DOWN_MEMORY, False))
    if isinstance(instr, CountDownInstruction):
        slots.append((COUNT_DOWN_MEMORY, False))
    if getattr(instr, "_oneshot", False):
        slots.append(("_oneshot", False))
    if isinstance(instr, ToggleInstruction):
        slots.append(("_toggle", False))
    if isinstance(instr, FlasherInstruction):
        slots.append(("_flash_idle", True))
    if isinstance(instr, ShiftInstruction):
        slots.append(("_shift_prev_clock", False))
    if isinstance(instr, _DrumBaseInstruction):
        slots.append(("_drum_jump_prev", False))
        slots.append(("_drum_jog_prev", False))
        # Completed: every output off until the drum steps forward or resets.
        slots.append(("_drum_done", False))
    if isinstance(instr, EventDrumInstruction):
        slots.append(("_drum_event_prev", False))
        slots.append(("_drum_event_ready", True))
        slots.append(("_drum_last_step", 0))
    return tuple(slots)


def _contacts_text(conditions: Iterable[Any]) -> tuple[str, ...]:
    from pyrung.core.validation.render import render_condition

    def _text(cond: Any) -> str:
        try:
            return render_condition(cond)
        except Exception:  # noqa: BLE001 - a fingerprint must never fail
            return type(cond).__name__

    return tuple(_text(c) for c in conditions)


def _operand_text(value: Any) -> str:
    from pyrung.core.validation.render import operand_name

    if isinstance(value, (list, tuple)):
        return "(" + ", ".join(_operand_text(v) for v in value) + ")"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_operand_text(v)}" for k, v in value.items()) + "}"
    if callable(value) and hasattr(value, "__qualname__"):
        return str(value.__qualname__)
    try:
        return operand_name(value)
    except Exception:  # noqa: BLE001 - a fingerprint must never fail
        return type(value).__name__


def _instruction_operands(instr: Instruction) -> tuple[str, ...]:
    fields = tuple(dict.fromkeys(type(instr)._reads + type(instr)._writes))
    return tuple(_operand_text(getattr(instr, name, None)) for name in fields)


def _row_text(rung: Rung) -> tuple[str, ...]:
    """A top-level rung as source text: its contacts and instructions in order
    (branches and For bodies included)."""
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.rung import Rung as RungLogic

    out: list[str] = []

    def _instruction(instr: Any) -> None:
        out.append(f"{type(instr).__name__}({', '.join(_instruction_operands(instr))})")
        if isinstance(instr, ForLoopInstruction):
            for item in instr.body:
                if isinstance(item, RungLogic):
                    _rung(item)
                else:
                    _instruction(item)

    def _rung(current: Any) -> None:
        out.append(
            "["
            + ", ".join(_contacts_text(current._conditions[current._branch_condition_start :]))
            + "]"
        )
        for item in current._execution_items:
            if isinstance(item, RungLogic):
                _rung(item)
            else:
                _instruction(item)

    _rung(rung)
    return tuple(out)


def _first_tag_name(instr: Instruction) -> str | None:
    from pyrung.core.tag import Tag

    for name in type(instr)._writes + type(instr)._reads:
        value = getattr(instr, name, None)
        if isinstance(value, Tag):
            return value.name
    return None


def _edge_kind(cond: Any) -> str | None:
    from pyrung.core.condition import FallingEdgeCondition, RisingEdgeCondition

    if isinstance(cond, RisingEdgeCondition):
        return "rise"
    if isinstance(cond, FallingEdgeCondition):
        return "fall"
    return None


def _children(cond: Any) -> list[Any] | None:
    from pyrung.core.condition import AllCondition, AnyCondition

    if isinstance(cond, (AllCondition, AnyCondition)):
        return cond.conditions
    return None


def _iter_edges(cond: Any) -> Iterable[Any]:
    if cond is None:
        return
    if _edge_kind(cond) is not None:
        yield cond
        return
    children = _children(cond)
    if children is not None:
        for child in children:
            yield from _iter_edges(child)


# Owner of every binding no program owns (a bare rung list, an overlay).
_UNOWNED = object()


class _Binder:
    """One :func:`bind_sites` run."""

    def __init__(
        self,
        namespace: str,
        *,
        only_unbound: bool,
        program_owned: bool = False,
        owner: object | None = None,
        keepers: Mapping[str, Any] | None = None,
        reserved: frozenset[str] = frozenset(),
    ) -> None:
        self.namespace = namespace
        self.only_unbound = only_unbound
        self.program_owned = program_owned
        self.owner: object = owner if owner is not None else _UNOWNED
        # only_unbound: the one object allowed to keep each key it already
        # carries, and every key some object of the list carries (a fresh key
        # never takes one of those).
        self.keepers: Mapping[str, Any] = keepers or {}
        self.reserved = reserved
        self.fresh_counts = {"i": 0, "e": 0}
        self.sites: list[Site] = []
        self.instruction_count = 0
        self.edge_count = 0
        # Edge objects claimed by this run, by identity.
        self.claimed: set[int] = set()
        # Per top-level rung: replaced condition objects (old id -> new) and the
        # edge objects that are this rung tree's own contacts.
        self.subst: dict[int, Any] = {}
        self.rung_edges: set[int] = set()
        self.scope = "main"
        self.row = 0
        # The top-level rung being bound, and its source text once an edge asks.
        self.row_rung: Any = None
        self.row_text: tuple[str, ...] | None = None

    def _fresh_key(self, letter: str, position: int) -> str:
        if not self.only_unbound:
            return namespaced(self.namespace, f"{letter}{position}")
        while True:
            self.fresh_counts[letter] += 1
            key = namespaced(self.namespace, f"{letter}{self.fresh_counts[letter]}")
            if key not in self.reserved:
                return key

    def _keeps(self, obj: Any, key: str | None) -> bool:
        """only_unbound: *obj* keeps the key it carries (it is that key's keeper)."""
        return self.only_unbound and key is not None and self.keepers.get(key) is obj

    def foreign(self, obj: Any) -> bool:
        """Whether another binding stamped *obj*: its keys are that binding's, so
        this one must work on a copy, never re-key the object in place."""
        if self.only_unbound:
            return False
        owner = getattr(obj, "_site_owner", None)
        return owner is not None and owner is not self.owner

    def holds_foreign(self, rung: Any) -> bool:
        """Whether *rung*, a rung of its tree or an instruction in it is another
        binding's (a rung or instruction object shared with another program)."""
        from pyrung.core.instruction.control import ForLoopInstruction
        from pyrung.core.rung import Rung as RungLogic

        def _instruction(instr: Any) -> bool:
            if self.foreign(instr):
                return True
            if isinstance(instr, ForLoopInstruction):
                for item in instr.body:
                    if _rung(item) if isinstance(item, RungLogic) else _instruction(item):
                        return True
            return False

        def _rung(current: Any) -> bool:
            if self.foreign(current):
                return True
            for item in current._execution_items:
                if _rung(item) if isinstance(item, RungLogic) else _instruction(item):
                    return True
            return False

        return _rung(rung)

    def stamp_rungs(self, rungs: Sequence[Rung]) -> None:
        """Mark every rung of *rungs*' trees as this binding's (a list bound as
        it is, ``only_unbound``, takes nothing over)."""
        if self.only_unbound:
            return
        from pyrung.core.instruction.control import ForLoopInstruction
        from pyrung.core.rung import Rung as RungLogic

        def _rung(current: Any) -> None:
            current._site_owner = self.owner
            for item in current._execution_items:
                if isinstance(item, RungLogic):
                    _rung(item)
                elif isinstance(item, ForLoopInstruction):
                    for child in item.body:
                        if isinstance(child, RungLogic):
                            _rung(child)

        for rung in rungs:
            _rung(rung)

    # -- instruction keys ---------------------------------------------------

    def number_instructions(self, rungs: Sequence[Rung], scope: str) -> None:
        """Assign ``i{N}`` in the order ``Program`` always has (keys never renumber)."""
        from pyrung.core.instruction.control import ForLoopInstruction
        from pyrung.core.rung import Rung as RungLogic

        def _walk_instructions(
            instructions: Sequence[Instruction],
            row: int,
            contacts: tuple[str, ...],
            body_contacts: Mapping[int, tuple[str, ...]],
        ) -> None:
            for instr in instructions:
                self.instruction_count += 1
                key: str | None = instr._state_key
                if key is None or not self._keeps(instr, key):
                    # bind_sites copied every rung holding another binding's
                    # instruction, so this one is this binding's to re-key.
                    assert not self.foreign(instr)
                    key = self._fresh_key("i", self.instruction_count)
                    instr._state_key = key
                    instr._site_program_owned = self.program_owned
                    instr._site_owner = self.owner
                own = (*contacts, *body_contacts.get(id(instr), ()))
                self._record_instruction(instr, key, scope, row, own)
                if isinstance(instr, ForLoopInstruction):
                    local: dict[int, tuple[str, ...]] = {}
                    for item in instr.body:
                        if isinstance(item, RungLogic):
                            item_contacts = _contacts_text(
                                item._conditions[item._branch_condition_start :]
                            )
                            for body_instr in item._instructions:
                                local[id(body_instr)] = item_contacts
                    _walk_instructions(instr.instructions, row, own, local)

        def _walk_rung(rung: Rung, row: int, contacts: tuple[str, ...]) -> None:
            own = (*contacts, *_contacts_text(rung._conditions[rung._branch_condition_start :]))
            _walk_instructions(rung._instructions, row, own, {})
            for branch in rung._branches:
                _walk_rung(branch, row, own)

        for row, rung in enumerate(rungs):
            _walk_rung(rung, row, ())

    def _record_instruction(
        self, instr: Instruction, key: str, scope: str, row: int, contacts: tuple[str, ...]
    ) -> None:
        kind = type(instr).__name__
        line = getattr(instr, "source_line", None)
        self.sites.append(
            Site(
                key=key,
                site=SITE_INSTRUCTION,
                kind=kind,
                tag=_first_tag_name(instr),
                scope=scope,
                row=row,
                leg="instruction",
                source_line=line,
                fingerprint=(scope, line, kind, _instruction_operands(instr)),
                contacts=contacts,
                memory=tuple(
                    (f"{prefix}:{key}", default) for prefix, default in instruction_memory(instr)
                ),
            )
        )

    # -- edge sites ---------------------------------------------------------

    def _claim(self, cond: Any, leg: str, line_hint: int | None) -> Any:
        """Claim every edge in *cond* as a site at *leg*; return the condition to keep.

        A tree with no edge claimed before comes back unchanged.  Otherwise the
        path to each already-claimed edge is copied (tags are shared, not
        copied) and the copy is returned.
        """
        kind = _edge_kind(cond)
        if kind is not None:
            owner = getattr(cond, "_site_key", None)
            self.edge_count += 1
            if id(cond) not in self.claimed and self._keeps(cond, owner):
                # Bound by the program this rung came from: keep its key.
                assert owner is not None
                key = owner
            else:
                # A second site, or an edge another binding (another program,
                # a bare list a runner scans, another namespace) already keyed,
                # gets an object of its own: that binding keeps its key.
                if id(cond) in self.claimed or (
                    not self.only_unbound
                    and owner is not None
                    and (self.foreign(cond) or _namespace_of(owner) != self.namespace)
                ):
                    cond = copy.copy(cond)
                key = self._fresh_key("e", self.edge_count)
                cond._site_key = key
                cond._site_program_owned = self.program_owned
                cond._site_owner = self.owner
            self.claimed.add(id(cond))
            line = cond.source_line if cond.source_line is not None else line_hint
            tag = cond._resolved_tag.name
            self.sites.append(
                Site(
                    key=key,
                    site=SITE_EDGE,
                    kind=kind,
                    tag=tag,
                    scope=self.scope,
                    row=self.row,
                    leg=leg,
                    source_line=line,
                    fingerprint=(self.scope, line, kind, (tag,)),
                    contacts=(leg.split(".", 1)[-1],),
                    context=self._row_context(),
                    memory=((f"{EDGE_MEMORY}:{key}", False),),
                )
            )
            return cond
        children = _children(cond)
        if children is None:
            return cond
        new_children = [self._claim(child, leg, line_hint) for child in children]
        if all(new is old for new, old in zip(new_children, children, strict=True)):
            return cond
        clone = copy.copy(cond)
        clone.conditions = new_children
        return clone

    def _row_context(self) -> tuple[str, ...]:
        if self.row_text is None:
            self.row_text = () if self.row_rung is None else _row_text(self.row_rung)
        return self.row_text

    def _substituted(self, cond: Any) -> Any:
        """*cond* with this rung tree's replaced contacts swapped in (never mutates)."""
        if cond is None:
            return None
        replaced = self.subst.get(id(cond))
        if replaced is not None:
            return replaced
        children = _children(cond)
        if children is None:
            return cond
        new_children = [self._substituted(child) for child in children]
        if all(new is old for new, old in zip(new_children, children, strict=True)):
            return cond
        clone = copy.copy(cond)
        clone.conditions = new_children
        return clone

    def bind_edges(self, rungs: Sequence[Rung], scope: str) -> None:
        for row, rung in enumerate(rungs):
            self.scope = scope
            self.row = row
            self.subst = {}
            self.rung_edges = set()
            self.row_rung = rung
            self.row_text = None
            self._edges_in_rung(rung, leg="rung")

    def _edges_in_rung(self, rung: Rung, *, leg: str) -> None:
        from pyrung.core.rung import Rung as RungLogic

        conditions = rung._conditions
        start = rung._branch_condition_start
        changed = False
        for index in range(start):
            replaced = self.subst.get(id(conditions[index]))
            if replaced is not None:
                conditions[index] = replaced
                changed = True
        for index in range(start, len(conditions)):
            old = conditions[index]
            new = self._claim(old, leg, rung.source_line)
            if new is not old:
                conditions[index] = new
                self.subst[id(old)] = new
                changed = True
            self.rung_edges.update(id(e) for e in _iter_edges(new))
        if changed:
            rung._invalidate_analysis_caches()
        for item in rung._execution_items:
            if isinstance(item, RungLogic):
                self._edges_in_rung(item, leg="branch")
            else:
                self._edges_in_instruction(item)

    def _edges_in_instruction(self, instr: Instruction) -> None:
        from pyrung.core.instruction.control import ForLoopInstruction
        from pyrung.core.rung import Rung as RungLogic

        prefix = instr._state_key or type(instr).__name__
        line = getattr(instr, "source_line", None)
        swapped: dict[int, Any] = {}
        for name in type(instr)._conditions:
            value = getattr(instr, name, None)
            if value is None:
                continue
            if isinstance(value, tuple):
                items = list(value)
                for i, item in enumerate(items):
                    if item is not None:
                        items[i] = self._leg(item, f"{prefix}.{name}[{i}]", line, swapped)
                new_value: Any = tuple(items)
                if all(a is b for a, b in zip(items, value, strict=True)):
                    new_value = value
            elif _is_power_field(instr, name):
                new_value = self._power(value, f"{prefix}.{name}", line, swapped)
            else:
                new_value = self._leg(value, f"{prefix}.{name}", line, swapped)
            if new_value is not value:
                setattr(instr, name, new_value)
        if swapped and instr.debug_substeps:
            instr.debug_substeps = tuple(
                replace(step, condition=swapped.get(id(step.condition), step.condition))
                if step.condition is not None
                else step
                for step in instr.debug_substeps
            )
        if isinstance(instr, ForLoopInstruction):
            for item in instr.body:
                if isinstance(item, RungLogic):
                    self._edges_in_rung(item, leg="body")
                else:
                    self._edges_in_instruction(item)

    def _leg(self, value: Any, leg: str, line: int | None, swapped: dict[int, Any]) -> Any:
        new = self._claim(value, leg, line)
        if new is not value:
            swapped[id(value)] = new
        return new

    def _power(self, value: Any, leg: str, line: int | None, swapped: dict[int, Any]) -> Any:
        new = self._substituted(value)
        if new is not value:
            swapped[id(value)] = new
        # An edge in the power field that is not one of the rung's contacts
        # (an instruction built by hand) is a site of its own.
        if any(id(e) not in self.rung_edges for e in _iter_edges(new)):
            claimed = self._claim_foreign_power(new, leg, line)
            if claimed is not new:
                swapped[id(value)] = claimed
            new = claimed
        return new

    def _claim_foreign_power(self, cond: Any, leg: str, line: int | None) -> Any:
        if _edge_kind(cond) is not None:
            if id(cond) in self.rung_edges:
                return cond
            return self._claim(cond, leg, line)
        children = _children(cond)
        if children is None:
            return cond
        new_children = [self._claim_foreign_power(c, leg, line) for c in children]
        if all(new is old for new, old in zip(new_children, children, strict=True)):
            return cond
        clone = copy.copy(cond)
        clone.conditions = new_children
        return clone


def _namespace_of(key: str) -> str:
    namespace, sep, _local = key.rpartition(":")
    return namespace if sep else USER_NAMESPACE


def bind_sites(
    rungs: Sequence[Rung],
    subroutines: Mapping[str, Sequence[Rung]] | None = None,
    *,
    namespace: str = USER_NAMESPACE,
    program_owned: bool = False,
    owner: object | None = None,
    _keepers: Mapping[str, Any] | None = None,
    _reserved: frozenset[str] = frozenset(),
) -> SiteTable:
    """Name every memory-keeping site of a program; stamp the keys on its objects.

    Instructions get ``_state_key`` (``i{N}``), edge conditions ``_site_key``
    (``e{N}``), both prefixed ``namespace:`` outside the user program.  Main
    rungs come first, then subroutines in definition order.  Rebinding the same
    rungs reproduces the same keys.  *program_owned* marks the keys as a
    ``Program``'s; *owner* is the binding's owner (the ``Program``).

    Keys are never re-stamped on another owner's objects: an edge another
    binding keyed is copied before this one keys it, and a rung of *rungs* or
    *subroutines* whose tree holds another binding's rung or instruction is
    replaced (in that list) by a structural copy, so the other program, and a
    runner already scanning it, keep their keys.
    """
    binder = _Binder(
        namespace,
        only_unbound=_keepers is not None,
        program_owned=program_owned,
        owner=owner,
        keepers=_keepers,
        reserved=_reserved,
    )
    scopes: list[tuple[str, Sequence[Rung]]] = [
        (namespace or "main", rungs),
        *((name, body) for name, body in (subroutines or {}).items()),
    ]
    for _scope, scope_rungs in scopes:
        _own_copies(binder, scope_rungs)
    for scope, scope_rungs in scopes:
        binder.number_instructions(scope_rungs, scope)
    for scope, scope_rungs in scopes:
        binder.bind_edges(scope_rungs, scope)
    for _scope, scope_rungs in scopes:
        binder.stamp_rungs(scope_rungs)
        for rung in scope_rungs:
            # Keys may have changed: each row rebuilds its edge frame when scanned next.
            rung._edge_frame = None
    instruction_sites = [s for s in binder.sites if s.site == SITE_INSTRUCTION]
    edge_sites = [s for s in binder.sites if s.site == SITE_EDGE]
    return SiteTable(namespace=namespace, sites=(*instruction_sites, *edge_sites))


def _own_copies(binder: _Binder, rungs: Sequence[Rung]) -> None:
    """Replace each rung of *rungs* that holds another binding's rung or
    instruction with a structural copy of it (see :func:`bind_sites`)."""
    memo: dict[int, Any] = {}
    for index, rung in enumerate(rungs):
        if not binder.holds_foreign(rung):
            continue
        if not isinstance(rungs, list):
            raise TypeError(
                "these rungs hold another program's rungs or instructions; "
                "pass them as a list so they can be copied, or copy them first "
                "with pyrung.core.site_memory.copy_rungs()"
            )
        rungs[index] = _copy_rung(rung, memo)


def _list_site_holders(rungs: Sequence[Rung]) -> list[tuple[Any, str | None]]:
    """Every site object of *rungs* with the key it carries, once per place it sits.

    An object at two places appears twice (an edge object shared by two
    contacts, a rung listed twice).  A power field's edges that are the rung's
    own contacts are not places of their own; they are skipped.
    """
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.rung import Rung as RungLogic

    out: list[tuple[Any, str | None]] = []

    def _conditions(cond: Any) -> None:
        for edge in _iter_edges(cond):
            out.append((edge, edge._site_key))

    def _instruction(instr: Any, rung_edges: set[int]) -> None:
        out.append((instr, instr._state_key))
        for name in type(instr)._conditions:
            value = getattr(instr, name, None)
            for item in value if isinstance(value, tuple) else (value,):
                if _is_power_field(instr, name):
                    for edge in _iter_edges(item):
                        if id(edge) not in rung_edges:
                            out.append((edge, edge._site_key))
                else:
                    _conditions(item)
        if isinstance(instr, ForLoopInstruction):
            for item in instr.body:
                if isinstance(item, RungLogic):
                    _rung(item, set())
                else:
                    _instruction(item, set())

    def _rung(rung: Any, inherited: set[int]) -> None:
        own = set(inherited)
        for cond in rung._conditions[rung._branch_condition_start :]:
            _conditions(cond)
            own.update(id(e) for e in _iter_edges(cond))
        for item in rung._execution_items:
            if isinstance(item, RungLogic):
                _rung(item, own)
            else:
                _instruction(item, own)

    for rung in rungs:
        _rung(rung, set())
    return out


def _bound_as_is(rungs: Sequence[Rung]) -> bool | None:
    """Whether every site of *rungs* already carries a key no other place carries.

    ``None`` when no site is bound at all (a list of fresh rungs).
    """
    holders = _list_site_holders(rungs)
    if all(key is None for _obj, key in holders):
        return None
    seen_objects: set[int] = set()
    seen_keys: set[str] = set()
    for obj, key in holders:
        if key is None or id(obj) in seen_objects or key in seen_keys:
            return False
        seen_objects.add(id(obj))
        seen_keys.add(key)
    return True


def _copy_condition(cond: Any, memo: dict[int, Any]) -> Any:
    """*cond* with every edge (and every composite holding one) copied; the rest shared."""
    if cond is None:
        return None
    done = memo.get(id(cond))
    if done is not None:
        return done
    if _edge_kind(cond) is not None:
        new = copy.copy(cond)
        new._site_key = None
        new._site_owner = None
    else:
        children = _children(cond)
        if children is None:
            return cond
        new_children = [_copy_condition(child, memo) for child in children]
        if all(n is o for n, o in zip(new_children, children, strict=True)):
            return cond
        new = copy.copy(cond)
        new.conditions = new_children
    memo[id(cond)] = new
    return new


def _copy_item(item: Any, memo: dict[int, Any]) -> Any:
    from pyrung.core.rung import Rung as RungLogic

    return _copy_rung(item, memo) if isinstance(item, RungLogic) else _copy_instruction(item, memo)


def _copy_instruction(instr: Any, memo: dict[int, Any]) -> Any:
    from pyrung.core.instruction.control import ForLoopInstruction

    done = memo.get(id(instr))
    if done is not None:
        return done
    new = copy.copy(instr)
    new._state_key = None
    new._site_owner = None
    new._edge_frame = None
    memo[id(instr)] = new
    for name in type(instr)._conditions:
        value = getattr(instr, name, None)
        if isinstance(value, tuple):
            setattr(new, name, tuple(_copy_condition(item, memo) for item in value))
        elif value is not None:
            setattr(new, name, _copy_condition(value, memo))
    if instr.debug_substeps:
        new.debug_substeps = tuple(
            replace(step, condition=_copy_condition(step.condition, memo))
            if step.condition is not None
            else step
            for step in instr.debug_substeps
        )
    if isinstance(instr, ForLoopInstruction):
        new.body = tuple(_copy_item(item, memo) for item in instr.body)
        new.instructions = [_copy_instruction(item, memo) for item in instr.instructions]
    return new


def _copy_rung(rung: Any, memo: dict[int, Any]) -> Any:
    done = memo.get(id(rung))
    if done is not None:
        return done
    new = copy.copy(rung)
    new._site_owner = None
    memo[id(rung)] = new
    new._conditions = [_copy_condition(c, memo) for c in rung._conditions]
    new._instructions = [_copy_instruction(i, memo) for i in rung._instructions]
    new._branches = [_copy_rung(b, memo) for b in rung._branches]
    new._execution_items = [_copy_item(item, memo) for item in rung._execution_items]
    new._execution_plan = [(kind, _copy_item(item, memo)) for kind, item in rung._execution_plan]
    if rung._terminal_instruction is not None:
        new._terminal_instruction = _copy_instruction(rung._terminal_instruction, memo)
    new._invalidate_analysis_caches()
    return new


def copy_rungs(rungs: Sequence[Rung]) -> list[Rung]:
    """A structural copy of *rungs*, unbound: rungs, instructions and edge contacts
    are new objects; tags and every condition without an edge are shared."""
    memo: dict[int, Any] = {}
    return [_copy_rung(rung, memo) for rung in rungs]


def private_edges(cond: Any, namespace: str) -> tuple[Any, tuple[Any, ...]]:
    """*cond* with each edge contact copied and keyed ``{namespace}:e{N}``, and
    those copies.

    For a condition judged outside the scan (``run_until()``, ``when()``): its
    edges are no program's contacts, so they get keys of their own, and the
    caller keeps their memory.  *cond* and any program holding its edges are
    left as they were.
    """
    copied = _copy_condition(cond, {})
    edges: list[Any] = []
    seen: set[int] = set()
    for edge in _iter_edges(copied):
        if id(edge) in seen:
            continue
        seen.add(id(edge))
        edge._site_key = namespaced(namespace, f"e{len(edges) + 1}")
        edges.append(edge)
    return copied, tuple(edges)


def bind_logic_list(rungs: Sequence[Any]) -> tuple[list[Any], SiteTable]:
    """Bind rungs handed to ``PLC`` as a bare list; return the rungs to scan and
    their table.

    - A list of fresh rungs binds in place, like a program (``i{N}``/``e{N}``).
    - A list whose every site already carries a key no other site of the list
      carries (one program's rungs, a list bound before, or a slice of one)
      keeps those keys; nothing is renamed.
    - Anything else (rungs of two programs or of two lists, fresh rungs mixed
      with bound ones, one rung listed twice) is copied and the copy bound
      fresh.  The originals keep their keys, so a program, or another runner
      scanning the same rung objects, never sees its keys change.
    """
    from pyrung.core.rung import Rung as RungLogic

    # A stand-in object scanned through ``evaluate()`` keeps no site memory.
    scanned = rungs if isinstance(rungs, list) else list(rungs)
    real = [rung for rung in scanned if isinstance(rung, RungLogic)]
    state = _bound_as_is(real)
    if state is None:
        return scanned, bind_sites(real)
    if state:
        keepers = {key: obj for obj, key in _list_site_holders(real) if key is not None}
        return scanned, bind_sites(real, _keepers=keepers, _reserved=frozenset(keepers))
    memo: dict[int, Any] = {}
    copied = [_copy_rung(rung, memo) if isinstance(rung, RungLogic) else rung for rung in scanned]
    return copied, bind_sites([rung for rung in copied if isinstance(rung, RungLogic)])


def site_table_of(logic: Any) -> SiteTable | None:
    """The site table bound for *logic* (a ``Program`` or a ``PLC``), if any."""
    table = getattr(logic, "_site_table", None)
    return table if isinstance(table, SiteTable) else None


def _match_sites(old: Sequence[Site], new: Sequence[Site]) -> dict[str, str]:
    """Old site key -> new site key, for the sites reload may carry memory between.

    1. Exact fingerprint ``(scope, line, kind, operands)`` plus the site's
       contacts and (an edge) its row's text: groups of the same size pair in
       source order (one source line that builds several rungs, such as a
       helper called in a loop).
    2. The rest, with the line ignored: ``(scope, kind, operands, contacts,
       row text)`` pairs only when exactly one old and one new site have it, so
       an edit that shifts lines keeps the memory of the sites it did not touch.

    Anything else is unmatched; its memory is dropped, not guessed.
    """
    matched: dict[str, str] = {}

    def _groups(sites: Sequence[Site], key_fn: Any) -> dict[Any, list[Site]]:
        groups: dict[Any, list[Site]] = {}
        for site in sites:
            groups.setdefault(key_fn(site), []).append(site)
        return groups

    def _exact(site: Site) -> Any:
        return (site.site, site.fingerprint, site.contacts, site.context)

    old_groups = _groups(old, _exact)
    new_groups = _groups(new, _exact)
    for key, olds in old_groups.items():
        news = new_groups.get(key, [])
        if len(olds) == len(news):
            for o, n in zip(olds, news, strict=True):
                matched[o.key] = n.key
    used_new = set(matched.values())
    rest_old = [site for site in old if site.key not in matched]
    rest_new = [site for site in new if site.key not in used_new]

    def _moved(site: Site) -> Any:
        scope, _line, kind, operands = site.fingerprint
        return (site.site, scope, kind, operands, site.contacts, site.context)

    old_groups = _groups(rest_old, _moved)
    new_groups = _groups(rest_new, _moved)
    for key, olds in old_groups.items():
        news = new_groups.get(key, [])
        if len(olds) == 1 and len(news) == 1:
            matched[olds[0].key] = news[0].key
    return matched


_OVERLAY_NAMESPACES = frozenset({PLANT_NAMESPACE, PILOT_NAMESPACE})


def _is_overlay_memory(name: str) -> bool:
    """Memory of a plant/PILOT overlay site (``_oneshot:pilot:i3``)."""
    parts = name.split(":")
    return len(parts) >= 3 and parts[-2] in _OVERLAY_NAMESPACES


def remap_site_memory(
    memory: Mapping[str, Any],
    old: SiteTable | None,
    new: SiteTable | None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Carry site memory to the matching sites of a new program.

    Memory of a site of *old* moves to the site of *new* that
    :func:`_match_sites` pairs it with, or is dropped.  Memory of overlay
    sites (``plant:`` / ``pilot:``) is dropped too: the overlays are rebuilt
    for the new program.  Edge memory (``_edge:e{N}``) is site memory like any
    other.  Other memory (timer fractions, system memory) is kept as is.
    Returns ``(memory, dropped keys)``.
    """
    owned: dict[str, tuple[str, str]] = {}
    if old is not None:
        for site in old.sites:
            for name, _default in site.memory:
                owned[name] = (site.key, name[: -(len(site.key) + 1)])
    new_names: set[str] = set()
    if new is not None:
        new_names = {name for site in new.sites for name, _default in site.memory}
    matched = _match_sites(old.sites, new.sites) if old is not None and new is not None else {}
    out: dict[str, Any] = {}
    dropped: list[str] = []
    for name, value in memory.items():
        owner = owned.get(name)
        if owner is None:
            if _is_overlay_memory(name):
                dropped.append(name)
            else:
                out[name] = value
            continue
        site_key, prefix = owner
        target = matched.get(site_key)
        new_name = f"{prefix}:{target}" if target is not None else None
        if new_name is None or new_name not in new_names:
            dropped.append(name)
            continue
        out[new_name] = value
    return out, tuple(dropped)


def seed_new_edge_memory(
    memory: Mapping[str, Any],
    old: SiteTable | None,
    new: SiteTable | None,
    tags: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[Site, ...]]:
    """*memory* with every edge site of *new* that no site of *old* matches
    seeded from its tag's value in *tags*.

    Run after :func:`remap_site_memory` on reload: an edge contact the new
    program adds (or one edited so it no longer matches) then starts as if it
    had seen its tag last scan, so a held bit never rises (or falls) just
    because the program was reloaded.  A matched site keeps what the remap
    gave it, including no memory at all: a contact that has only seen its tag
    off has never written its memory, and must still see a rise.  A site whose
    tag *tags* does not hold is left to its default.  Returns ``(memory,
    seeded sites)``.
    """
    out = dict(memory)
    if new is None:
        return out, ()
    matched = (
        frozenset(_match_sites(old.sites, new.sites).values()) if old is not None else frozenset()
    )
    seeded: list[Site] = []
    for site in new.edge_sites:
        if site.key in matched or site.tag is None or site.tag not in tags:
            continue
        for name, _default in site.memory:
            out[name] = bool(tags[site.tag])
        seeded.append(site)
    return out, tuple(seeded)


LEGACY_EDGE_PREFIX = "_prev:"


def migrate_legacy_edge_memory(state: Any, table: SiteTable | None) -> Any:
    """*state* with pyrung's old per-tag edge history moved to edge sites.

    Before edge memory was kept per contact, ``_prev:X`` held X at the end of
    the previous scan, one entry per tag.  Each such entry seeds the memory of
    every edge site reading X (``_edge:e{N}``) that *state* does not already
    hold, and is dropped; with no site reading X it is just dropped.  A state
    without ``_prev:`` entries is returned as is.
    """
    memory = state.memory
    legacy = [
        name for name in memory if isinstance(name, str) and name.startswith(LEGACY_EDGE_PREFIX)
    ]
    if not legacy:
        return state
    updated = memory.evolver()
    for name in legacy:
        value = memory[name]
        del updated[name]
        if table is None:
            continue
        for site in table.sites_reading(name[len(LEGACY_EDGE_PREFIX) :]):
            for key, _default in site.memory:
                if key not in memory:
                    updated[key] = bool(value)
    return state.set(memory=updated.persistent())


def program_memory_defaults(program: Any) -> dict[str, Any]:
    """Every site-memory key of *program* with its unwritten default.

    Reads the program's :class:`SiteTable`; a program that was never bound
    (assembled by hand) is walked directly, keyed by the keys its instructions
    carry.
    """
    table = site_table_of(program)
    if table is not None:
        return table.memory_defaults()
    from pyrung.core.validation._common import walk_instructions

    out: dict[str, Any] = {}
    for instr in walk_instructions(program):
        key = instr._state_key
        if key is None:
            continue
        for prefix, default in instruction_memory(instr):
            out[f"{prefix}:{key}"] = default
    return out


# -- edge frames: which edge sites a row scans ------------------------------
#
# A row is a top-level rung (main or subroutine), a For loop's body rung, or a
# bare For-body instruction.  When a row is scanned, every edge site in it
# stores the bit its contact sees (``_edge:e{N}``), powered or not (a CLICK
# updates an edge contact's memory whenever the contact is scanned).  The
# row's condition view is taken first, so its edges compare the bit against the
# memory as it was before the row: ``evaluate()`` stays a pure read.


def edge_memory_key(cond: Any) -> str:
    """``_edge:e{N}`` of an edge condition; raises for an edge never bound."""
    key = cond._site_key
    if key is None:
        raise RuntimeError(
            f"{_edge_kind(cond)}({cond._resolved_tag.name}) has no site key: it was never "
            "bound into a program. Build it inside `with Program():`, or bind overlay "
            "rungs with pyrung.core.site_memory.bind_sites()."
        )
    return f"{EDGE_MEMORY}:{key}"


def _frame_contacts(rung: Any, out: set[int]) -> None:
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.rung import Rung as RungLogic

    for cond in rung._conditions:
        out.update(id(e) for e in _iter_edges(cond))
    for item in rung._execution_items:
        if isinstance(item, RungLogic):
            _frame_contacts(item, out)
        elif isinstance(item, ForLoopInstruction):
            for child in item.body:
                if isinstance(child, RungLogic):
                    _frame_contacts(child, out)


def _frame_add(edge: Any, out: dict[str, Any]) -> None:
    out.setdefault(edge_memory_key(edge), edge)


def _frame_names(frame: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple((key, edge._resolved_tag.name) for key, edge in frame.items())


_Rows = dict[int, tuple[tuple[str, Any], ...]]


def _frame_instruction(instr: Any, contacts: set[int], out: dict[str, Any], rows: _Rows) -> None:
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.rung import Rung as RungLogic

    for name in type(instr)._conditions:
        value = getattr(instr, name, None)
        power = _is_power_field(instr, name)
        for item in value if isinstance(value, tuple) else (value,):
            for edge in _iter_edges(item):
                # A power field's edges are its rung's contacts (scanned with
                # the row that owns them), unless built by hand.
                if not (power and id(edge) in contacts):
                    _frame_add(edge, out)
    if isinstance(instr, ForLoopInstruction):
        for child in instr.body:
            child_frame: dict[str, Any] = {}
            if isinstance(child, RungLogic):
                _frame_row(child, contacts, child_frame, rows)
            else:
                _frame_instruction(child, contacts, child_frame, rows)
                child._body_row_legs = _has_helper_leg(child)
            child._edge_frame = _frame_names(child_frame)
            rows[id(child)] = tuple(child_frame.items())


def _has_helper_leg(instr: Any) -> bool:
    for name in type(instr)._conditions:
        if _is_power_field(instr, name):
            continue
        value = getattr(instr, name, None)
        if value is None or (isinstance(value, tuple) and not value):
            continue
        return True
    return False


def _frame_row(rung: Any, contacts: set[int], out: dict[str, Any], rows: _Rows) -> None:
    from pyrung.core.rung import Rung as RungLogic

    conditions = rung._conditions
    for index in range(rung._branch_condition_start, len(conditions)):
        for edge in _iter_edges(conditions[index]):
            _frame_add(edge, out)
    for item in rung._execution_items:
        if isinstance(item, RungLogic):
            _frame_row(item, contacts, out, rows)
        else:
            _frame_instruction(item, contacts, out, rows)


def edge_rows(rung: Any) -> dict[int, tuple[tuple[str, Any], ...]]:
    """``(memory key, edge condition)`` of every row in top-level *rung*, by ``id``.

    The rows are *rung* itself and every For-body row inside it (a body rung or
    a bare body instruction), each with the edge sites it scans, in solve
    order.  Also stamps each row's ``_edge_frame`` (``(memory key, tag)``
    pairs) for the interpreter.
    """
    contacts: set[int] = set()
    _frame_contacts(rung, contacts)
    frame: dict[str, Any] = {}
    rows: _Rows = {}
    _frame_row(rung, contacts, frame, rows)
    rows[id(rung)] = tuple(frame.items())
    rung._edge_frame = _frame_names(frame)
    return rows


def build_edge_frames(rung: Any) -> tuple[tuple[str, str], ...]:
    """``(memory key, tag)`` of every edge site *rung*'s row scans, in solve order.

    Also stamps the frame of every For-body row inside it (``_edge_frame`` on
    each body rung and bare body instruction): a body row scans its sites on
    every iteration, not with the row that holds the loop.
    """
    edge_rows(rung)
    return rung._edge_frame


def scan_edge_frame(ctx: Any, view: Any, frame: Sequence[tuple[str, str]]) -> None:
    """Store, for each edge site of *frame*, the bit its contact sees in *view*.

    These are bookkeeping reads, kept out of the read journal: a contact's
    evaluation reads the same bit and memory from *view*, so the journal holds
    exactly the reads its conditions make, in their order (as when edge
    memory was stored at the end of the scan).
    """
    sink = getattr(view, "_read_sink", None)
    if sink is not None:
        view._read_sink = None
    try:
        for key, name in frame:
            current = bool(view.get_tag(name, False))
            if current != bool(view.get_memory(key, False)):
                ctx.set_memory(key, current)
    finally:
        if sink is not None:
            view._read_sink = sink


def begin_bare_body_row(ctx: Any, instr: Any) -> tuple[Any] | None:
    """Scan a bare For-body instruction as a row of its own, if it has helper legs.

    CLICK holds a bare body instruction as a wire-only rung between For and
    Next, so its helper legs (reset, jump, ...) read the tags as that rung
    starts, after the body rows above it, as a body rung's contacts do.  An
    edge leg then compares against the memory the previous iteration stored.
    Returns the condition snapshot to restore afterwards (wrapped), or ``None``
    when the instruction has no helper leg (nothing reads a view).
    """
    frame = getattr(instr, "_edge_frame", None)
    if not frame and not getattr(instr, "_body_row_legs", False):
        return None
    outer = ctx._condition_snapshot
    view = ctx._new_condition_view()
    ctx._condition_snapshot = view
    if frame:
        scan_edge_frame(ctx, view, frame)
    return (outer,)


def edge_memory_seeding(logic: Any, values: Mapping[str, Any]) -> dict[str, bool]:
    """Edge memory that makes every edge contact on a tag of *values* remember it.

    *logic* is a ``Program`` or a list of rungs.  For each tag name in
    *values*, every edge site reading the tag gets ``bool(value)``: a
    hypothetical scan then sees the tag move from that value at each of them,
    as if the previous scan had ended on it.
    """
    out: dict[str, bool] = {}
    table = site_table_of(logic)
    if table is not None:
        for name, value in values.items():
            for site in table.sites_reading(name):
                for key, _default in site.memory:
                    out[key] = bool(value)
        return out
    from pyrung.core.rung import Rung as RungLogic

    for rung in logic:
        if not isinstance(rung, RungLogic):
            continue
        for row in edge_rows(rung).values():
            for key, edge in row:
                name = edge._resolved_tag.name
                if name in values:
                    out[key] = bool(values[name])
    return out
