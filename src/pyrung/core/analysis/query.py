"""Whole-program dynamic analysis surveys.

``QueryNamespace`` is exposed as ``plc.query`` and provides survey methods
that aggregate dynamic history across retained scans:

- ``cold_rungs()`` — rungs that never fired (1-indexed rung numbers)
- ``hot_rungs()`` — rungs that fired every scan (1-indexed rung numbers)
- ``stranded_bits()`` — persistent bits with no reachable clear path
- ``wait_edges_without_escape()`` — wait-shaped steps that can hang forever
  (static survey; the only read-side survey here, needs no history)

These are compositions over the causal chain primitives (``cause``/``effect``)
and the per-scan ``rung_firings`` data.

Limitations
-----------
Persistent-bit detection currently considers only ``latch()``-written tags.
Tags written by ``out()`` inside conditionally-called subroutines can also
become stranded if the subroutine stops executing, but detecting that
requires call-graph analysis (not yet implemented).  Similarly, ``out()``
with mutually exclusive rung conditions can leave a tag stranded in
practice despite being structurally self-clearing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.context import RungId

if TYPE_CHECKING:
    from pyrung.core.analysis.causal import CausalChain
    from pyrung.core.rung import Rung
    from pyrung.core.runner import PLC
    from pyrung.core.tag import Tag


def find_tag_object(logic: list[Rung], tag_name: str) -> Tag | None:
    """Find a ``Tag`` object by name from a program's rung instructions."""
    from pyrung.core.tag import ImmediateRef
    from pyrung.core.tag import Tag as TagClass

    for rung in logic:
        for instr in rung._instructions:
            target = getattr(instr, "target", None)
            if target is None:
                continue
            raw = target
            if isinstance(raw, ImmediateRef):
                raw = object.__getattribute__(raw, "value")
            if isinstance(raw, TagClass) and raw.name == tag_name:
                return raw
        # Also check conditions for tag references
        for cond in rung._conditions:
            tag_obj = getattr(cond, "tag", None)
            if tag_obj is not None:
                raw = tag_obj
                if isinstance(raw, ImmediateRef):
                    raw = object.__getattribute__(raw, "value")
                if isinstance(raw, TagClass) and raw.name == tag_name:
                    return raw
    return None


def _persistent_bits(logic: list[Rung]) -> list[Tag]:
    """Return tags written by ``latch()`` instructions.

    These are the tags that require an explicit ``reset()`` to clear.
    ``out()``-driven tags are self-clearing (the instruction writes False
    when disabled) and are excluded.

    See module docstring for known limitations (subroutines, mutually
    exclusive outs).
    """
    from pyrung.core.instruction.coils import LatchInstruction
    from pyrung.core.tag import ImmediateRef
    from pyrung.core.tag import Tag as TagClass

    seen: set[str] = set()
    result: list[TagClass] = []
    for rung in logic:
        for instr in rung._instructions:
            if not isinstance(instr, LatchInstruction):
                continue
            target = instr.target
            if isinstance(target, ImmediateRef):
                target = object.__getattribute__(target, "value")
            if isinstance(target, TagClass) and target.name not in seen:
                seen.add(target.name)
                result.append(target)
    return result


def _rung_label(subroutine: str | None, rung_index: int) -> str:
    """User-facing 1-indexed rung label.

    ``"3"`` for a main rung, ``"MySub:3"`` for a rung inside subroutine
    ``MySub`` — matching the ``--- SubName ---`` / ``r{n}`` rendering used
    by ``why()``/``cause()`` chains.
    """
    n = rung_index + 1
    return f"{subroutine}:{n}" if subroutine is not None else str(n)


def _rung_sort_key(ident: tuple[str | None, int]) -> tuple[int, str, int]:
    """Order main rungs first (ascending), then subroutine rungs grouped by name."""
    subroutine, rung_index = ident
    return (0 if subroutine is None else 1, subroutine or "", rung_index)


# ---------------------------------------------------------------------------
# Hang-forever survey — wait-shaped steps with no fireable escape
# ---------------------------------------------------------------------------
#
# A step register whose only advance out of value ``k`` is gated on an external
# input is a *wait edge*.  If no timeout/error rung can fire while the machine
# sits at step ``k`` (the escape is disabled by a program-constant config value,
# or its guard excludes step ``k``), the wait can hang forever.  This is a
# read-side static survey: it reports the design decision, never fixes it.
#
# Guard reading is fail-closed.  A guard clause we cannot decode statically
# (an ``Or``, a rising edge, an indirect compare) makes the rung unreadable;
# an unreadable escape candidate is treated as a *possible* escape (so we never
# fabricate a "no escape" verdict), and an unreadable advance guard is skipped.

_OP_SYMBOL = {"eq": "==", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}


def _unwrap_target(value: Any) -> Any:
    from pyrung.core.tag import ImmediateRef

    if isinstance(value, ImmediateRef):
        return object.__getattribute__(value, "value")
    return value


def _guard_atoms(rung: Rung) -> tuple[list[tuple[Any, ...]], bool]:
    """Decode a rung's conditions into required atoms.

    Returns ``(atoms, readable)``.  ``readable`` is ``False`` when any clause
    cannot be pinned to a required value (``Or``, edges, indirect refs) — the
    conjunction is then not statically decidable and the caller fails closed.

    Atom shapes: ``("eq"|"ne"|"gt"|"ge"|"lt"|"le", tag_name, int)``,
    ``("true"|"false"|"nonzero", tag_name)``, ``("cmp_tag", tag_name, tag_name)``.
    """
    from pyrung.core.condition import (
        AllCondition,
        BitCondition,
        CompareEq,
        CompareGe,
        CompareGt,
        CompareLe,
        CompareLt,
        CompareNe,
        IntTruthyCondition,
        NormallyClosedCondition,
        RisingEdgeCondition,
    )
    from pyrung.core.tag import Tag as TagClass

    atoms: list[tuple[Any, ...]] = []
    readable = True
    _CMP = {
        CompareEq: "eq",
        CompareNe: "ne",
        CompareGt: "gt",
        CompareGe: "ge",
        CompareLt: "lt",
        CompareLe: "le",
    }

    def walk(cond: Any) -> None:
        nonlocal readable
        if isinstance(cond, AllCondition):
            for sub in cond.conditions:
                walk(sub)
        elif isinstance(cond, BitCondition):
            atoms.append(("true", _unwrap_target(cond.tag).name))
        elif isinstance(cond, NormallyClosedCondition):
            atoms.append(("false", _unwrap_target(cond.tag).name))
        elif isinstance(cond, IntTruthyCondition):
            atoms.append(("nonzero", cond.tag.name))
        elif type(cond) in _CMP:
            tag = cond.tag
            value = cond.value
            if isinstance(tag, TagClass) and isinstance(value, bool):
                readable = False
            elif isinstance(tag, TagClass) and isinstance(value, int):
                atoms.append((_CMP[type(cond)], tag.name, value))
            elif isinstance(tag, TagClass) and isinstance(value, TagClass):
                atoms.append(("cmp_tag", tag.name, value.name))
            else:
                readable = False
        elif isinstance(cond, RisingEdgeCondition):
            # rise(X) requires X to go false->true: a *stronger* demand than X,
            # and the same demand for our purposes — the rung needs X supplied.
            # Pinning it as ("true", X) keeps an edge-triggered advance visible as
            # the wait it is.  Sound in the escape direction too: an edge escape
            # needing an unguaranteed X is no more fireable than a level one.
            atoms.append(("true", _unwrap_target(cond.tag).name))
        else:
            # AnyCondition, falling edges, indirect compares — not statically
            # pinnable.  fall(X) is deliberately excluded: it needs X to have been
            # true and then drop, which a resting value cannot tell us about.
            readable = False

    for cond in rung._conditions:
        walk(cond)
    return atoms, readable


def _guard_leaf(rung: Rung, tag_name: str) -> Any | None:
    """The first leaf condition in ``rung`` that constrains ``tag_name``.

    ``_guard_atoms`` flattens a guard to ``(op, name, bound)`` tuples, which is
    all the *decision* needs but drops the condition the engineer wrote.  A
    finding has to point back at that condition to underline it, so this
    recovers the leaf by name.  ``AllCondition`` is transparent (a rung's
    implicit AND); anything else is returned as found.
    """
    from pyrung.core.condition import AllCondition
    from pyrung.core.tag import Tag as TagClass

    stack = list(rung._conditions)
    while stack:
        cond = stack.pop(0)
        if isinstance(cond, AllCondition):
            stack = list(cond.conditions) + stack
            continue
        tag = _unwrap_target(getattr(cond, "tag", None))
        if isinstance(tag, TagClass) and tag.name == tag_name:
            return cond
    return None


def _step_write_target(instr: Any) -> str | None:
    """Tag name for a write that moves a step register, else None.

    Two idioms, one meaning — the rung sets where the sequence goes next:

    * ``calc(X + 1, X)`` — the self-increment.
    * ``copy(k, X)`` — stamping the next state number in, the commoner Click form.

    ``copy(k, X)`` alone is far too broad (``copy(1, Err)`` is not a sequencer), so
    it only counts once the caller confirms the *same* rung is gated on ``X == j``:
    a rung that reads the step to decide and then writes the step is a step machine
    by construction.  The increment needs no such check — ``X + 1`` into ``X`` names
    the register itself.
    """
    from pyrung.core.expression import BinaryExpr, LiteralExpr, TagExpr
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.tag import Tag as TagClass

    if isinstance(instr, CopyInstruction):
        dest = _unwrap_target(instr.dest)
        source = instr.source
        if isinstance(dest, TagClass) and isinstance(source, int) and not isinstance(source, bool):
            return dest.name
        return None

    if not isinstance(instr, CalcInstruction):
        return None
    expr = instr.expression
    if not (isinstance(expr, BinaryExpr) and expr.symbol == "+"):
        return None
    dest = instr.dest
    if not isinstance(dest, TagClass):
        return None
    for operand, other in ((expr.left, expr.right), (expr.right, expr.left)):
        if isinstance(operand, TagExpr) and isinstance(other, LiteralExpr):
            if isinstance(operand.tag, TagClass) and operand.tag.name == dest.name:
                return dest.name
    return None


def _self_increment_target(instr: Any) -> str | None:
    """Return the tag name for a ``calc(X + 1, X)`` self-increment, else None.

    The narrow form: unlike :func:`_step_write_target` this never matches a
    ``copy``, so it identifies a step register without needing the rung's guard.
    """
    from pyrung.core.instruction.calc import CalcInstruction

    if not isinstance(instr, CalcInstruction):
        return None
    return _step_write_target(instr)


def _step_registers(rungs: list[Rung]) -> set[str]:
    """Registers this scope drives as a step machine.

    A self-increment names its own register (``calc(X + 1, X)``).  A literal
    stamp does not — ``copy(1, Err)`` is a fault write, not a sequencer — so it
    counts only when the same rung *reads* the register it writes (``Step == j``
    gating ``copy(k, Step)``).  Read-to-decide plus write-to-move is the step
    machine; either half alone is ordinary logic.

    Requiring the self-reference costs nothing downstream: a step whose advance
    is not pinned to ``step == k`` yields no wait edge anyway.
    """
    registers: set[str] = set()
    for rung in rungs:
        written = {
            name for instr in rung._instructions if (name := _step_write_target(instr)) is not None
        }
        if not written:
            continue
        for instr in rung._instructions:
            if (inc := _self_increment_target(instr)) is not None:
                registers.add(inc)
        atoms, readable = _guard_atoms(rung)
        if not readable:
            continue
        pinned = {atom[1] for atom in atoms if atom[0] == "eq" and len(atom) == 3}
        registers |= written & pinned
    return registers


def _condition_tag_names(rung: Rung) -> set[str]:
    """Every tag name referenced in a rung's conditions, decodable or not.

    Used for step-scoping unreadable guards: a rung whose guard we cannot pin
    to atoms (an ``Or``) still reveals which tags it touches, so a step-scoped
    escape we cannot decode fails closed (a possible escape) rather than being
    silently dropped.
    """
    from pyrung.core.tag import Tag as TagClass

    names: set[str] = set()
    for cond in rung._conditions:
        stack = [cond]
        while stack:
            node = stack.pop()
            subs = getattr(node, "conditions", None)
            if subs is not None:
                stack.extend(subs)
            for attr in ("tag", "value"):
                obj = _unwrap_target(getattr(node, attr, None))
                if isinstance(obj, TagClass):
                    names.add(obj.name)
    return names


def _nonzero_const_writes(rung: Rung) -> list[str]:
    """Target names of ``copy(<nonzero int literal>, target)`` writes in a rung."""
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.tag import Tag as TagClass

    result: list[str] = []
    for instr in rung._instructions:
        if isinstance(instr, CopyInstruction):
            source = instr.source
            target = _unwrap_target(instr.target)
            if (
                isinstance(target, TagClass)
                and isinstance(source, int)
                and not isinstance(source, bool)
                and source != 0
            ):
                result.append(target.name)
    return result


def _all_scopes(program: Any) -> list[tuple[str | None, list[Rung]]]:
    scopes: list[tuple[str | None, list[Rung]]] = [(None, list(program.rungs))]
    for name in sorted(program.subroutines):
        scopes.append((name, list(program.subroutines[name])))
    return scopes


def _tag_index(program: Any) -> dict[str, Tag]:
    """Name -> Tag object over every tag referenced in the program."""
    from pyrung.core.tag import Tag as TagClass

    index: dict[str, TagClass] = {}

    def note(value: Any) -> None:
        value = _unwrap_target(value)
        if isinstance(value, TagClass):
            index.setdefault(value.name, value)

    for _scope, rungs in _all_scopes(program):
        for rung in rungs:
            for cond in rung._conditions:
                stack = [cond]
                while stack:
                    node = stack.pop()
                    subs = getattr(node, "conditions", None)
                    if subs is not None:
                        stack.extend(subs)
                    note(getattr(node, "tag", None))
                    note(getattr(node, "value", None))
            for instr in rung._instructions:
                for attr in ("target", "dest", "source"):
                    note(getattr(instr, attr, None))
    return index


def _written_registers(program: Any) -> tuple[set[str], set[tuple[int, int]]]:
    """Registers written anywhere: (target names, filled (block_id, addr) pairs)."""
    from pyrung.core.instruction.data_transfer import FillInstruction
    from pyrung.core.tag import Tag as TagClass

    names: set[str] = set()
    filled: set[tuple[int, int]] = set()
    for _scope, rungs in _all_scopes(program):
        for rung in rungs:
            for instr in rung._instructions:
                for attr in ("target", "dest"):
                    target = _unwrap_target(getattr(instr, attr, None))
                    if isinstance(target, TagClass):
                        names.add(target.name)
                if isinstance(instr, FillInstruction):
                    block_range = instr.dest
                    block = getattr(block_range, "block", None)
                    for addr in getattr(block_range, "addresses", ()):
                        filled.add((id(block), addr))
    return names, filled


def _unguaranteed_names(program: Any) -> set[str]:
    """Tags whose value the program cannot author, so it cannot guarantee them.

    The survey's one predicate, and the reason it can answer "can this hang?".
    Steerability (:func:`pyrung.core.analysis.steerable.compute_steerable`) is
    usually read as *"what may I command?"*; read the other way it says *"what
    might never be steered?"* — a physical input nobody trips, an operator button
    nobody presses, a config register nobody sets.  Same fact, and it is exactly
    what the ladder cannot make happen on its own.

    Two consumers, one set: a guard clause on such a tag can neither *advance* a
    step (it is a wait) nor *rescue* one (it is not an automatic escape).  Which
    of those a clause is depends on the rung, never on the tag.

    Mirrors extend it: an ``out(dst)`` whose guard is only unguaranteed contacts
    republishes a signal the program still does not author, so ``dst`` inherits
    the property even though the ladder writes it.  Steerability alone would miss
    that (an out coil is program-authored by construction).
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.steerable import compute_steerable
    from pyrung.core.instruction.coils import OutInstruction
    from pyrung.core.tag import Tag as TagClass

    scopes = _all_scopes(program)
    index = _tag_index(program)
    pdg = build_program_graph(program)
    external: set[str] = set(compute_steerable(pdg, index, program))

    # Fixpoint: out(dst) whose guard is only external true-contacts mirrors an
    # external signal onto ``dst``.
    changed = True
    while changed:
        changed = False
        for _scope, rungs in scopes:
            for rung in rungs:
                atoms, readable = _guard_atoms(rung)
                if not readable or not atoms:
                    continue
                if not all(atom[0] == "true" and atom[1] in external for atom in atoms):
                    continue
                for instr in rung._instructions:
                    if isinstance(instr, OutInstruction):
                        target = _unwrap_target(instr.target)
                        if isinstance(target, TagClass) and target.name not in external:
                            external.add(target.name)
                            changed = True
    return external


def _x_admits(atoms: list[tuple[Any, ...]], step: str, value: int) -> bool:
    """Do the guard's constraints on ``step`` admit ``step == value``?"""
    for atom in atoms:
        if len(atom) == 3 and atom[1] == step:
            op, _name, bound = atom
            if op == "eq" and value != bound:
                return False
            if op == "ne" and value == bound:
                return False
            if op == "gt" and not value > bound:
                return False
            if op == "ge" and not value >= bound:
                return False
            if op == "lt" and not value < bound:
                return False
            if op == "le" and not value <= bound:
                return False
    return True


def _unmet_atom(
    atoms: list[tuple[Any, ...]], index: dict[str, Tag], unguaranteed: set[str]
) -> tuple[str, int] | None:
    """First guard clause the program cannot satisfy on its own, as
    ``(tag_name, resting_value)`` — else None.

    A clause on an unguaranteed tag (:func:`_unguaranteed_names`) holds only if
    the tag's resting value already satisfies it: nothing in the ladder will move
    it.  ``EnableLimit == 1`` on a register nobody writes rests at 0 and never
    fires; ``i_AbortBtn`` rests False until a human intervenes.  Both are the
    same fact — the rung needs something from outside — and neither makes the
    program's own progress possible.

    Deliberately *not* classified further.  Whether a tag is a config register
    someone should have set at commissioning or a button someone would press is a
    question about intent, and the only signals available here — type, the
    ``external`` flag — do not carry it: ``Bool("EnableLimit")`` is an ordinary way
    to write a config flag, and an Int mode selector may live on an HMI screen.
    Reporting the fact we proved ("nothing sets this") and leaving the intent to
    the engineer who wrote it beats guessing from a declaration.
    """
    for atom in atoms:
        name = atom[1]
        tag = index.get(name)
        if tag is None or name not in unguaranteed:
            continue
        default = tag.default
        op = atom[0]
        dead = (
            (op == "eq" and default != atom[2])
            or (op == "ne" and default == atom[2])
            or (op == "true" and not default)
            or (op == "nonzero" and not default)
            or (op == "false" and bool(default))
            or (op == "gt" and not default > atom[2])
            or (op == "ge" and not default >= atom[2])
            or (op == "lt" and not default < atom[2])
            or (op == "le" and not default <= atom[2])
        )
        if dead:
            return (name, default)
    return None


@dataclass(frozen=True)
class RangedEscape:
    """An escape rung that exists but guards a step range excluding the wait.

    The classic near-miss: a timeout written for ``Step == 3`` while the wait
    sits at step 1.  It reads as coverage and fires for some other step.

    ``conditions`` is the rung as written and ``guard`` is the step clause
    within it — the two a caller needs to underline the wrong range at its
    source rather than describe it.
    """

    rung_label: str
    conditions: tuple[Any, ...]
    guard: Any | None
    step: str
    op: str
    bound: int


@dataclass(frozen=True)
class UnmetEscape:
    """An escape rung needing a value the program never sets.

    The rung is aimed at the right step, but a clause tests a tag the ladder does
    not author, resting at a value that does not satisfy it.  Nothing in the
    program will move it, so the rung cannot fire unaided — a timeout switched off
    by a config register nobody set, or an abort that waits on a button nobody
    pressed.  One shape, deliberately: see :func:`_unmet_atom` on why the survey
    does not guess which.

    ``guard`` is the clause that cannot be met; ``resting`` is the value the tag
    sits at.
    """

    rung_label: str
    conditions: tuple[Any, ...]
    guard: Any | None
    tag: str
    resting: Any


@dataclass(frozen=True)
class WaitEscapeFinding:
    """One wait-shaped step that can hang forever with no fireable escape.

    Read-side verdict: the step advances only when an external input arrives,
    and no timeout/error rung can fire while the machine waits.  Reported as a
    design decision for the engineer — never auto-fixed.  Doubles as the
    wait-edge annotation the pilot wants (no self-escape ⇒ trace the producers,
    don't budget-wait here).

    ``ranged_escapes`` / ``unmet_escapes`` are the escapes that *look* like
    coverage and aren't — the diagnostic payload, since an engineer skimming
    the ladder sees a timeout and stops looking.  Each carries the rung it came
    from so a caller can show it as written; ``advance_conditions`` and
    ``wait_guard`` do the same for the waiting rung itself.
    """

    subroutine: str | None
    step_register: str
    step_value: int
    wait_inputs: tuple[str, ...]
    advance_rung: str
    advance_conditions: tuple[Any, ...] = ()
    wait_guard: Any | None = None
    ranged_escapes: tuple[RangedEscape, ...] = ()
    unmet_escapes: tuple[UnmetEscape, ...] = ()

    @property
    def location(self) -> str:
        """User-facing anchor, e.g. ``"Rotate:R8"`` or ``"R8"`` for main."""
        return (
            self.advance_rung
            if self.subroutine is None
            else f"{self.subroutine}:{self.advance_rung}"
        )

    @property
    def message(self) -> str:
        who = self.subroutine or "main"
        waits = ", ".join(self.wait_inputs)
        head = f"{who} step {self.step_value} waits on {waits} with no escape"
        clauses: list[str] = []
        for esc in self.ranged_escapes:
            clauses.append(
                f"{esc.rung_label} guards {esc.step} {_OP_SYMBOL.get(esc.op, esc.op)} {esc.bound}"
            )
        for esc in self.unmet_escapes:
            clauses.append(
                f"{esc.rung_label} needs {esc.tag}, which nothing sets (rests at {esc.resting})"
            )
        if not clauses:
            return head
        return head + " — " + " and ".join(clauses)


def wait_edges_without_escape(program: Any) -> list[WaitEscapeFinding]:
    """Survey wait-shaped steps that can hang forever (see module docstring).

    Static and read-side: for each step register whose only advance out of a
    value ``k`` needs something the program cannot author, report the absence of
    an escape the program *can* fire unaided.

    One rule decides both halves (:func:`_unguaranteed_names`): a clause on a tag
    the ladder does not author holds only if the tag's resting value already
    satisfies it.  So the advance waits (its input may never arrive) and an
    escape gated the same way does not rescue it (its clause may never be met) —
    a config-gated timeout and an operator's abort button fail for one reason,
    not two.  A step-range guard excluding ``k`` fails separately, on range.

    Fail-closed: an unreadable escape guard is treated as a possible escape,
    never as proof of absence.
    """
    index = _tag_index(program)
    unguaranteed = _unguaranteed_names(program)

    findings: list[WaitEscapeFinding] = []
    for subroutine, rungs in _all_scopes(program):
        step_registers = _step_registers(rungs)
        if not step_registers:
            continue

        for step in sorted(step_registers):
            # Advance triggers: the rungs that move the step, plus writers of any
            # transition flag that gates such a move.
            increment_idx = {
                i
                for i, rung in enumerate(rungs)
                if any(_step_write_target(instr) == step for instr in rung._instructions)
            }
            flags: set[str] = set()
            for i in increment_idx:
                atoms, readable = _guard_atoms(rungs[i])
                if not readable:
                    continue
                for atom in atoms:
                    if atom[0] in ("true", "nonzero") and atom[1] != step:
                        flags.add(atom[1])
                    elif atom[0] == "eq" and atom[1] != step and atom[2] != 0:
                        flags.add(atom[1])
            advance_idx = set(increment_idx)
            for i, rung in enumerate(rungs):
                if any(name in flags for name in _nonzero_const_writes(rung)):
                    advance_idx.add(i)

            # Step timer accumulators: non-step, non-external registers compared
            # with a range in an advance guard (e.g. ``tmr.Acc > 2``).
            timer_acc: set[str] = set()
            for i in advance_idx:
                atoms, readable = _guard_atoms(rungs[i])
                if not readable:
                    continue
                for atom in atoms:
                    if atom[0] in ("gt", "ge", "lt", "le") and atom[1] != step:
                        if atom[1] not in unguaranteed:
                            timer_acc.add(atom[1])

            # Wait edges: an advance trigger pinned to ``step == k`` that needs
            # something the program cannot supply.
            #
            # A contact (``FB``) and a threshold on a live measurement
            # (``S_Temp > 100``) are the same wait: the ladder does not author
            # either operand, so it cannot make the clause true.  The step
            # register's own pin is excluded (that is what holds us here, not what
            # we wait for), as is a timer accumulator — the program drives those,
            # which is exactly what makes them not-a-wait.
            waits: dict[int, tuple[set[str], int]] = {}
            for i in sorted(advance_idx):
                atoms, readable = _guard_atoms(rungs[i])
                if not readable:
                    continue
                pin = next((a[2] for a in atoms if a[0] == "eq" and a[1] == step), None)
                if pin is None:
                    continue
                inputs = {
                    a[1]
                    for a in atoms
                    if a[1] != step
                    and a[1] in unguaranteed
                    and a[0] in ("true", "nonzero", "gt", "ge", "lt", "le", "eq", "ne")
                }
                if inputs:
                    existing = waits.setdefault(pin, (set(), i))
                    existing[0].update(inputs)

            if not waits:
                continue

            # Escape candidates: step-scoped, state-changing rungs that are not
            # themselves the advance.  Step-scoping reads raw condition tags so
            # an unreadable guard that touches the step still counts.
            escape_candidates: list[tuple[int, list[tuple[Any, ...]], bool, list[str]]] = []
            for i, rung in enumerate(rungs):
                if i in advance_idx:
                    continue
                writes = _nonzero_const_writes(rung)
                if not writes:
                    continue
                raw_names = _condition_tag_names(rung)
                if step not in raw_names and not (raw_names & timer_acc):
                    continue
                atoms, readable = _guard_atoms(rung)
                escape_candidates.append((i, atoms, readable, writes))

            # Fault sink: the register the escapes converge on — the shared target
            # that surfaces in the message, dropping lone bookkeeping writes.
            #
            # "Shared" needs two writers to mean anything, so that is the bar when
            # there is a choice to make.  With a single candidate there is nothing
            # to disambiguate and the bar deletes the only explanation the finding
            # has (the verdict is unaffected — this filter feeds the *message*), so
            # a sole candidate's writes qualify on their own.
            sink_writers: dict[str, set[int]] = {}
            for i, _atoms, _readable, writes in escape_candidates:
                for name in writes:
                    sink_writers.setdefault(name, set()).add(i)
            _sink_floor = 1 if len(escape_candidates) == 1 else 2
            fault_sinks = {
                name for name, writers in sink_writers.items() if len(writers) >= _sink_floor
            }

            for value, (inputs, anchor) in sorted(waits.items()):
                escaped = False
                for _i, atoms, readable, _writes in escape_candidates:
                    if not readable:
                        # Fail-closed: could fire at this step; assume it escapes.
                        escaped = True
                        break
                    if not _x_admits(atoms, step, value):
                        continue
                    if _unmet_atom(atoms, index, unguaranteed) is not None:
                        continue
                    if any(a[0] in ("true", "nonzero") and a[1] in inputs for a in atoms):
                        continue
                    escaped = True
                    break
                if escaped:
                    continue

                ranged: list[RangedEscape] = []
                unmet: list[UnmetEscape] = []
                for i, atoms, readable, writes in escape_candidates:
                    if not readable or not (set(writes) & fault_sinks):
                        continue
                    rung = rungs[i]
                    unmet_atom = _unmet_atom(atoms, index, unguaranteed)
                    label = f"R{i + 1}"
                    if unmet_atom is not None:
                        unmet.append(
                            UnmetEscape(
                                rung_label=label,
                                conditions=tuple(rung._conditions),
                                guard=_guard_leaf(rung, unmet_atom[0]),
                                tag=unmet_atom[0],
                                resting=unmet_atom[1],
                            )
                        )
                    elif not _x_admits(atoms, step, value):
                        pin_atom = next((a for a in atoms if len(a) == 3 and a[1] == step), None)
                        if pin_atom is not None:
                            ranged.append(
                                RangedEscape(
                                    rung_label=label,
                                    conditions=tuple(rung._conditions),
                                    guard=_guard_leaf(rung, step),
                                    step=step,
                                    op=pin_atom[0],
                                    bound=pin_atom[2],
                                )
                            )
                sorted_inputs = tuple(sorted(inputs))
                findings.append(
                    WaitEscapeFinding(
                        subroutine=subroutine,
                        step_register=step,
                        step_value=value,
                        wait_inputs=sorted_inputs,
                        advance_rung=f"R{anchor + 1}",
                        advance_conditions=tuple(rungs[anchor]._conditions),
                        wait_guard=_guard_leaf(rungs[anchor], sorted_inputs[0]),
                        ranged_escapes=tuple(ranged),
                        unmet_escapes=tuple(unmet),
                    )
                )
    return findings


class QueryNamespace:
    """Survey namespace for whole-program dynamic analysis.

    Accessed via ``plc.query``.  Methods aggregate findings across all
    retained history scans.
    """

    def __init__(self, plc: PLC) -> None:
        self._plc = plc

    def _subroutine_rung_ids(self) -> set[RungId]:
        """All subroutine rungs in the program (the cold/hot universe for subs).

        Drawn from the PDG so subroutine rungs are visible to coverage even
        when they never fire.  Branch rungs (``branch_path != ()``) are
        excluded — branch coverage needs a separate "powered" signal the
        write-firing log can't provide.
        """
        plc = self._plc
        pdg = plc._ensure_pdg() if plc._logic else None
        if pdg is None:
            return set()
        return {
            RungId(node.subroutine, node.rung_index)
            for node in pdg.rung_nodes
            if node.scope == "subroutine" and not node.branch_path
        }

    def cold_rungs(self) -> list[str]:
        """Rung labels that never fired across retained history.

        Backed by :class:`RungFiringTimelines` — a rung with no timeline
        (or an empty timeline) is cold.  Covers main rungs (the int firing
        log) and subroutine rungs (the node firing log), so a subroutine
        that was never called is reported as cold.

        Labels are **1-indexed** to match ``why()``/``cause()`` and the
        debugger: ``"3"`` for a main rung, ``"MySub:3"`` for a subroutine
        rung.
        """
        plc = self._plc
        idents: list[tuple[str | None, int]] = []
        ever_main = plc._rung_firing_timelines.ever_fired()
        idents.extend((None, i) for i in range(len(plc._logic)) if i not in ever_main)
        sub_universe = self._subroutine_rung_ids()
        if sub_universe:
            ever_sub = plc._node_firing_timelines.ever_fired()
            idents.extend(
                (rid.subroutine, rid.rung_index) for rid in sub_universe if rid not in ever_sub
            )
        idents.sort(key=_rung_sort_key)
        return [_rung_label(sub, idx) for sub, idx in idents]

    def hot_rungs(self) -> list[str]:
        """Rung labels that fired every scan across retained history.

        A rung is "hot" if it fired on every retained scan_id (excluding
        the initial scan, which predates any rung evaluation).  Covers main
        rungs (int firing log) and subroutine rungs (node firing log).

        Labels are **1-indexed**: ``"3"`` for a main rung, ``"MySub:3"``
        for a subroutine rung.
        """
        plc = self._plc
        initial_scan_id = plc._initial_scan_id
        scan_ids = [sid for sid in plc._history.scan_ids() if sid != initial_scan_id]
        if not scan_ids:
            return []
        hot_main = set(range(len(plc._logic)))
        hot_sub = self._subroutine_rung_ids()
        for scan_id in scan_ids:
            if hot_main:
                hot_main &= plc._rung_firing_timelines.fired_on(scan_id)
            if hot_sub:
                hot_sub &= plc._node_firing_timelines.fired_on(scan_id)
            if not hot_main and not hot_sub:
                break
        idents: list[tuple[str | None, int]] = [(None, i) for i in hot_main]
        idents.extend((rid.subroutine, rid.rung_index) for rid in hot_sub)
        idents.sort(key=_rung_sort_key)
        return [_rung_label(sub, idx) for sub, idx in idents]

    def stranded_bits(self) -> list[CausalChain]:
        """Persistent bits with no reachable clear path from current state.

        Returns a list of ``CausalChain`` objects with ``mode='unreachable'``,
        one per stranded bit.  The chains carry blocker information explaining
        *why* each bit is stranded.

        Only considers ``latch()``-written tags (see module docstring for
        limitations).
        """
        persistent = _persistent_bits(self._plc._logic)
        stranded: list[CausalChain] = []
        for tag in persistent:
            chain = self._plc.cause(tag, to=tag.default)
            if chain is not None and chain.mode == "unreachable":
                stranded.append(chain)
        return stranded

    def wait_edges_without_escape(self) -> list[WaitEscapeFinding]:
        """Wait-shaped steps that can hang forever with no fireable escape.

        A static (read-side) survey over the whole program — the only survey
        here that needs no retained history.  For each step register whose only
        advance out of a value is gated on an external input, report the absence
        of a timeout or error rung that can actually fire under the declared
        config.  A design decision surfaced to the engineer, never auto-fixed.
        """
        return wait_edges_without_escape(self._plc.program)

    def report(self) -> CoverageReport:
        """Emit a per-test coverage report for merge across a test suite."""
        return CoverageReport(
            cold_rungs=frozenset(self.cold_rungs()),
            hot_rungs=frozenset(self.hot_rungs()),
            stranded_chains=frozenset(_chain_identity(c) for c in self.stranded_bits()),
        )


# ---------------------------------------------------------------------------
# Coverage report & merge
# ---------------------------------------------------------------------------


def _chain_identity(chain: CausalChain) -> tuple[str, tuple[Any, ...]]:
    """Fingerprint a stranded chain by (effect tag, blocker signature).

    Two chains with the same identity are "stranded for the same reason."
    Different blocker signatures surface refactors that silently changed
    the recovery path.
    """
    effect_tag = chain.effect.tag_name
    blocker_sig = tuple(
        (b.rung_index, b.blocked_tag, b.needed_value, b.reason.value)
        for b in sorted(chain.blockers, key=lambda b: (b.rung_index, b.blocked_tag))
    )
    return (effect_tag, blocker_sig)


@dataclass(frozen=True)
class CoverageReport:
    """Aggregated coverage findings from one test (or merged across tests).

    Merge semantics:
    - **Negative findings** (cold_rungs, stranded_chains) merge by
      **intersection** — a rung is only cold in the suite if *no* test
      fired it.
    - **Positive findings** (hot_rungs) merge by **intersection** — a
      rung is only hot in the suite if *every* test shows it hot.

    Stranded chains merge by chain identity (effect tag + blocker
    fingerprint), so "stranded for a different reason" is a distinct
    CI signal from "still stranded."
    """

    cold_rungs: frozenset[str] = field(default_factory=frozenset)
    hot_rungs: frozenset[str] = field(default_factory=frozenset)
    stranded_chains: frozenset[tuple[str, tuple[Any, ...]]] = field(default_factory=frozenset)

    def merge(self, other: CoverageReport) -> CoverageReport:
        """Merge two reports (intersection for negative, intersection for hot)."""
        return CoverageReport(
            cold_rungs=self.cold_rungs & other.cold_rungs,
            hot_rungs=self.hot_rungs & other.hot_rungs,
            stranded_chains=self.stranded_chains & other.stranded_chains,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON output."""
        return {
            "cold_rungs": sorted(self.cold_rungs),
            "hot_rungs": sorted(self.hot_rungs),
            "stranded_chains": sorted(
                {"tag": tag, "blockers": list(blockers)} for tag, blockers in self.stranded_chains
            ),
        }
