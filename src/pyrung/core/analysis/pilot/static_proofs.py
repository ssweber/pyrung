"""Prove ``PlanStatus.CANNOT_REACH`` statically for how().

``CANNOT_REACH`` claims that no scan sequence gets there.  So each proof here reasons
about every write to a tag, not about the one route a trace prefers.  When a proof
cannot enumerate something, it proves nothing.  The answer then falls back to
``STOPPED``, which claims nothing.

Two kinds of write are not seen yet, so a proof over them can be false.  One is a
text Copy from a tag that spills past its first slot, sized at run time.  The other
is a block write onto a plain tag that is named like the slot.

Three facts support both proofs:

* ``ProofFacts.producers`` lists every rung that can put a value in a tag.  It
  refuses when a write can happen outside the rung's condition (a coil going
  false, a timer), through a pointer, or in a for loop.  A pointer write counts
  against every slot of its block, whatever range the pointer declares, because a
  declared range only warns.
* A tag that the plan, the plant or the outside may set has no such list.  These
  tags are ``free``: steerable, forced or patched in the session how() was asked
  from, harness feedback, external, an input block's slot, and system tags.
* The state that how() starts from counts.  A value that is already there needs
  no producer.

**Blocked by a link** (``linked_feedback_block``).  A value is blocked when it
does not hold now and every producer's condition needs a blocked value.  Suppose
a value of the proof ever appears, and take the first scan in which any value of
the proof appears.  Its producer read another value of the proof that was already
there, which contradicts "first".  That leaves the place where the proof bottoms
out.  It is the Done bit of an on-delay timer that meets all of these conditions:

* The timer is non-retentive, on a main-level rung, with a literal preset, on a
  fixed dt.
* Only the timer writes its Acc and Done.
* The rung's own contacts hold a bool coupling's enable and feedback opposed
  (``En, ~Fb`` or ``~En, Fb``).

No rung or pointer writes that feedback and the session does not override it, so
only the plant moves it.  The plant ends the opposed state within its delay.  The
timer starts over whenever its rung goes false.  So the timer cannot time out when
its preset exceeds the delay plus some scans of slop.

The plant reads the enable between scans and a rung reads it mid-scan.  So the proof
holds only while those two readings agree to within a scan.  That requires the
enable to have no pointer write and not be ``free``.  It has either no writer, or
one writer that is an ``out`` coil on a main-level rung with no for loop.  Anything
wider gives ``STOPPED``.

**Mutually exclusive** (``exclusion_evidence``).  ``X=a`` and ``Y=b`` cannot both
hold at the end of a scan when two things are true.  No rung produces both.  Each
rung that produces one leaves the other off, either by writing it off or by reading
it off in its own condition.  Suppose both held.  Take the later of the two last
writes that put them in place.  Right after that write the other was off, and nothing
wrote it since.  This fails only when neither was ever written.  So the proof fails
when both already hold at the start.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.crossings import forward
from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.trace import _scope_ref
from pyrung.core.analysis.simplified import And, Atom, Const, Or, _conditions_list_to_expr
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.analysis.write_sites import instruction_write_targets, instruction_writes_tag
from pyrung.core.crossing import CrossingContext, Literal
from pyrung.core.instruction.coils import OutInstruction
from pyrung.core.instruction.control import ForLoopInstruction
from pyrung.core.instruction.timers import OnDelayInstruction
from pyrung.core.memory_block import Block, IndirectBlockRange, IndirectExprRef, IndirectRef
from pyrung.core.synthesis import coil_timer
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.core.tag import ImmediateRef, TagType, tag_written_from_outside
from pyrung.core.time_mode import TimeMode
from pyrung.core.validation._common import walk_instructions

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph, RungNode
    from pyrung.core.analysis.pilot.drive_setup import DriveSetup
    from pyrung.core.harness import Coupling

# How many scans a coupling's feedback may lag its delay, and a rung's timer the plant's.
_WINDOW_SLOP_SCANS = 3
# Past this many steps the walk gives up and returns no proof, which is always allowed.
_MAX_PROOF_STEPS = 2000
_NUMERIC_TYPES = frozenset({TagType.INT, TagType.DINT, TagType.WORD, TagType.REAL})


@dataclass(frozen=True)
class ProofFacts:
    """What the proofs read about one how(): the state it starts from and who else writes."""

    snapshot: dict[str, Any]
    pdg: ProgramGraph
    program: Any
    free: frozenset[str]
    overridden: frozenset[str]
    couplings: tuple[Coupling, ...]
    dt_ms: float | None
    pointer_written_blocks: frozenset[Block]

    def holds(self, tag: str, value: Any) -> bool:
        """Does *tag* have *value* now?  A tag no scan has written yet has its default."""
        if tag in self.snapshot:
            return _values_match(self.snapshot[tag], value)
        known = self.pdg.tags.get(tag)
        return known is None or _values_match(known.default, value)

    def rung_of(self, node_index: int) -> Any:
        return resolve_rung(self.program, self.pdg.rung_nodes[node_index])

    def _writable_through_pointer(self, tag: str) -> bool:
        return bool(self.pdg.tag_positions(tag, self.pointer_written_blocks))

    def _can_write(self, written: Any, tag: str, value: Any) -> bool:
        """Might this static write leave *tag* at *value*?  Only a literal can rule it out."""
        if not isinstance(written, Literal):
            return True
        literal = written.value
        known = self.pdg.tags.get(tag)
        if known is not None and known.type == TagType.BOOL:
            return bool(literal) == bool(value)
        if _values_match(literal, value):
            return True
        # A copy clamps what is out of range and truncates a real.  A literal int from 0 to
        # 32766 needs neither in any numeric type, so it writes exactly itself.
        numeric = known is not None and known.type in _NUMERIC_TYPES
        return not (numeric and type(literal) is int and 0 <= literal < 32767)

    def producers(self, tag: str, value: Any) -> tuple[int, ...] | None:
        """Return every node that can put *value* in *tag*, each only while its rung is true.

        Return ``None`` when that list cannot be made.  That happens for a free tag, a
        tag that nothing in the program writes (so something else does), a write through
        a pointer, and a write that fires with its rung false.
        """
        writers = self.pdg.writers_of.get(tag, frozenset())
        if not writers or tag in self.free or self._writable_through_pointer(tag):
            return None
        found: list[int] = []
        for node_index in sorted(writers):
            rung = self.rung_of(node_index)
            if rung is None or _has_a_loop(rung):
                return None
            writes = [instr for instr in rung._instructions if instruction_writes_tag(instr, tag)]
            if not writes:
                return None
            produces = False
            for instr in writes:
                if isinstance(instr, OutInstruction):
                    if not value:  # false with its rung false
                        return None
                elif not instr.is_inert_when_disabled():
                    return None
                written = forward(instr, tag, CrossingContext())
                produces = produces or self._can_write(written, tag, value)
            if produces:
                found.append(node_index)
        return tuple(found)


def _has_a_loop(rung: Any) -> bool:
    """Return whether *rung* has a for loop, which hides writes from ``instruction_writes_tag``."""
    return any(isinstance(instr, ForLoopInstruction) for instr in rung._instructions)


def _pointer_written_blocks(program: Any) -> frozenset[Block]:
    """Return every block that some instruction writes at an address only the scan knows."""
    blocks: set[Block] = set()
    for instr in walk_instructions(program):
        for target in instruction_write_targets(instr):
            if isinstance(target, ImmediateRef):
                target = target.value
            if isinstance(target, (IndirectRef, IndirectExprRef, IndirectBlockRange)):
                blocks.add(target.block)
    return frozenset(blocks)


def facts_for(setup: DriveSetup) -> ProofFacts:
    """Gather :class:`ProofFacts` from the fork a drive runs on, harness installed."""
    work = setup.work
    harness = getattr(work, "_harness", None)
    couplings = tuple(harness.couplings()) if harness is not None else ()
    overridden = setup.session_overrides
    free = (
        frozenset(setup.steerable)
        | overridden
        | {coupling.fb_name for coupling in couplings}
        | SYSTEM_TAGS_BY_NAME.keys()
        | {name for name, tag in setup.pdg.tags.items() if tag_written_from_outside(tag)}
    )
    return ProofFacts(
        snapshot=setup.diag_snapshot,
        pdg=setup.pdg,
        program=setup.program,
        free=free,
        overridden=overridden,
        couplings=couplings,
        dt_ms=work._dt * 1000 if work._time_mode is TimeMode.FIXED_STEP else None,
        pointer_written_blocks=_pointer_written_blocks(setup.program),
    )


# ---------------------------------------------------------------------------
# Blocked by a link
# ---------------------------------------------------------------------------


class _GaveUp(Exception):
    pass


def _conjuncts(expr: Any) -> list[Any]:
    if isinstance(expr, And):
        return [leaf for term in expr.terms for leaf in _conjuncts(term)]
    return [expr]


def _contact(atoms: list[Any], tag: str, form: str) -> bool:
    return any(isinstance(atom, Atom) and atom.tag == tag and atom.form == form for atom in atoms)


def _plant_delays_ms(physical: Any) -> tuple[int, int]:
    """Return the plant's debounce delay each way, rounded up to the unit both sides count in."""
    on_preset, off_preset, _unit, unit_ms = coil_timer(
        physical.on_delay_ms or 0, physical.off_delay_ms or 0
    )
    return on_preset * unit_ms, off_preset * unit_ms


class _LinkProof:
    """One walk back from a target.

    A result is the set of links used, or ``None`` when not proved.
    """

    def __init__(self, facts: ProofFacts) -> None:
        self.facts = facts
        self.steps = 0

    def blocked(self, tag: str, value: Any, stack: frozenset[Any]) -> frozenset[Any] | None:
        self.steps += 1
        if self.steps > _MAX_PROOF_STEPS:
            raise _GaveUp
        facts = self.facts
        if tag in facts.free or facts.holds(tag, value):
            return None
        key = (tag, repr(value))
        if key in stack:
            return frozenset()  # the first-appearance argument covers a cycle
        stack = stack | {key}
        timed = self._timer_done_blocked(tag, value, stack)
        if timed is not NotImplemented:
            return timed
        nodes = facts.producers(tag, value)
        if nodes is None:
            return None
        links: frozenset[Any] = frozenset()
        for node_index in nodes:
            used = self.never_true(self._condition_of(node_index), stack)
            if used is None:
                return None
            links |= used
        return links

    def _condition_of(self, node_index: int) -> Any:
        return _conditions_list_to_expr(self.facts.rung_of(node_index)._conditions)

    def never_true(self, expr: Any, stack: frozenset[Any]) -> frozenset[Any] | None:
        if isinstance(expr, Const):
            return None if expr.value else frozenset()
        if isinstance(expr, And):
            for term in expr.terms:
                used = self.never_true(term, stack)
                if used is not None:
                    return used
            return None
        if isinstance(expr, Or):
            links: frozenset[Any] = frozenset()
            for arm in expr.terms:
                used = self.never_true(arm, stack)
                if used is None:
                    return None
                links |= used
            return links
        if not isinstance(expr, Atom) or expr.unsupported is not None:
            return None
        if expr.form in ("xic", "rise"):
            return self.blocked(expr.tag, True, stack)
        if expr.form in ("xio", "fall"):
            return self.blocked(expr.tag, False, stack)
        if expr.form == "eq" and not expr.operand_is_tag:
            return self.blocked(expr.tag, expr.operand, stack)
        return None

    def _timer_done_blocked(self, tag: str, value: Any, stack: frozenset[Any]) -> Any:
        """Handle a non-retentive on-delay Done.  Return ``NotImplemented`` when *tag* is no Done."""
        pdg = self.facts.pdg
        writers = pdg.writers_of.get(tag, frozenset())
        found = [
            (node_index, instr)
            for node_index in writers
            for instr in getattr(self.facts.rung_of(node_index), "_instructions", ())
            if isinstance(instr, OnDelayInstruction) and instr.done_bit.name == tag
        ]
        if not found:
            return NotImplemented
        if (
            not value
            or len(found) != 1
            or len(writers) != 1
            or self.facts._writable_through_pointer(tag)
        ):
            return None
        node_index, timer = found[0]
        node = pdg.rung_nodes[node_index]
        acc = timer.accumulator.name
        if self._rung_also_writes(node_index, timer, (acc, tag)):
            return None
        # Skipped scans would hold the count, as a retentive timer does.
        if (
            node.subroutine is not None
            or node.guard_reads
            or timer.reset_condition is not None
            or not isinstance(timer.preset, int)
            or pdg.writers_of.get(acc, frozenset()) != writers
            or acc in self.facts.free
            or self.facts._writable_through_pointer(acc)
        ):
            return None
        per_ms = timer.unit.dt_to_units(0.001)
        counted = self.facts.snapshot.get(acc) or 0
        room_ms = (timer.preset - counted) / per_ms
        return self._dwell_limited(self._condition_of(node_index), room_ms, stack)

    def _rung_also_writes(self, node_index: int, timer: Any, tags: tuple[str, ...]) -> bool:
        """Does anything on the timer's rung other than the timer write these tags?"""
        rung = self.facts.rung_of(node_index)
        return _has_a_loop(rung) or any(
            instruction_writes_tag(instr, tag)
            for instr in rung._instructions
            if instr is not timer
            for tag in tags
        )

    def _dwell_limited(self, expr: Any, room_ms: float, stack: frozenset[Any]) -> Any:
        """Can *expr* never stay true for *room_ms* in one stretch?"""
        proved = self.never_true(expr, stack)
        if proved is not None:
            return proved
        atoms = _conjuncts(expr)
        for coupling in self.facts.couplings:
            delay_ms = self._opposed_delay(atoms, coupling)
            if delay_ms is None:
                continue
            assert self.facts.dt_ms is not None
            if delay_ms + _WINDOW_SLOP_SCANS * self.facts.dt_ms < room_ms:
                return frozenset({(coupling.en_name, coupling.fb_name)})
        return None

    def _opposed_delay(self, atoms: list[Any], coupling: Coupling) -> float | None:
        """Return how long the plant lets these contacts hold the feedback against its enable.

        Return ``None`` when there is no bound.  Only an on/off delay coupling has a bound.
        A flasher has no resting value.
        """
        facts = self.facts
        fb = coupling.fb_name
        trigger = coupling.trigger_value
        if (
            facts.dt_ms is None
            or trigger not in (None, 0)
            or coupling.physical.kind != "delay"
            or fb in facts.overridden
            or facts.pdg.writers_of.get(fb)
            or facts._writable_through_pointer(fb)
            or not self._read_as_the_plant_reads(coupling.en_name)
        ):
            return None
        on_form, off_form = ("xio", "xic") if trigger == 0 else ("xic", "xio")
        driving = _contact(atoms, coupling.en_name, on_form)
        resting = _contact(atoms, coupling.en_name, off_form)
        fb_high = _contact(atoms, fb, "xic")
        fb_low = _contact(atoms, fb, "xio")
        on_ms, off_ms = _plant_delays_ms(coupling.physical)
        if driving and not resting and fb_low and not fb_high:
            return on_ms
        if resting and not driving and fb_high and not fb_low:
            # The debounce's off side compares a scan after the fall.
            return off_ms + facts.dt_ms
        return None

    def _read_as_the_plant_reads(self, en: str) -> bool:
        """Does a rung read this enable as the plant does, between scans, at most a scan late?

        If nothing writes the enable, it moves only between scans.  One main-level ``out``
        coil gives it one position per scan, so a rung reads this scan's value or the last.
        Any other write can move it after one reader and back before the next.
        """
        facts = self.facts
        if facts._writable_through_pointer(en):
            return False
        writers = facts.pdg.writers_of.get(en, frozenset())
        if not writers:
            return True
        if en in facts.free or len(writers) != 1:
            return False
        node_index = next(iter(writers))
        if facts.pdg.rung_nodes[node_index].subroutine is not None:
            return False
        rung = facts.rung_of(node_index)
        if rung is None or _has_a_loop(rung):
            return False
        writes = [i for i in rung._instructions if instruction_writes_tag(i, en)]
        return len(writes) == 1 and isinstance(writes[0], OutInstruction)


def linked_feedback_block(target_tag: str, target_value: Any, facts: ProofFacts) -> str | None:
    """Return why *target_tag* cannot take *target_value* with the harness links intact.

    Return ``None`` when that is not proved.  The harness owns the feedback.  Every way to
    the value needs a watchdog-style dwell, and the link cuts that dwell short.  ``unlink=``
    models the dead sensor that would let the dwell finish.
    """
    if not facts.couplings:
        return None
    try:
        links = _LinkProof(facts).blocked(target_tag, target_value, frozenset())
    except _GaveUp:
        return None
    if not links:
        return None
    ordered = sorted(links, key=lambda link: link[1])
    named = ", ".join(f"{fb}<-{en}" for en, fb in ordered)
    names = ", ".join(repr(fb) for _en, fb in ordered)
    return (
        f"pilot: {target_tag}={target_value!r} is blocked by physical link(s) {named}; "
        "every way there needs a timer to finish while the feedback disagrees with its "
        "driver, and the harness ends that before the timer can. "
        f"Retry with unlink=[{names}] to model a dead sensor (fault injection)."
    )


# ---------------------------------------------------------------------------
# Mutually exclusive
# ---------------------------------------------------------------------------


def _leaves_off(atom: Atom, value: Any) -> bool:
    """Does this contact being true mean its tag does not have *value*?"""
    if atom.form in ("xic", "rise"):
        return not value
    if atom.form in ("xio", "fall"):
        return bool(value)
    if atom.operand_is_tag or atom.unsupported is not None or atom.operand is None:
        return False
    operand = atom.operand
    try:
        verdict = {
            "eq": lambda: value == operand,
            "ne": lambda: value != operand,
            "lt": lambda: value < operand,
            "le": lambda: value <= operand,
            "gt": lambda: value > operand,
            "ge": lambda: value >= operand,
        }[atom.form]()
    except (KeyError, TypeError):
        return False
    return not verdict


def _reads_off(expr: Any, tag: str, value: Any) -> bool:
    if isinstance(expr, And):
        return any(_reads_off(term, tag, value) for term in expr.terms)
    if isinstance(expr, Or):
        return all(_reads_off(term, tag, value) for term in expr.terms)
    return isinstance(expr, Atom) and expr.tag == tag and _leaves_off(expr, value)


def _rung_chain(facts: ProofFacts, node: RungNode) -> list[Any]:
    """Return the rung and the branches down to *node*.  All of them are true when it fires."""
    rung = resolve_rung(facts.program, replace(node, branch_path=()))
    if rung is None:
        return []
    chain = [rung]
    for branch_index in node.branch_path:
        rung = rung._branches[branch_index]
        chain.append(rung)
    return chain


def _rung_key(node: RungNode) -> tuple[str | None, int]:
    return (node.subroutine, node.rung_index)


def _leaves_other_off(facts: ProofFacts, node_index: int, tag: str, value: Any) -> str | None:
    """Return how firing this node leaves *tag* off *value*, in words, or ``None``."""
    node = facts.pdg.rung_nodes[node_index]
    where = _scope_ref(node_index, node)
    # A return may exit the rung between its write of one tag and its write of the other.
    may_return = any(
        other.guard_reads for other in facts.pdg.rung_nodes if _rung_key(other) == _rung_key(node)
    )
    for link in _rung_chain(facts, node):
        if _reads_off(_conditions_list_to_expr(link._conditions), tag, value):
            return f"{where} reads {tag} off {value!r}"
        if may_return:
            continue
        for instr in link._instructions:
            if not instruction_writes_tag(instr, tag) or not instr.is_inert_when_disabled():
                continue
            written = forward(instr, tag, CrossingContext())
            if (
                not getattr(instr, "oneshot", False)
                and isinstance(written, Literal)
                and not facts._can_write(written, tag, value)
            ):
                return f"{where} sets {tag}={written.value!r}"
    return None


def _each_leaves_other_off(
    facts: ProofFacts, nodes: tuple[int, ...], tag: str, value: Any
) -> list[str] | None:
    words: list[str] = []
    for node_index in nodes:
        node = facts.pdg.rung_nodes[node_index]
        # A call runs other rungs between this rung's own instructions.
        if any(
            other.calls for other in facts.pdg.rung_nodes if _rung_key(other) == _rung_key(node)
        ):
            return None
        said = _leaves_other_off(facts, node_index, tag, value)
        if said is None:
            return None
        words.append(said)
    return words


def exclusion_evidence(
    facts: ProofFacts, a: tuple[str, Any], b: tuple[str, Any]
) -> tuple[list[str], list[str]] | None:
    """Return what keeps ``a`` and ``b`` from holding together, each way round, or ``None``."""
    if facts.holds(*a) and facts.holds(*b):
        return None
    makes_a = facts.producers(*a)
    makes_b = facts.producers(*b)
    if not makes_a or not makes_b:
        return None
    pdg = facts.pdg
    rungs_a = {_rung_key(pdg.rung_nodes[node_index]) for node_index in makes_a}
    if any(_rung_key(pdg.rung_nodes[node_index]) in rungs_a for node_index in makes_b):
        return None
    a_side = _each_leaves_other_off(facts, makes_a, *b)
    if a_side is None:
        return None
    b_side = _each_leaves_other_off(facts, makes_b, *a)
    if b_side is None:
        return None
    return a_side, b_side
