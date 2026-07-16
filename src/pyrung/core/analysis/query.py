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
        AnyCondition,
        BitCondition,
        CompareEq,
        CompareGe,
        CompareGt,
        CompareLe,
        CompareLt,
        CompareNe,
        IntTruthyCondition,
        NormallyClosedCondition,
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
        else:
            # AnyCondition, edges, indirect compares — not statically pinnable.
            if isinstance(cond, AnyCondition):
                readable = False
            else:
                readable = False

    for cond in rung._conditions:
        walk(cond)
    return atoms, readable


def _self_increment_target(instr: Any) -> str | None:
    """Return the tag name for a ``calc(X + 1, X)`` self-increment, else None."""
    from pyrung.core.expression import BinaryExpr, LiteralExpr, TagExpr
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.tag import Tag as TagClass

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


def _constant_predicate(program: Any):
    """A register is constant if nothing (copy/calc/fill) ever writes it."""
    from pyrung.core.tag import Tag as TagClass

    names, filled = _written_registers(program)

    def is_constant(tag: Any) -> bool:
        if not isinstance(tag, TagClass):
            return False
        if tag.name in names:
            return False
        block = getattr(tag, "_pyrung_block", None)
        addr = getattr(tag, "_pyrung_block_addr", None)
        if block is not None and addr is not None and (id(block), addr) in filled:
            return False
        return True

    return is_constant


def _external_input_names(program: Any) -> set[str]:
    """Tags the ladder cannot drive: ``external`` slots, their one-hop mirrors,
    and Bool contacts never written anywhere (read-only physical inputs)."""
    from pyrung.core.instruction.coils import OutInstruction
    from pyrung.core.tag import Tag as TagClass
    from pyrung.core.tag import TagType

    scopes = _all_scopes(program)
    external: set[str] = set()

    index = _tag_index(program)
    written, _filled = _written_registers(program)
    for name, tag in index.items():
        if getattr(tag, "external", False):
            external.add(name)
        elif tag.type == TagType.BOOL and name not in written:
            external.add(name)

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


def _dead_config_atom(
    atoms: list[tuple[Any, ...]], index: dict[str, Tag], is_constant
) -> tuple[str, int] | None:
    """First guard clause unsatisfiable against a program constant, as
    ``(config_tag_name, default_value)`` — else None."""
    for atom in atoms:
        name = atom[1]
        tag = index.get(name)
        if tag is None or not is_constant(tag):
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
class WaitEscapeFinding:
    """One wait-shaped step that can hang forever with no fireable escape.

    Read-side verdict: the step advances only when an external input arrives,
    and no timeout/error rung can fire while the machine waits.  Reported as a
    design decision for the engineer — never auto-fixed.  Doubles as the
    wait-edge annotation the pilot wants (no self-escape ⇒ trace the producers,
    don't budget-wait here).
    """

    subroutine: str | None
    step_register: str
    step_value: int
    wait_inputs: tuple[str, ...]
    advance_rung: str
    ranged_escapes: tuple[tuple[str, str, str, int], ...] = ()
    dead_escapes: tuple[tuple[str, str, int], ...] = ()

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
        for rung_label, step, op, bound in self.ranged_escapes:
            clauses.append(f"{rung_label} guards {step} {_OP_SYMBOL.get(op, op)} {bound}")
        for rung_label, config_tag, default in self.dead_escapes:
            clauses.append(f"{config_tag} = {default} disables the {rung_label} timeout")
        if not clauses:
            return head
        return head + " — " + " and ".join(clauses)


def wait_edges_without_escape(program: Any) -> list[WaitEscapeFinding]:
    """Survey wait-shaped steps that can hang forever (see module docstring).

    Static and read-side: for each step register whose only advance out of a
    value ``k`` is gated on an external input, report the absence of a fireable
    escape (timeout or error rung that can actually fire under the declared
    config).  A step-range guard that excludes ``k`` or a clause dead against a
    program-constant config value does not count as an escape.  Fail-closed:
    an unreadable escape guard is treated as a possible escape, never as proof
    of absence.
    """
    index = _tag_index(program)
    is_constant = _constant_predicate(program)
    external = _external_input_names(program)

    findings: list[WaitEscapeFinding] = []
    for subroutine, rungs in _all_scopes(program):
        step_registers: set[str] = set()
        for rung in rungs:
            for instr in rung._instructions:
                target = _self_increment_target(instr)
                if target is not None:
                    step_registers.add(target)
        if not step_registers:
            continue

        for step in sorted(step_registers):
            # Advance triggers: the self-increment rungs, plus writers of any
            # transition flag that gates such an increment.
            increment_idx = {
                i
                for i, rung in enumerate(rungs)
                if any(_self_increment_target(instr) == step for instr in rung._instructions)
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
                        if atom[1] not in external:
                            timer_acc.add(atom[1])

            # Wait edges: an advance trigger pinned to ``step == k`` that
            # requires an external input.
            waits: dict[int, tuple[set[str], int]] = {}
            for i in sorted(advance_idx):
                atoms, readable = _guard_atoms(rungs[i])
                if not readable:
                    continue
                pin = next((a[2] for a in atoms if a[0] == "eq" and a[1] == step), None)
                if pin is None:
                    continue
                inputs = {a[1] for a in atoms if a[0] in ("true", "nonzero") and a[1] in external}
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

            # Fault sink: a register several escape candidates write — the shared
            # target that surfaces in the message (drops lone bookkeeping writes).
            sink_writers: dict[str, set[int]] = {}
            for i, _atoms, _readable, writes in escape_candidates:
                for name in writes:
                    sink_writers.setdefault(name, set()).add(i)
            fault_sinks = {name for name, writers in sink_writers.items() if len(writers) >= 2}

            for value, (inputs, anchor) in sorted(waits.items()):
                escaped = False
                for _i, atoms, readable, _writes in escape_candidates:
                    if not readable:
                        # Fail-closed: could fire at this step; assume it escapes.
                        escaped = True
                        break
                    if not _x_admits(atoms, step, value):
                        continue
                    if _dead_config_atom(atoms, index, is_constant) is not None:
                        continue
                    if any(a[0] in ("true", "nonzero") and a[1] in inputs for a in atoms):
                        continue
                    escaped = True
                    break
                if escaped:
                    continue

                ranged: list[tuple[str, str, str, int]] = []
                dead: list[tuple[str, str, int]] = []
                for i, atoms, readable, writes in escape_candidates:
                    if not readable or not (set(writes) & fault_sinks):
                        continue
                    dead_atom = _dead_config_atom(atoms, index, is_constant)
                    label = f"R{i + 1}"
                    if dead_atom is not None:
                        dead.append((label, dead_atom[0], dead_atom[1]))
                    elif not _x_admits(atoms, step, value):
                        pin_atom = next((a for a in atoms if len(a) == 3 and a[1] == step), None)
                        if pin_atom is not None:
                            ranged.append((label, step, pin_atom[0], pin_atom[2]))
                findings.append(
                    WaitEscapeFinding(
                        subroutine=subroutine,
                        step_register=step,
                        step_value=value,
                        wait_inputs=tuple(sorted(inputs)),
                        advance_rung=f"R{anchor + 1}",
                        ranged_escapes=tuple(ranged),
                        dead_escapes=tuple(dead),
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
