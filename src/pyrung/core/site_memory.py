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
  a drum's jump, a For loop's body rows).  They are recorded but not yet used
  for evaluation.  An edge condition object met at a second site is copied, so
  each edge object stands for exactly one site.

Overlays get their own namespace, so their keys never collide with the user
program's: plant rungs are ``plant:i{N}`` / ``plant:e{N}``, PILOT holds are
``pilot:i{N}`` / ``pilot:e{N}``.  An instruction that was never bound has no
key, and asking for one raises (there is no ``id()`` fallback).

Every site records a fingerprint ``(scope, source line, kind, operands)`` and
its contacts, so hot reload can carry memory to the matching site of the new
program, and drop it where no site matches unambiguously
(:func:`remap_site_memory`).

Keys are stamped on the objects, so rungs reused in several bare rung lists
share them; :func:`bind_logic_list` renames clashing holders so that keys stay
unique within one runner's logic.
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
# Rungs handed to ``PLC`` as a bare list that mixes bound and unbound rungs.
LOGIC_NAMESPACE = "logic"

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
    from pyrung.core.instruction.drums import EventDrumInstruction, _DrumBaseInstruction
    from pyrung.core.instruction.out_variants import ToggleInstruction

    slots: list[tuple[str, Any]] = []
    if getattr(instr, "_oneshot", False):
        slots.append(("_oneshot", False))
    if isinstance(instr, ToggleInstruction):
        slots.append(("_toggle", False))
    if isinstance(instr, ShiftInstruction):
        slots.append(("_shift_prev_clock", False))
    if isinstance(instr, _DrumBaseInstruction):
        slots.append(("_drum_jump_prev", False))
        slots.append(("_drum_jog_prev", False))
    if isinstance(instr, EventDrumInstruction):
        slots.append(("_drum_event_prev", False))
        slots.append(("_drum_event_ready", True))
        slots.append(("_drum_last_step", 0))
    return tuple(slots)


def _contacts_text(conditions: Iterable[Any]) -> tuple[str, ...]:
    from pyrung.core.validation.render import render_condition

    return tuple(render_condition(c) for c in conditions)


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


class _Binder:
    """One :func:`bind_sites` run."""

    def __init__(
        self,
        namespace: str,
        *,
        only_unbound: bool,
        program_owned: bool = False,
        keepers: Mapping[str, Any] | None = None,
        reserved: frozenset[str] = frozenset(),
    ) -> None:
        self.namespace = namespace
        self.only_unbound = only_unbound
        self.program_owned = program_owned
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
                    key = self._fresh_key("i", self.instruction_count)
                    instr._state_key = key
                    instr._site_program_owned = self.program_owned
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
                # A second site, or an edge another namespace (the user program)
                # already owns, gets an object of its own.
                if id(cond) in self.claimed or (
                    not self.only_unbound
                    and owner is not None
                    and _namespace_of(owner) != self.namespace
                ):
                    cond = copy.copy(cond)
                key = self._fresh_key("e", self.edge_count)
                cond._site_key = key
                cond._site_program_owned = self.program_owned
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
    _keepers: Mapping[str, Any] | None = None,
    _reserved: frozenset[str] = frozenset(),
) -> SiteTable:
    """Name every memory-keeping site of a program; stamp the keys on its objects.

    Instructions get ``_state_key`` (``i{N}``), edge conditions ``_site_key``
    (``e{N}``), both prefixed ``namespace:`` outside the user program.  Main
    rungs come first, then subroutines in definition order.  Rebinding the same
    rungs reproduces the same keys.  *program_owned* marks the keys as a
    ``Program``'s, which a bare rung list never renames (:func:`bind_logic_list`).
    """
    binder = _Binder(
        namespace,
        only_unbound=_keepers is not None,
        program_owned=program_owned,
        keepers=_keepers,
        reserved=_reserved,
    )
    scopes: list[tuple[str, Sequence[Rung]]] = [
        (namespace or "main", rungs),
        *((name, body) for name, body in (subroutines or {}).items()),
    ]
    for scope, scope_rungs in scopes:
        binder.number_instructions(scope_rungs, scope)
    for scope, scope_rungs in scopes:
        binder.bind_edges(scope_rungs, scope)
    instruction_sites = [s for s in binder.sites if s.site == SITE_INSTRUCTION]
    edge_sites = [s for s in binder.sites if s.site == SITE_EDGE]
    return SiteTable(namespace=namespace, sites=(*instruction_sites, *edge_sites))


def _list_key_holders(rungs: Sequence[Rung]) -> dict[str, list[Any]]:
    """Every key an object of *rungs* carries -> the distinct objects carrying it."""
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.rung import Rung as RungLogic

    holders: dict[str, list[Any]] = {}
    seen: set[int] = set()

    def _hold(obj: Any, key: str | None) -> None:
        if key is None or id(obj) in seen:
            return
        seen.add(id(obj))
        holders.setdefault(key, []).append(obj)

    def _conditions(cond: Any) -> None:
        for edge in _iter_edges(cond):
            _hold(edge, edge._site_key)

    def _instruction(instr: Any) -> None:
        _hold(instr, instr._state_key)
        for name in type(instr)._conditions:
            value = getattr(instr, name, None)
            for item in value if isinstance(value, tuple) else (value,):
                _conditions(item)
        if isinstance(instr, ForLoopInstruction):
            for item in instr.body:
                if isinstance(item, RungLogic):
                    _rung(item)
                else:
                    _instruction(item)

    def _rung(rung: Any) -> None:
        for cond in rung._conditions:
            _conditions(cond)
        for item in rung._execution_items:
            if isinstance(item, RungLogic):
                _rung(item)
            else:
                _instruction(item)

    for rung in rungs:
        _rung(rung)
    return holders


def bind_logic_list(rungs: Sequence[Rung]) -> SiteTable:
    """Bind rungs handed to ``PLC`` as a bare list.

    A list of fresh rungs binds like a program (``i{N}``).  A list that already
    holds bound objects (rungs taken from a program, or from another bare list)
    keeps one holder per key and names the rest, and every unbound object,
    under :data:`LOGIC_NAMESPACE` with keys no object of the list carries, so
    the keys are unique within this list.  The holder that keeps a key is the
    one a ``Program`` owns, else the first in the list.  Two objects that hold
    the same ``Program``-owned key cannot both keep it; that raises.
    """
    from pyrung.core.rung import Rung as RungLogic

    # A stand-in object scanned through ``evaluate()`` keeps no site memory.
    rungs = [rung for rung in rungs if isinstance(rung, RungLogic)]
    holders = _list_key_holders(rungs)
    if not holders:
        return bind_sites(rungs)
    keepers: dict[str, Any] = {}
    for key, objs in holders.items():
        owned = [obj for obj in objs if getattr(obj, "_site_program_owned", False)]
        if len(owned) > 1:
            raise ValueError(
                f"Site key {key!r} is held by {len(owned)} objects of different programs "
                "in one rung list; their memory would collide. Build the rungs into one "
                "Program instead of combining rungs of separately built programs."
            )
        keepers[key] = owned[0] if owned else objs[0]
    return bind_sites(
        rungs,
        namespace=LOGIC_NAMESPACE,
        _keepers=keepers,
        _reserved=frozenset(holders),
    )


def site_table_of(logic: Any) -> SiteTable | None:
    """The site table bound for *logic* (a ``Program`` or a ``PLC``), if any."""
    table = getattr(logic, "_site_table", None)
    return table if isinstance(table, SiteTable) else None


def _match_sites(old: Sequence[Site], new: Sequence[Site]) -> dict[str, str]:
    """Old site key -> new site key, for the sites reload may carry memory between.

    1. Exact fingerprint ``(scope, line, kind, operands)`` plus the site's
       contacts: groups of the same size pair in source order (one source line
       that builds several rungs, such as a helper called in a loop).
    2. The rest, with the line ignored: ``(scope, kind, operands, contacts)``
       pairs only when exactly one old and one new site have it, so an edit
       that shifts lines keeps the memory of the sites it did not touch.

    Anything else is unmatched; its memory is dropped, not guessed.
    """
    matched: dict[str, str] = {}

    def _groups(sites: Sequence[Site], key_fn: Any) -> dict[Any, list[Site]]:
        groups: dict[Any, list[Site]] = {}
        for site in sites:
            groups.setdefault(key_fn(site), []).append(site)
        return groups

    def _exact(site: Site) -> Any:
        return (site.site, site.fingerprint, site.contacts)

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
        return (site.site, scope, kind, operands, site.contacts)

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
    for the new program.  Other memory (edge history by tag, timer fractions,
    system memory) is kept as is.  Returns ``(memory, dropped keys)``.
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
