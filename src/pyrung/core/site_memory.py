"""Site identity: every place in a program that keeps memory across scans.

A CLICK keeps one memory cell for each *place* in the program: a one-shot coil, a
drum's jump leg, an edge contact.  This module names those places once per
program. The interpreter, the compiled kernel, the static facts and hot reload all
use the same names.

:func:`bind_sites` walks a program in solve order and returns a
:class:`SiteTable`:

- **Instruction sites** keep the ``i{N}`` keys that ``Program`` has always assigned.
  The walk visits each rung's instructions, then its branches. A For loop's
  flattened body follows the loop.  ``Instruction.memory_key(prefix)`` returns
  ``prefix:i{N}``.
- **Edge sites** (``rise()`` / ``fall()`` contacts) get ``e{N}`` in solve
  order. That order is a rung's contacts, then its items in source order: a branch's
  local contacts and items, an instruction's helper legs (such as a counter's reset
  or a drum's jump), and a For loop's body rows.  Each edge site has one memory
  cell, ``_edge:e{N}``, which holds the bit the contact saw last.  If an edge
  condition object appears at a second site, the walk copies it, so each edge
  object belongs to exactly one site.

Overlays get their own namespace, so their keys never collide with the user
program's. Plant rungs use ``plant:i{N}`` and ``plant:e{N}``. PILOT holds use
``pilot:i{N}`` and ``pilot:e{N}``.  The harness keys each of its coils by the
feedback it drives (``plant:fb.{name}``).  An instruction that was never bound has no
key. Asking for one raises, and there is no ``id()`` fallback.

Every site records a fingerprint ``(scope, source line, kind, operands)`` and
its contacts. An edge site also records its row as source text. Hot reload uses
these to carry memory to the matching site of the new program
(:func:`remap_site_memory`), and drops the memory where no site matches
unambiguously.  An instruction's inputs as it last saw them (its rung and its
helper legs) also follow an edit of what the instruction reads.
An edge site with no match in the old program, because it was added or edited,
starts from its tag's current value (:func:`seed_new_edge_memory`).

The keys are stamped on the objects, and a bound object is never renamed. If a bare
rung list holds objects that would clash (rungs of two programs, or of two lists),
:func:`bind_logic_list` binds a copy. A program, and every runner already scanning
its rungs, keeps its keys.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from pyrung.core.time_mode import TimeUnit

if TYPE_CHECKING:
    from pyrung.core.instruction.base import Instruction
    from pyrung.core.rung import Rung

USER_NAMESPACE = ""
PLANT_NAMESPACE = "plant"
PILOT_NAMESPACE = "pilot"
PLANT_FEEDBACK_SITE = f"{PLANT_NAMESPACE}:fb."
"""Site key of a harness coil, before the name of the feedback it drives."""


EDGE_MEMORY = "_edge"
"""Memory prefix of an edge contact's site: ``_edge:e{N}`` holds the bit as the
contact last saw it (off at RUN)."""

SITE_INSTRUCTION = "instruction"
SITE_EDGE = "edge"

# These instruction fields hold the rung's own power: the conjunction of the rung's
# contacts, captured when the instruction was built.  Their edges are the rung's
# contacts, not sites of their own.  Every other ``_conditions`` field is a helper
# leg (reset, jump, jog, clock, events, a counter's down leg).
_POWER_FIELDS = frozenset(
    {"enable_condition", "_enable_condition", "up_condition", "data_condition", "auto_condition"}
)


def _is_power_field(instr: Any, name: str) -> bool:
    if name in _POWER_FIELDS:
        return True
    # A down counter's count input is its rung. On an up counter, ``down_condition``
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
    carries: tuple[tuple[str, int], ...] = ()
    """``(_frac key, nanoseconds to a unit)`` for each accumulator a timed instruction
    ticks.  A carry is no site's own memory (it follows its accumulator's name), but reload
    has only this table to learn the unit of the program it is moving to."""
    targets: tuple[str, ...] = ()
    """An instruction's written operands (its ``_writes``) as source text."""
    instruction: Any = field(default=None, compare=False, repr=False)
    """The instruction an instruction site was bound for.  Reload reads its helper
    legs from it, only for a site it pairs with an edited one."""


@dataclass(frozen=True)
class SiteTable:
    """Every memory-keeping site of one program, in solve order."""

    namespace: str
    sites: tuple[Site, ...]
    _by_key: dict[str, Site] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._by_key.update({s.key: s for s in self.sites})

    def site(self, key: str) -> Site:
        """Return the site named *key*, or raise ``KeyError`` if there is none."""
        return self._by_key[key]

    @property
    def instruction_sites(self) -> tuple[Site, ...]:
        return tuple(s for s in self.sites if s.site == SITE_INSTRUCTION)

    @property
    def edge_sites(self) -> tuple[Site, ...]:
        return tuple(s for s in self.sites if s.site == SITE_EDGE)

    def sites_reading(self, tag_name: str) -> tuple[Site, ...]:
        """Return the edge sites whose contact reads *tag_name*."""
        return tuple(s for s in self.edge_sites if s.tag == tag_name)

    def memory_defaults(self) -> dict[str, Any]:
        """Return every memory key the sites own, with the value an unwritten key reads as."""
        return {name: default for s in self.sites for name, default in s.memory}


def namespaced(namespace: str, local_key: str) -> str:
    return f"{namespace}:{local_key}" if namespace else local_key


def instruction_memory(instr: Instruction) -> tuple[tuple[str, Any], ...]:
    """Return ``(prefix, default)`` for each memory cell *instr* keeps under its site key.

    The default is the value that the interpreter and the compiled kernel read for a
    key that was never written (``get(key, default)``). A missing key and a key that
    holds its default are therefore the same state.
    """
    from pyrung.core.instruction.advanced import ShiftInstruction
    from pyrung.core.instruction.counters import (
        COUNT_DOWN_MEMORY,
        COUNT_UP_MEMORY,
        CountDownInstruction,
        CountUpInstruction,
    )
    from pyrung.core.instruction.drums import EventDrumInstruction, _DrumBaseInstruction
    from pyrung.core.instruction.out_variants import (
        DebounceInstruction,
        FlasherInstruction,
        TimedInstruction,
        ToggleInstruction,
    )

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
    if isinstance(instr, DebounceInstruction):
        slots.append(("_debounce_rung", False))
    if isinstance(instr, FlasherInstruction):
        slots.append(("_flash_phase", 0))
    if isinstance(instr, TimedInstruction):
        slots.append(("_timed_rung", False))
    if isinstance(instr, ShiftInstruction):
        slots.append(("_shift_prev_clock", False))
    if isinstance(instr, _DrumBaseInstruction):
        slots.append(("_drum_jump_prev", False))
        slots.append(("_drum_jog_prev", False))
        # The rung as the last scan left it. A false-to-true change writes the outputs.
        slots.append(("_drum_rung_prev", False))
    if isinstance(instr, EventDrumInstruction):
        slots.append(("_drum_event_prev", False))
        slots.append(("_drum_last_step", 0))
    return tuple(slots)


def _timer_carries(instr: Instruction) -> tuple[tuple[str, int], ...]:
    """Return ``(_frac key, nanoseconds to a unit)`` for each accumulator *instr* ticks."""
    unit = getattr(instr, "unit", None)
    if not isinstance(unit, TimeUnit):
        return ()
    accumulators = (
        getattr(instr, f, None) for f in ("accumulator", "on_acc", "off_acc", "timer_acc")
    )
    return tuple((f"_frac:{acc.name}", unit.ns) for acc in accumulators if acc is not None)


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


def _operand_texts(instr: Instruction) -> dict[str, str]:
    """Map each operand field of *instr*, reads then writes, to its source text."""
    fields = dict.fromkeys(type(instr)._reads + type(instr)._writes)
    return {name: _operand_text(getattr(instr, name, None)) for name in fields}


def _instruction_operands(instr: Instruction) -> tuple[str, ...]:
    return tuple(_operand_texts(instr).values())


def _leg_text(instr: Instruction, name: str) -> str:
    """Return helper leg *name* of *instr*, a condition or a tuple of them, as text."""
    value = getattr(instr, name, None)
    items = value if isinstance(value, tuple) else (value,)
    return ", ".join("None" if item is None else _contacts_text((item,))[0] for item in items)


def _row_text(rung: Rung) -> tuple[str, ...]:
    """Return a top-level rung as source text: its contacts and instructions in order.

    Branches and For bodies are included.
    """
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


# The owner of every binding that no program owns, such as a bare rung list or an overlay.
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
        # Used only when only_unbound is set. ``keepers`` maps each key to the one object
        # allowed to keep it. ``reserved`` holds every key that some object of the list
        # carries, so a fresh key never takes one of those.
        self.keepers: Mapping[str, Any] = keepers or {}
        self.reserved = reserved
        self.fresh_counts = {"i": 0, "e": 0}
        self.sites: list[Site] = []
        self.instruction_count = 0
        self.edge_count = 0
        # The edge objects that this run has claimed, by identity.
        self.claimed: set[int] = set()
        # For the current top-level rung, ``subst`` maps the id of each replaced condition
        # object to its replacement. ``rung_edges`` holds the edge objects that are
        # this rung tree's own contacts.
        self.subst: dict[int, Any] = {}
        self.rung_edges: set[int] = set()
        self.scope = "main"
        self.row = 0
        # The top-level rung being bound. Its source text is built when an edge first asks.
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
        """Return whether, under only_unbound, *obj* is the keeper of the key it carries."""
        return self.only_unbound and key is not None and self.keepers.get(key) is obj

    def foreign(self, obj: Any) -> bool:
        """Return whether another binding stamped *obj*.

        The keys on *obj* belong to that binding. This binding must work on a copy and
        never re-key the object in place.
        """
        if self.only_unbound:
            return False
        owner = getattr(obj, "_site_owner", None)
        return owner is not None and owner is not self.owner

    def holds_foreign(self, rung: Any) -> bool:
        """Return whether *rung*, a rung of its tree or an instruction in it is another binding's.

        That happens when a rung or instruction object is shared with another program.
        """
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
        """Mark every rung in the trees of *rungs* as this binding's.

        A list bound as it is (``only_unbound``) takes nothing over.
        """
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
        """Assign ``i{N}`` keys in the order that ``Program`` has always used.

        Keys never renumber.
        """
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
                    # bind_sites copied every rung that held another binding's
                    # instruction, so this binding can re-key this one.
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
        texts = _operand_texts(instr)
        memory = instruction_memory(instr)
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
                fingerprint=(scope, line, kind, tuple(texts.values())),
                contacts=contacts,
                memory=tuple((f"{prefix}:{key}", default) for prefix, default in memory),
                carries=_timer_carries(instr),
                targets=tuple(texts[name] for name in dict.fromkeys(type(instr)._writes)),
                instruction=instr,
            )
        )

    # -- edge sites ---------------------------------------------------------

    def _claim(self, cond: Any, leg: str, line_hint: int | None) -> Any:
        """Claim every edge in *cond* as a site at *leg*; return the condition to keep.

        If no edge in the tree was claimed before, return the tree unchanged.
        Otherwise copy the path to each edge that was already claimed, and return the
        copy.  The copy shares the tags.
        """
        kind = _edge_kind(cond)
        if kind is not None:
            owner = getattr(cond, "_site_key", None)
            self.edge_count += 1
            if id(cond) not in self.claimed and self._keeps(cond, owner):
                # The program that this rung came from bound it, so it keeps its key.
                assert owner is not None
                key = owner
            else:
                # A second site gets an object of its own. So does an edge that another
                # binding already keyed (another program, a bare list that a runner scans,
                # another namespace). That binding keeps its key.
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
        """Return *cond* with this rung tree's replaced contacts swapped in.

        It does not change *cond*.
        """
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
        # An edge in the power field that is not one of the rung's contacts is a site
        # of its own. This happens when an instruction was built by hand.
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
    """Name every memory-keeping site of a program and stamp the keys on its objects.

    Each instruction gets a ``_state_key`` (``i{N}``) and each edge condition gets a
    ``_site_key`` (``e{N}``). Outside the user program, both have a ``namespace:``
    prefix.  Main rungs come first, then subroutines in definition order.  Binding the
    same rungs again produces the same keys.  *program_owned* marks the keys as a
    ``Program``'s. *owner* is the owner of this binding, which is the ``Program``.

    This function never re-stamps keys on another owner's objects. If an edge was keyed
    by another binding, it copies the edge before it keys it. If the tree of a rung in
    *rungs* or *subroutines* holds another binding's rung or instruction, it replaces
    that rung in the list with a structural copy. The other program, and any runner
    already scanning it, keep their keys.
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
            # The keys may have changed, so each row rebuilds its edge frame on its next scan.
            rung._edge_frame = None
    instruction_sites = [s for s in binder.sites if s.site == SITE_INSTRUCTION]
    edge_sites = [s for s in binder.sites if s.site == SITE_EDGE]
    return SiteTable(namespace=namespace, sites=(*instruction_sites, *edge_sites))


def _own_copies(binder: _Binder, rungs: Sequence[Rung]) -> None:
    """Replace each rung of *rungs* that holds another binding's rung or instruction.

    The replacement is a structural copy of the rung. See :func:`bind_sites`.
    """
    memo: dict[int, Any] = {}
    for index, rung in enumerate(rungs):
        if not binder.holds_foreign(rung):
            continue
        if not isinstance(rungs, list):
            raise TypeError(
                "The rungs you passed hold rungs or instructions that another program "
                "already uses. Pass the rungs as a list so that pyrung can copy them, or "
                "copy them first with pyrung.core.site_memory.copy_rungs()."
            )
        rungs[index] = _copy_rung(rung, memo)


def _list_site_holders(rungs: Sequence[Rung]) -> list[tuple[Any, str | None]]:
    """Return every site object of *rungs* with the key it carries, once per place it sits.

    An object at two places appears twice, such as an edge object that two contacts
    share or a rung that is listed twice.  The edges in a power field that are the
    rung's own contacts are not places of their own, so this function skips them.
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
    """Return whether every site of *rungs* already carries a key that no other place carries.

    Return ``None`` when no site is bound at all, which is the case for a list of fresh
    rungs.
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
    """Return *cond* with every edge, and every composite that holds one, copied.

    The other conditions are shared with *cond*.
    """
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
    if rung._pin_box is not None:
        new._pin_box = _copy_instruction(rung._pin_box, memo)
    new._invalidate_analysis_caches()
    return new


def copy_rungs(rungs: Sequence[Rung]) -> list[Rung]:
    """Return an unbound structural copy of *rungs*.

    The rungs, instructions and edge contacts are new objects. The tags, and every
    condition that has no edge, are shared with the original.
    """
    memo: dict[int, Any] = {}
    return [_copy_rung(rung, memo) for rung in rungs]


def private_edges(cond: Any, namespace: str) -> tuple[Any, tuple[Any, ...]]:
    """Return *cond* with each edge contact copied and keyed ``{namespace}:e{N}``, and the copies.

    Use this for a condition that is judged outside the scan, such as in ``run_until()``
    or ``when()``.  Its edges are not any program's contacts, so they get keys of their
    own, and the caller keeps their memory.  This function does not change *cond* or any
    program that holds its edges.
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
    """Bind rungs that were handed to ``PLC`` as a bare list.

    Return the rungs to scan and their table.  There are three cases.

    - A list of fresh rungs binds in place, like a program (``i{N}``/``e{N}``).
    - Suppose every site in the list already carries a key that no other site of the
      list carries. This is true of one program's rungs, a list that was bound before,
      and a slice of one.  The list keeps those keys, and nothing is renamed.
    - Any other list is copied, and the copy is bound fresh.  This covers rungs of two
      programs or of two lists, fresh rungs mixed with bound ones, and one rung listed
      twice.  The originals keep their keys. A program, or another runner that scans
      the same rung objects, never sees its keys change.
    """
    from pyrung.core.rung import Rung as RungLogic

    # A stand-in object that is scanned through ``evaluate()`` keeps no site memory.
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
    """Return the site table bound for *logic*, a ``Program`` or a ``PLC``, or ``None``."""
    table = getattr(logic, "_site_table", None)
    return table if isinstance(table, SiteTable) else None


def _match_sites(old: Sequence[Site], new: Sequence[Site]) -> dict[str, str]:
    """Map old site keys to new site keys, for the sites that reload may carry memory between.

    Matching runs in two passes.

    1. Match the exact fingerprint ``(scope, line, kind, operands)`` plus the site's
       contacts and, for an edge, its row's text.  Groups of the same size pair up in
       source order. One source line can build several rungs, for example a helper
       that is called in a loop.
    2. Match the remaining sites with the line ignored, on ``(scope, kind, operands,
       contacts, row text)``.  A pair matches only when exactly one old site and one
       new site have that value. An edit that shifts lines therefore keeps the memory
       of the sites it did not touch.

    Any other site is unmatched. Its memory is dropped and never guessed.
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


_RUNG_INPUT_MEMORY = frozenset(
    {
        "_oneshot",
        "_toggle",
        "_debounce_rung",
        "_timed_rung",
        "_flash_phase",  # idle (0) while the rung is false
        "_drum_rung_prev",
        "_cu",  # COUNT_UP_MEMORY
    }
)
"""Memory prefixes that hold an instruction's rung as it last saw it, and a flasher's
phase, which is idle whenever its rung was last seen false, and while a preset of 0 or
below holds it reset.  On reload a flasher
whose preset was edited keeps its phase."""

_LEG_INPUT_MEMORY: Mapping[str, str] = {
    "_cd": "down_condition",  # COUNT_DOWN_MEMORY: an up counter's down leg
    "_shift_prev_clock": "clock_condition",
    "_drum_jump_prev": "jump_condition",
    "_drum_jog_prev": "jog_condition",
    # The step's event as last seen, and the step it was seen on.
    "_drum_event_prev": "events",
    "_drum_last_step": "events",
}
"""Memory prefix -> the helper leg whose input, as last seen, it holds."""

_INPUT_MEMORY = _RUNG_INPUT_MEMORY | frozenset(_LEG_INPUT_MEMORY)
"""Memory prefixes that hold an instruction's inputs (its rung, a helper leg) as it
last saw them.  An edit to what the instruction reads (a preset, a source, a jump
step) does not change those inputs, so on reload this memory follows the edited
instruction (:func:`_match_input_memory`)."""

_RUNG = ""
"""The :func:`_match_input_memory` pairing of rung memory."""


def _input_leg(site: Site, prefix: str) -> str:
    """Return the helper leg that *prefix* holds on *site*, or :data:`_RUNG` for its rung."""
    leg = _LEG_INPUT_MEMORY.get(prefix)
    if leg is None or (site.instruction is not None and _is_power_field(site.instruction, leg)):
        # A down counter's count input is its rung.
        return _RUNG
    return leg


def _match_input_memory(
    old: Sequence[Site], new: Sequence[Site], matched: Mapping[str, str]
) -> dict[str, dict[str, str]]:
    """Pair the input memory of sites that :func:`_match_sites` left unmatched.

    The input memory is :data:`_INPUT_MEMORY`.  The result maps a leg (:data:`_RUNG` for
    the rung) to a map from old site key to new site key.

    Rung memory depends only on the instruction's contacts. It follows a site whose read
    operands changed (a preset, a source) when exactly one old site and one new site
    share its scope, kind, written operands and contacts.  A leg's memory is also paired
    on that leg's text, so the text must be unchanged.  Without this pairing, a held
    input would read as a rising edge after reload. A timed coil or a one-shot would
    fire again, a counter would count once more, and a flasher would restart.  A changed
    target or contact drops all of the memory. A changed leg drops that leg's memory.
    """
    used = set(matched.values())
    unmatched_old = [site for site in old if site.key not in matched]
    unmatched_new = [site for site in new if site.key not in used]

    def _legs(site: Site) -> set[str]:
        legs = set()
        for name, _default in site.memory:
            prefix = name[: -(len(site.key) + 1)]
            if prefix in _INPUT_MEMORY:
                legs.add(_input_leg(site, prefix))
        return legs

    def _key(site: Site, leg: str) -> Any:
        scope, _line, kind, _operands = site.fingerprint
        text = (
            None if leg == _RUNG or site.instruction is None else _leg_text(site.instruction, leg)
        )
        return (site.site, scope, kind, site.targets, site.contacts, leg, text)

    def _groups(sites: Sequence[Site]) -> dict[Any, list[Site]]:
        groups: dict[Any, list[Site]] = {}
        for site in sites:
            for leg in _legs(site):
                if leg != _RUNG and site.instruction is None:
                    continue  # no leg to compare
                groups.setdefault(_key(site, leg), []).append(site)
        return groups

    olds = _groups(unmatched_old)
    news = _groups(unmatched_new)
    out: dict[str, dict[str, str]] = {}
    for key, old_sites in olds.items():
        new_sites = news.get(key, [])
        if len(old_sites) == 1 and len(new_sites) == 1:
            out.setdefault(key[5], {})[old_sites[0].key] = new_sites[0].key
    return out


_OVERLAY_NAMESPACES = frozenset({PLANT_NAMESPACE, PILOT_NAMESPACE})


def _is_overlay_memory(name: str) -> bool:
    """Return whether *name* is the memory of a plant or PILOT overlay site.

    An example is ``_oneshot:pilot:i3``.  A harness coil's memory, such as
    ``_toggle:plant:fb.Lamp``, is not overlay memory.  Its key names the feedback that it
    drives, so the coil rebuilt for the new program finds it.  If reload dropped it, a
    toggle or timed coil with its enable on would see a rise on the first scan.
    """
    if f":{PLANT_FEEDBACK_SITE}" in name:
        return False
    parts = name.split(":")
    return len(parts) >= 3 and parts[-2] in _OVERLAY_NAMESPACES


@dataclass(frozen=True)
class SiteMemoryRemap:
    """What :func:`remap_site_memory_report` did with a state's memory."""

    memory: dict[str, Any]
    """The memory to start the new program from."""
    dropped: tuple[str, ...]
    """Keys reset because no site of the new program matches theirs unambiguously
    (an overlay's memory included)."""


def remap_site_memory(
    memory: Mapping[str, Any],
    old: SiteTable | None,
    new: SiteTable | None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Carry site memory to the matching sites of a new program.

    Return ``(memory, dropped keys)``.  See :func:`remap_site_memory_report`.
    """
    report = remap_site_memory_report(memory, old, new)
    return report.memory, report.dropped


def remap_site_memory_report(
    memory: Mapping[str, Any],
    old: SiteTable | None,
    new: SiteTable | None,
) -> SiteMemoryRemap:
    """Carry site memory to the matching sites of a new program.

    The memory of a site in *old* moves to the site in *new* that :func:`_match_sites`
    pairs it with. If there is no such site, the memory is dropped.  The memory of overlay
    sites (``plant:`` and ``pilot:``) is dropped too, because the overlays are rebuilt for
    the new program.  A harness coil's memory (``plant:fb.{name}``) is kept.
    Edge memory (``_edge:e{N}``) is site memory like any other.  A site that is left
    unmatched still passes its input memory to its edited counterpart
    (:func:`_match_input_memory`).  Other memory, such as timer carries and system memory,
    is kept as it is, with one exception.  A carry is cut to below one unit of its timer
    in *new*, for two reasons. By definition, a carry is less than one unit. And an edit
    to the timer's unit must not finish the timer.
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
    input_matched = (
        _match_input_memory(old.sites, new.sites, matched)
        if old is not None and new is not None
        else {}
    )
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
        if target is None and old is not None and new is not None and prefix in _INPUT_MEMORY:
            leg = _input_leg(old.site(site_key), prefix)
            target = input_matched.get(leg, {}).get(site_key)
        new_name = f"{prefix}:{target}" if target is not None else None
        if new_name is None or new_name not in new_names:
            dropped.append(name)
            continue
        out[new_name] = value
    if new is not None:
        for site in new.sites:
            for name, unit_ns in site.carries:
                if name in out:
                    out[name] %= unit_ns
    return SiteMemoryRemap(out, tuple(dropped))


def seed_new_edge_memory(
    memory: Mapping[str, Any],
    old: SiteTable | None,
    new: SiteTable | None,
    tags: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[Site, ...]]:
    """Return *memory* with every unmatched edge site of *new* seeded from its tag's value.

    An unmatched site is one that no site of *old* matches.  The value comes from *tags*.
    Return ``(memory, seeded sites)``.

    Call this after :func:`remap_site_memory` on reload.  An edge contact that the new
    program adds, or that was edited so it no longer matches, then starts as if it had
    seen its tag on the last scan. A held bit therefore never rises or falls just because
    the program was reloaded.  A matched site keeps what the remap gave it, even when that
    is no memory at all. A contact that has only seen its tag off has never written its
    memory, and it must still see a rise.  A site whose tag is not in *tags* keeps its
    default.
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
    """Return *state* with pyrung's old per-tag edge history moved to edge sites.

    Before pyrung kept edge memory for each contact, it kept one ``_prev:X`` entry for
    each tag, which held X at the end of the previous scan.  This function seeds each
    such entry into the memory of every edge site that reads X (``_edge:e{N}``) and that
    *state* does not already hold. Then it drops the entry.  If no site reads X, it just
    drops the entry.  A state without ``_prev:`` entries is returned as it is.
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
    """Return every site-memory key of *program* with its unwritten default.

    This function reads the program's :class:`SiteTable`.  If the program was never bound
    because it was assembled by hand, the function walks it directly and uses the keys
    that its instructions carry.
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
# A row is one of these:
# - a trunk, which is the contacts of a top-level rung in main or a subroutine, without
#   the contacts of its branches (a continued rung is a trunk of its own);
# - a branch, which is its local contacts;
# - a box, which is an instruction's helper legs (reset, jump, ...);
# - a For loop's body rung;
# - a bare For-body instruction, which is a box.
# Each row has an edge frame of its own. The frame holds the edge sites that the row's
# own contacts hold, in solve order.  When the solve reaches a row, every edge site in
# it stores the bit that its contact sees (``_edge:e{N}``), whether the contact is
# powered or not.  pyrung does this because a CLICK updates an edge contact's memory
# whenever it scans the contact.  The row's condition view is taken first, so its edges
# compare the bit against the memory as it was before the row. That keeps ``evaluate()``
# a pure read. A row below sees the stores of the rows above it, as it sees their writes.


def edge_memory_key(cond: Any) -> str:
    """Return the ``_edge:e{N}`` key of an edge condition, or raise if it was never bound."""
    key = cond._site_key
    if key is None:
        raise RuntimeError(
            f"{_edge_kind(cond)}({cond._resolved_tag.name}) is not part of a program, so it "
            "has no edge memory. Build it inside `with Program():`, or bind overlay rungs "
            "with pyrung.core.site_memory.bind_sites()."
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


def _stamp_row(row: Any, frame: dict[str, Any], rows: _Rows) -> None:
    row._edge_frame = _frame_names(frame)
    rows[id(row)] = tuple(frame.items())


def _frame_instruction(instr: Any, contacts: set[int], rows: _Rows) -> None:
    """Frame box *instr* (its helper legs) and, for a For loop, each body row."""
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.rung import Rung as RungLogic

    frame: dict[str, Any] = {}
    for name in type(instr)._conditions:
        value = getattr(instr, name, None)
        power = _is_power_field(instr, name)
        for item in value if isinstance(value, tuple) else (value,):
            for edge in _iter_edges(item):
                # The edges in a power field are usually its rung's contacts, and the row
                # that owns those contacts scans them. An instruction built by hand can hold
                # other edges in a power field. The box's own frame holds those.
                if not (power and id(edge) in contacts):
                    _frame_add(edge, frame)
    _stamp_row(instr, frame, rows)
    if isinstance(instr, ForLoopInstruction):
        for child in instr.body:
            if isinstance(child, RungLogic):
                _frame_rung(child, contacts, rows)
            else:
                _frame_instruction(child, contacts, rows)


def _frame_row(rung: Any, rows: _Rows) -> None:
    """Frame the contacts that *rung* adds as a row, but not the rows inside it.

    For a branch, these are its local contacts.
    """
    conditions = rung._conditions
    frame: dict[str, Any] = {}
    for index in range(rung._branch_condition_start, len(conditions)):
        for edge in _iter_edges(conditions[index]):
            _frame_add(edge, frame)
    _stamp_row(rung, frame, rows)


def _frame_rung(rung: Any, contacts: set[int], rows: _Rows) -> None:
    """Frame *rung* and each branch and box inside it as a row of its own."""
    from pyrung.core.rung import Rung as RungLogic

    _frame_row(rung, rows)
    for item in rung._execution_items:
        if isinstance(item, RungLogic):
            _frame_rung(item, contacts, rows)
        else:
            _frame_instruction(item, contacts, rows)


def edge_rows(rung: Any) -> _Rows:
    """Return ``(memory key, edge condition)`` for every row in top-level *rung*, by ``id``.

    The rows are *rung*'s trunk, each of its nested branches and boxes, and every
    For-body row inside it. A For-body row is a body rung with its branches and boxes,
    or a bare body instruction.  Each row holds the edge sites of its own contacts,
    in solve order.  This function also stamps each row's ``_edge_frame``, which is a
    tuple of ``(memory key, tag)`` pairs.  The interpreter scans a row's frame when the
    solve reaches the row.
    """
    contacts: set[int] = set()
    _frame_contacts(rung, contacts)
    rows: _Rows = {}
    _frame_rung(rung, contacts, rows)
    return rows


def build_edge_frames(rung: Any) -> tuple[tuple[str, str], ...]:
    """Return ``(memory key, tag)`` for the edge sites in *rung*'s own row, its trunk.

    This function also stamps the frame (``_edge_frame``) of every row inside the rung:
    each branch, box and For-body row.  The solve scans each frame when it reaches the
    row.
    """
    edge_rows(rung)
    return rung._edge_frame


def build_box_frame(instr: Any) -> tuple[tuple[str, str], ...]:
    """Return ``(memory key, tag)`` for the edge sites in box *instr*'s helper legs.

    Use this for a box that runs outside a framed rung, such as in an instruction unit
    test.
    """
    _frame_instruction(instr, set(), {})
    return instr._edge_frame


def scan_edge_frame(ctx: Any, view: Any, frame: Sequence[tuple[str, str]]) -> None:
    """Store, for each edge site of *frame*, the bit that its contact sees in *view*.

    These are bookkeeping reads, and they stay out of the read journal.  A contact's
    evaluation reads the same bit and memory from *view*. The journal therefore holds
    exactly the reads that the conditions make, in their order, as it did when edge
    memory was stored at the end of the scan.
    """
    sink = getattr(view, "_read_sink", None)
    if sink is not None:
        view._read_sink = None
    hold = view._hold_memory if getattr(view, "live", False) else None
    try:
        for key, name in frame:
            current = bool(view.get_tag(name, False))
            if current != bool(view.get_memory(key, False)):
                if hold is not None:
                    # A live view sees the store. Hold the memory as it was before the
                    # row, so the row's own edge contacts read the old value.
                    hold(key)
                ctx.set_memory(key, current)
    finally:
        if sink is not None:
            view._read_sink = sink


def edge_memory_seeding(logic: Any, values: Mapping[str, Any]) -> dict[str, bool]:
    """Return edge memory that makes every edge contact on a tag in *values* remember it.

    *logic* is a ``Program`` or a list of rungs.  For each tag name in *values*, every
    edge site that reads the tag gets ``bool(value)``.  A hypothetical scan then sees the
    tag move from that value at each of those sites, as if the previous scan had ended
    on it.
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
