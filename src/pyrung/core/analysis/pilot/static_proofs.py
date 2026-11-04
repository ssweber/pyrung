"""The static proofs behind ``PlanStatus.CANNOT_REACH``.

``CANNOT_REACH`` claims no scan sequence gets there, so each proof here reasons
about every write to a tag rather than the one route a trace prefers. Whatever
it cannot enumerate it does not prove: the answer falls back to ``STOPPED``,
which claims nothing.

Three facts carry both proofs:

* ``ProofFacts.producers`` lists every rung able to put a value in a tag, and
  refuses when a write can happen outside the rung's condition (a coil going
  false, a timer) or through a pointer.
* A tag the plan or the plant may set (``free``: steerable, forced, harness
  feedback) has no such list.
* The state how() starts from counts: a value already there needs no producer.

**Blocked by a link** (``linked_feedback_block``). A value is blocked when it
does not hold now and every producer's condition needs a blocked value. Take the
first scan any value in the proof appears: its producer read another value of
the proof, already there, which contradicts first. That leaves where the proof
bottoms out, a non-retentive on-delay's Done, whose rung needs a bool coupling's
enable and its feedback opposed (``En, ~Fb`` or ``~En, Fb``). The plant ends that
state within its delay, and the timer starts over whenever its rung goes false,
so it cannot time out when its preset exceeds the delay plus some scans of slop.
An instantaneous read of the same contacts proves nothing: the state exists for
those scans. A ``Pulse`` feedback alternates, so it has no such bound.

**Mutually exclusive** (``exclusion_evidence``). ``X=a`` and ``Y=b`` cannot both
hold at the end of a scan when no rung produces both and each rung producing one
leaves the other off, by writing it off or by reading it off in its own
condition. Suppose both held. Take the later of the two last writes that put them
in place: right after it the other was off, and nothing wrote it since. Only when
neither was ever written does that fail, so both holding at the start is no proof.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.crossings import forward
from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.trace import _scope_ref
from pyrung.core.analysis.simplified import And, Atom, Const, Or, _conditions_list_to_expr
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.analysis.write_sites import instruction_writes_tag
from pyrung.core.crossing import CrossingContext, Literal
from pyrung.core.instruction.coils import OutInstruction
from pyrung.core.instruction.timers import OnDelayInstruction
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.core.tag import TagType
from pyrung.core.time_mode import TimeMode

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph, RungNode
    from pyrung.core.analysis.pilot.drive_setup import DriveSetup
    from pyrung.core.harness import Coupling

# Scans a coupling's feedback may lag its delay, and a rung's timer the plant's.
_WINDOW_SLOP_SCANS = 3
# Past this the walk gives up: no proof, which is always allowed.
_MAX_PROOF_STEPS = 2000
_NUMERIC_TYPES = frozenset({TagType.INT, TagType.DINT, TagType.WORD, TagType.REAL})


@dataclass(frozen=True)
class ProofFacts:
    """What the proofs read about one how(): the state it starts from and who else writes."""

    snapshot: dict[str, Any]
    pdg: ProgramGraph
    program: Any
    free: frozenset[str]
    forced: frozenset[str]
    couplings: tuple[Coupling, ...]
    dt_ms: float | None

    def holds(self, tag: str, value: Any) -> bool:
        """Does *tag* have *value* now?  A tag no scan has written yet has its default."""
        if tag in self.snapshot:
            return _values_match(self.snapshot[tag], value)
        known = self.pdg.tags.get(tag)
        return known is None or _values_match(known.default, value)

    def rung_of(self, node_index: int) -> Any:
        return resolve_rung(self.program, self.pdg.rung_nodes[node_index])

    def _writable_through_pointer(self, tag: str) -> bool:
        block = getattr(self.pdg.tags.get(tag), "_pyrung_block", None)
        return block is not None and any(ref.block is block for ref in self.pdg.indirect_writes)

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
        # A copy clamps what is out of range and truncates a real: no numeric type does either here.
        numeric = known is not None and known.type in _NUMERIC_TYPES
        return not (numeric and type(literal) is int and 0 <= literal < 32767)

    def producers(self, tag: str, value: Any) -> tuple[int, ...] | None:
        """Every node able to put *value* in *tag*, each only while its rung is true.

        ``None`` when that list cannot be made: a free tag, a tag nothing in the program
        writes (so something else does), a write through a pointer, or a write that
        fires with its rung false.
        """
        writers = self.pdg.writers_of.get(tag, frozenset())
        if not writers or tag in self.free or self._writable_through_pointer(tag):
            return None
        found: list[int] = []
        for node_index in sorted(writers):
            rung = self.rung_of(node_index)
            if rung is None:
                return None
            writes = [
                instr for instr in rung._instructions if instruction_writes_tag(instr, tag)
            ]
            if not writes:  # through a pointer or inside another instruction
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


def facts_for(setup: DriveSetup) -> ProofFacts:
    """Gather :class:`ProofFacts` from the fork a drive runs on, harness installed."""
    work = setup.work
    harness = getattr(work, "_harness", None)
    couplings = tuple(harness.couplings()) if harness is not None else ()
    forced = frozenset(work._forces)
    free = (
        frozenset(setup.steerable)
        | forced
        | {coupling.fb_name for coupling in couplings}
        | SYSTEM_TAGS_BY_NAME.keys()
    )
    return ProofFacts(
        snapshot=setup.diag_snapshot,
        pdg=setup.pdg,
        program=setup.program,
        free=free,
        forced=forced,
        couplings=couplings,
        dt_ms=work._dt * 1000 if work._time_mode is TimeMode.FIXED_STEP else None,
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


def _requires(atoms: list[Any], tag: str, form: str, operand: Any = None) -> bool:
    return any(
        isinstance(atom, Atom)
        and atom.tag == tag
        and atom.form == form
        and (operand is None or (not atom.operand_is_tag and _values_match(atom.operand, operand)))
        for atom in atoms
    )


def _enable_required(atoms: list[Any], coupling: Coupling) -> bool | None:
    """Do these contacts need the coupling driving (True), not driving (False), or neither?"""
    en, trigger = coupling.en_name, coupling.trigger_value
    on_form, off_form = ("xio", "xic") if trigger == 0 else ("xic", "xio")
    if trigger not in (None, 0):
        on = _requires(atoms, en, "eq", trigger)
        off = _requires(atoms, en, "ne", trigger)
    else:
        on, off = _requires(atoms, en, on_form), _requires(atoms, en, off_form)
    return True if on and not off else False if off and not on else None


class _LinkProof:
    """One walk back from a target.  A result is the links used, ``None`` when not proved."""

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
            return self._every_arm(expr, lambda arm: self.never_true(arm, stack))
        if not isinstance(expr, Atom) or expr.unsupported is not None:
            return None
        if expr.form in ("xic", "rise"):
            return self.blocked(expr.tag, True, stack)
        if expr.form in ("xio", "fall"):
            return self.blocked(expr.tag, False, stack)
        if expr.form == "eq" and not expr.operand_is_tag:
            return self.blocked(expr.tag, expr.operand, stack)
        return None

    def _every_arm(self, expr: Or, check: Any) -> frozenset[Any] | None:
        links: frozenset[Any] = frozenset()
        for arm in expr.terms:
            used = check(arm)
            if used is None:
                return None
            links |= used
        return links

    def _timer_done_blocked(self, tag: str, value: Any, stack: frozenset[Any]) -> Any:
        """A non-retentive on-delay's Done, ``NotImplemented`` when *tag* is no timer's Done."""
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
        if not value or len(found) != 1 or len(writers) != 1:
            return None
        node_index, timer = found[0]
        node = pdg.rung_nodes[node_index]
        acc = timer.accumulator.name
        # Skipped scans would hold the count, as a retentive timer does.
        if (
            node.subroutine is not None
            or node.guard_reads
            or timer.reset_condition is not None
            or not isinstance(timer.preset, int)
            or pdg.writers_of.get(acc, frozenset()) != writers
            or acc in self.facts.free
        ):
            return None
        per_ms = timer.unit.dt_to_units(0.001)
        counted = self.facts.snapshot.get(acc) or 0
        room_ms = (timer.preset - counted) / per_ms
        return self._dwell_limited(self._condition_of(node_index), room_ms, stack)

    def _dwell_limited(self, expr: Any, room_ms: float, stack: frozenset[Any]) -> Any:
        """Can *expr* never stay true for *room_ms* in one stretch?"""
        proved = self.never_true(expr, stack)
        if proved is not None:
            return proved
        if isinstance(expr, Or):
            return self._every_arm(expr, lambda arm: self._dwell_limited(arm, room_ms, stack))
        atoms = _conjuncts(expr)
        for coupling in self.facts.couplings:
            if self._opposition_ends_within(atoms, coupling, room_ms):
                return frozenset({(coupling.en_name, coupling.fb_name)})
        for term in atoms:
            if isinstance(term, Or):
                used = self._dwell_limited(term, room_ms, stack)
                if used is not None:
                    return used
        return None

    def _opposition_ends_within(self, atoms: list[Any], coupling: Coupling, room_ms: float) -> bool:
        """Do these contacts need the feedback against its enable, for less than the plant allows?

        Only a timing coupling has that bound: a profile (``Pulse``) has no resting value.
        """
        facts = self.facts
        dt_ms = facts.dt_ms
        fb = coupling.fb_name
        if (
            dt_ms is None
            or coupling.physical.profile is not None
            or fb in facts.forced
            or facts.pdg.writers_of.get(fb)
        ):
            return False
        driving = _enable_required(atoms, coupling)
        fb_high = _requires(atoms, coupling.fb_name, "xic")
        fb_low = _requires(atoms, coupling.fb_name, "xio")
        if driving is True and fb_low and not fb_high:
            delay_ms = coupling.physical.on_delay_ms or 0
        elif driving is False and fb_high and not fb_low:
            delay_ms = coupling.physical.off_delay_ms or 0
        else:
            return False
        return delay_ms + _WINDOW_SLOP_SCANS * dt_ms < room_ms


def linked_feedback_block(target_tag: str, target_value: Any, facts: ProofFacts) -> str | None:
    """The reason *target_tag* cannot take *target_value* with the harness links intact, if proved.

    The harness owns the feedback, and every way to the value needs a watchdog-style dwell that
    its link cuts short.  ``unlink=`` models the dead sensor that would let it finish.
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
    """The rung and the branches down to *node*: all of them are true when it fires."""
    rung = resolve_rung(facts.program, replace(node, branch_path=()))
    chain = [rung]
    for branch_index in node.branch_path:
        rung = rung._branches[branch_index]
        chain.append(rung)
    return chain


def _rung_key(node: RungNode) -> tuple[str | None, int]:
    return (node.subroutine, node.rung_index)


def _leaves_other_off(facts: ProofFacts, node_index: int, tag: str, value: Any) -> str | None:
    """How firing this node leaves *tag* off *value*, in words, or ``None``."""
    node = facts.pdg.rung_nodes[node_index]
    where = _scope_ref(node_index, node)
    for link in _rung_chain(facts, node):
        if _reads_off(_conditions_list_to_expr(link._conditions), tag, value):
            return f"{where} reads {tag} off {value!r}"
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
    """What keeps ``a`` and ``b`` from holding together, each way round, or ``None`` if unproved."""
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
