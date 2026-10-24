"""Hidden counters whose progress is dead at a key.

A hidden accumulator's progress matters only while something may read it
before it is overwritten.  A runaway guard on a jump chain is the shape this
finds::

    Rung(CmdStart):  copy(STARTING, Req); copy(0, Loops)    # each new request
    Rung(Req != 0):  call(jump)                              # the only reader
    # jump: calc(Loops + 1, Loops); Rung(Loops > 10): ...; copy(0, Req) when done

``Loops`` keeps whatever value the last request left it at, so states at a
key with ``Req == 0`` differ in it by the branch they came from.  That value
is never read: the next request overwrites it first.  ``cycles.py`` would
keep each such revisit as its own node (its counter differs from the first
node's), and so would every successor until the next request.  A
``DeadGuard`` lets it treat the counter as settled at those keys instead.

Soundness.  For a hidden counter ``X`` the analysis finds a set ``S`` of
subroutines (closed under the calls they make) and a guard ``T != c`` (or
``T == c``) on a tag ``T`` tracked exactly in the state key, such that:

1. every read of ``X`` is in ``S`` -- conditions, instruction reads, address
   reads, and any instruction that writes ``X`` other than a plain ``copy`` or
   ``calc`` (an accumulating instruction reads its own value);
2. every call into ``S`` from outside it is a top-level instruction of a
   top-level rung whose conditions include the guard;
3. every write of ``T`` outside ``S`` either copies a literal that keeps the
   guard false, or sits at the top level of a branch-free rung that also
   copies into ``X`` without a oneshot, with only ``copy``/``calc`` up to both;
4. indirect accesses outside ``S`` cannot reach the slot of ``X`` (reads) or
   of ``T`` (writes): their address sets, from a value-set fixpoint over the
   pointers' writers, defaults and indirect deposits, exclude the slot.  A
   pointer that cannot be bounded fails the analysis for that slot's block.

Claim: at a scan that starts with the guard false, the entry value of ``X``
is not read, and if ``X`` is not overwritten during the scan the guard is
false at its end.  Code in ``S`` runs only after a call whose guard held when
its rung was evaluated (2).  The guard was false at entry, so a write made it
true before that point; writes inside ``S`` cannot come first, writes of a
dead literal keep it false, so the first such write is a (3) rung, which
overwrote ``X`` on the way (its copies execute together, nothing in between
can leave the rung).  Every read of ``X`` is in ``S`` (1, 4), hence after
that overwrite.  If ``X`` is never overwritten, no (3) rung ran, ``S`` never
ran, and ``T`` holds a dead value at the end.  By induction over scans, from
a state whose key has the guard false the entry value of ``X`` reaches no
computation until ``X`` is overwritten, and each key on the way has the
guard false.  Two states at such a key that differ only in ``X`` therefore
run identically, scan for scan, except for ``X`` itself, until it is
overwritten (identically) -- whatever races the rest of the program holds.
``X``'s threshold bits in the key are computed from ``X``, which stays put,
so the two runs share every key.  A property or projection never reads a
hidden accumulator (it would be tracked exactly), so they agree there too.

Anything this does not recognize gives no guard: the counter keeps the
strict treatment in ``cycles.py``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.value_sets import (
    _INDIRECT,
    _is_plain_copy,
    _scan,
    _targets,
    _ValueSets,
)
from pyrung.core.condition import CompareEq, CompareNe, IntTruthyCondition
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.control import CallInstruction, ForLoopInstruction
from pyrung.core.instruction.conversions import _truncate_to_tag_type
from pyrung.core.memory_block import (
    BlockRange,
    IndirectBlockRange,
    IndirectExprRef,
    IndirectRef,
)
from pyrung.core.tag import Tag

if TYPE_CHECKING:
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung


@dataclass(frozen=True, slots=True)
class DeadGuard:
    """``X`` is dead at a key whose ``tag`` value makes the guard false.

    The guard is true (code reading ``X`` may run) when ``tag != value``
    (``op == "ne"``) or ``tag == value`` (``op == "eq"``).
    """

    tag: str
    op: str
    value: int

    def dead(self, current: Any) -> bool:
        if type(current) is not int:
            return False
        if self.op == "ne":
            return current == self.value
        return current != self.value


@dataclass(frozen=True, slots=True)
class _Site:
    sub: str | None
    rung: Rung
    # In a top-level rung's own instruction list (not a branch, not a loop).
    top: bool
    # Position in ``rung._instructions`` (-1 inside a loop body).
    index: int
    instr: Any


def _collect(program: Program) -> tuple[list[_Site], list[tuple[str | None, Any]]]:
    """Every instruction with its place, and every condition with its owner."""
    sites: list[_Site] = []
    conditions: list[tuple[str | None, Any]] = []

    def nested(instructions: Iterable[Any], sub: str | None, rung: Rung) -> None:
        for instr in instructions:
            sites.append(_Site(sub, rung, False, -1, instr))
            if isinstance(instr, ForLoopInstruction):
                nested(instr.instructions, sub, rung)

    def visit(rung: Rung, sub: str | None, branch: bool) -> None:
        for condition in rung._conditions:
            conditions.append((sub, condition))
        for index, instr in enumerate(rung._instructions):
            sites.append(_Site(sub, rung, not branch, index, instr))
            if isinstance(instr, ForLoopInstruction):
                nested(instr.instructions, sub, rung)
        for child in rung._branches:
            visit(child, sub, True)

    for rung in program.rungs:
        visit(rung, None, False)
    for name in sorted(program.subroutines):
        for rung in program.subroutines[name]:
            visit(rung, name, False)
    return sites, conditions


def _read_values(instr: Any) -> list[Any]:
    """Every field of *instr* that it reads, condition fields included."""
    cls = type(instr)
    fields = (
        *getattr(cls, "_reads", ()),
        *getattr(cls, "_conditions", ()),
        *getattr(cls, "_exclusive_fields", ()),
    )
    return [getattr(instr, name, None) for name in dict.fromkeys(fields)]


def _guards_of(rung: Rung) -> set[DeadGuard]:
    """Guards among a rung's own conditions (they are ANDed)."""
    out: set[DeadGuard] = set()
    for condition in rung._conditions:
        if isinstance(condition, (CompareNe, CompareEq)):
            tag, value = condition.tag, condition.value
            if isinstance(tag, Tag) and type(value) is int:
                op = "ne" if isinstance(condition, CompareNe) else "eq"
                out.add(DeadGuard(tag.name, op, value))
        elif isinstance(condition, IntTruthyCondition) and isinstance(condition.tag, Tag):
            out.add(DeadGuard(condition.tag.name, "ne", 0))
    return out


class _Analysis:
    def __init__(
        self,
        program: Program,
        tags: Mapping[str, Tag],
        unbounded: Collection[str],
        initial_state: Mapping[str, Any] | None,
    ) -> None:
        from pyrung.core.validation.walker import _instruction_fields

        self.sites, self.conditions = _collect(program)
        self.known = all(_instruction_fields(site.instr) is not None for site in self.sites)
        self.tags = tags
        self.values = _ValueSets(
            (site.instr for site in self.sites), tags, unbounded, initial_state
        )
        self.calls: dict[str | None, set[str]] = {}
        for site in self.sites:
            if isinstance(site.instr, CallInstruction):
                self.calls.setdefault(site.sub, set()).add(site.instr.subroutine_name)

    def _reads(self, value: Any, name: str) -> bool:
        names, indirect = _scan(value)
        if name in names:
            return True
        return any(self.values.may_reach(ref, name) for ref in indirect)

    def _writes(self, target: Any, name: str) -> bool:
        if isinstance(target, Tag):
            return target.name == name
        if isinstance(target, BlockRange):
            return any(tag.name == name for tag in target.tags())
        if isinstance(target, _INDIRECT):
            if isinstance(target, IndirectBlockRange):
                slots = self.values.slots(target.block, name)
                return slots is None or bool(slots)
            return self.values.may_reach(target, name)
        return False

    def _reader_subs(self, x: str) -> set[str | None]:
        subs: set[str | None] = set()
        for sub, condition in self.conditions:
            if self._reads(condition, x):
                subs.add(sub)
        for site in self.sites:
            instr = site.instr
            if any(self._reads(value, x) for value in _read_values(instr)):
                subs.add(site.sub)
                continue
            for target in _targets(instr):
                if isinstance(target, _INDIRECT):
                    address = (
                        target.pointer
                        if isinstance(target, IndirectRef)
                        else target.expr
                        if isinstance(target, IndirectExprRef)
                        else (target.start_expr, target.end_expr)
                    )
                    if self._reads(address, x):
                        subs.add(site.sub)
                        break
                # Any writer of X but a plain copy or calc may read it too
                # (an accumulating instruction adds to its own value).
                if self._writes(target, x) and not (
                    _is_plain_copy(instr) or isinstance(instr, CalcInstruction)
                ):
                    subs.add(site.sub)
                    break
        return subs

    def guard(self, x: str, eligible: Collection[str]) -> DeadGuard | None:
        if not self.known:
            return None
        readers = self._reader_subs(x)
        if not readers or None in readers:
            return None
        region: set[str] = {sub for sub in readers if sub is not None}
        pending = list(region)
        while pending:
            for callee in self.calls.get(pending.pop(), ()):
                if callee not in region:
                    region.add(callee)
                    pending.append(callee)
        entries = [
            site
            for site in self.sites
            if isinstance(site.instr, CallInstruction)
            and site.instr.subroutine_name in region
            and site.sub not in region
        ]
        if not entries or not all(site.top for site in entries):
            return None
        candidates = _guards_of(entries[0].rung)
        for site in entries[1:]:
            candidates &= _guards_of(site.rung)
        for guard in sorted(candidates, key=lambda g: (g.tag, g.op, g.value)):
            if guard.tag != x and guard.tag in eligible and self._writers_ok(guard, x, region):
                return guard
        return None

    def _writers_ok(self, guard: DeadGuard, x: str, region: set[str]) -> bool:
        t = guard.tag
        for site in self.sites:
            instr = site.instr
            hits = [target for target in _targets(instr) if self._writes(target, t)]
            if not hits or site.sub in region:
                continue
            if not all(isinstance(target, Tag) for target in hits):
                return False
            if _is_plain_copy(instr) and _literal_dead(instr, hits[0], guard):
                continue
            if not self._overwrites_with(site, x):
                return False
        return True

    def _overwrites_with(self, site: _Site, x: str) -> bool:
        """Whether *site* writes the guard tag only together with a copy into *x*."""
        instr = site.instr
        if not (_is_plain_copy(instr) or isinstance(instr, CalcInstruction)):
            return False
        rung = site.rung
        if not site.top or rung._branches:
            return False
        instructions = rung._instructions
        for j, other in enumerate(instructions):
            if not (_is_plain_copy(other) and not getattr(other, "_oneshot", False)):
                continue
            targets = _targets(other)
            if len(targets) != 1 or not isinstance(targets[0], Tag) or targets[0].name != x:
                continue
            last = max(site.index, j)
            if all(
                _is_plain_copy(item) or isinstance(item, CalcInstruction)
                for item in instructions[: last + 1]
            ):
                return True
        return False


def _literal_dead(instr: Any, dest: Tag, guard: DeadGuard) -> bool:
    source = instr.source
    if type(source) is not int:
        return False
    try:
        stored = _truncate_to_tag_type(source, dest)
    except Exception:  # noqa: BLE001
        return False
    return stored == source and guard.dead(stored)


def hidden_dead_guards(
    program: Program,
    tags: Mapping[str, Tag],
    accumulators: Iterable[str],
    *,
    eligible: Collection[str],
    unbounded: Collection[str],
    initial_state: Mapping[str, Any] | None = None,
) -> dict[str, DeadGuard]:
    """Dead guards for hidden counters (see the module docstring).

    *eligible* are the tags a guard may test: tracked exactly in the state
    key (stateful, not an input, not a Done slot).  *unbounded* are tags any
    value may reach from outside the program (inputs).
    """
    names = [name for name in accumulators]
    if not names:
        return {}
    analysis = _Analysis(program, tags, unbounded, initial_state)
    out: dict[str, DeadGuard] = {}
    for name in names:
        guard = analysis.guard(name, eligible)
        if guard is not None:
            out[name] = guard
    return out


__all__ = ["DeadGuard", "hidden_dead_guards"]
