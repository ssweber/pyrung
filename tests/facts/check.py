"""Check the static facts how() reads against real runs of the program.

This module checks the facts against plain stepping.  It computes a program's facts,
drives the program with chosen inputs, and judges every fact at every scan of every run.
A fact that a scan contradicts is a soundness bug in the fact, not in the run.

The facts come from two entry points, and neither runs the search:

- ``prove.passes._run_prior_pipeline`` on a ``_PassContext`` built the way
  ``prove._build_prior_facts`` builds it.  The check keeps the context, so it also reads the
  init-constant representatives and the timer presets.
- ``pilot.drive_setup.prepare_drive`` on a fresh PLC, handed those same facts.  It gives the
  steerable inputs and the facts the CANNOT_REACH proofs read (``static_proofs.facts_for``).

What each fact claims, and how a run checks it.  The state before the first scan holds
power-on defaults that no scan produced, so the checks start at the end of scan 1.

- **Value domains** (``stateful_dims``, ``nondeterministic_dims``).  A domain lists one
  value for each way the program's comparisons can answer.  So a value outside it must
  behave like some value in it.  At the start of a scan the check swaps each such value for
  the nearest values of its domain.  A swap must answer each comparison in a rung condition
  the same way.  Then the check runs the scan on a fork and compares what the program can
  see at its end.  That is every Bool, every memory key and every comparison of a tag not
  swapped.  A swap whose own comparisons answer otherwise at the end judges nothing.  A
  counter one count short of a literal crosses it in a scan, but the domain need not hold
  that neighbour.  A tag compared with a swapped one may move too, and so may each tag
  compared with that one.  The domains cover such comparisons only through values chosen
  together.  A chain of three or more tags gets more values and more swaps to choose from.
- **Produced value domains** (``produced_value_domains``).  These list every value exactly,
  so each value a tag holds is one of them.
- **Scan-local tags.**  The world key (``stateful_names``) lists the tags whose values
  how() keeps from one scan to the next.  The scan-local tags are the written tags outside
  it, the elided tags and the absorbed values below excepted.  Their value at the start of
  a scan reaches no other tag and no memory by its end.  A probe changes them all on one
  fork and one of them alone on another.  Then it compares each fork's result with the
  run's.  A tag the classification found no finite domain for is not one of them, since
  the prior claims nothing about it.
- **Absorbed values** (``state_key_done_specs``, ``threshold_vector_specs``).  The world key
  keeps which side of each threshold a timer, counter or progress value is on, not the
  value itself.  A probe moves the value, or a threshold tag, without crossing a threshold
  or landing on one.  Then no tag of the world key changes.  Each Done also turns on or off
  only when its Acc is on the matching side of its preset.  That is the literal the fact
  gives, or the tag the instruction reads.  A time drum's completion flag turns on only at
  its last step's time, and off only on a reset.
- **Projections.**  A tag elided as ``functional_dep`` equals ``offset + scale * source``
  after every scan.  An init constant has one value for each value of its representative.
- **Stepping tags.**  A stepping tag has writes that can move it through values, such as a
  self-increment or two literal writes.  The claim is about the writes, so a run cannot
  refute it.  A rung that fires once leaves its tag still.  The report lists the stepping
  tags no run moved, and the idiom test asks that each idiom move all of them.
- **CANNOT_REACH facts** (``static_proofs``).  A tag only becomes a value that some rung in
  ``ProofFacts.producers`` writes.  Two values that ``exclusion_evidence`` keeps apart never
  hold together.  A value that ``linked_feedback_block`` says is blocked never appears.
- **Program facts** (``program_facts``).  A reference constant never changes.  A tag that
  ``scan_transient_rest`` says rests at one value holds it at the end of every scan.
- **Declared writes** (``write_sites``).  The facts learn what each instruction writes
  from ``write_sites``.  The check records the tags each instruction writes while it runs.
  Each must be in the list the facts use, or a fault flag the instruction can set.  The
  report also names each instruction class that has writes and that no run saw write
  (``Report.silent_writers``).

How the inputs are chosen.  The run drives each nondeterministic or steerable tag that the
outside can write.  These are:

- a tag marked ``external``
- a physical input
- a field feedback (``link=``) that no harness drives
- a tag the program writes too

Any other tag the program never writes is a constant, so the run leaves it alone.  Each
driven tag draws from its declared ``choices`` or ``min``/``max``, so a pointer never points
past its bounds.  It also draws every value of its input domain and every literal the
program compares it with, each plus and minus one too.  An integer without declared bounds
also gets 0 and its type's extremes.  A ``Real`` also draws the midpoint of each pair of
neighbouring values.

A seeded random walk picks a mode, runs it for some scans, then picks again:

- In chatter, each input changes on half the scans, which gives every edge combination.
- A slow walk changes each input on one scan in twenty.
- A hold keeps every input still for longer than the longest literal timer preset.
- An aim drives the inputs of one counter, timer or time drum until its Done turns on.  A
  counter's input flips every scan.  Then the aim holds the reset on for three scans.

The same seed gives the same run every time.

A fact the runs never reach passes unchecked.  So the report lists each kind of fact the
program has that no scan judged, and each Done that never turned on or never turned off.
The tests assert that list, with a reason for each entry they allow.
"""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core import PLC
from pyrung.core.analysis import value_domains
from pyrung.core.analysis.pdg import ProgramGraph, resolve_rung
from pyrung.core.analysis.pilot import drive_setup, program_facts, static_proofs
from pyrung.core.analysis.prove import passes as prove_passes
from pyrung.core.analysis.prove.results import PENDING, Intractable
from pyrung.core.analysis.simplified import And, Atom, Or, _sp_to_expr
from pyrung.core.analysis.write_sites import (
    instruction_fault_writes,
    instruction_write_targets,
    static_write_target_names,
)
from pyrung.core.expression import randint_sites
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.core.tag import TagType

#: The scan time ``_build_prior_facts`` assumes, and every run uses.
DT = 0.010

#: The RTC every run starts at.  A PLC starts its RTC at the wall clock, so a fact about a
#: copy of ``rtc.hour`` would hold between midnight and 1 am only.  No field of this time
#: is 0.
RTC_START = datetime(2026, 6, 15, 13, 30, 20)


def _plc(program: Any) -> PLC:
    """A PLC of *program* with its RTC at :data:`RTC_START`."""
    plc = PLC(program, dt=DT)
    plc.set_rtc(RTC_START)
    return plc


_INT_RANGES = {
    TagType.INT: (-32768, 32767),
    TagType.DINT: (-(2**31), 2**31 - 1),
    TagType.WORD: (0, 65535),
}
_PROJECTION_REASONS = frozenset(
    {"functional_dep", "init_constant", "init_constant_colatch", "init_constant_first_scan"}
)
#: Probe scans per run.  Each probe forks the PLC a few times and runs one scan on each fork.
PROBES_PER_RUN = 60
#: At most this many value swaps per domain judgement.
_MAX_SWAPS = 32
#: At most this many when the rungs compare three or more tags in a chain.
_MAX_CHAIN_SWAPS = 1024


@dataclass(frozen=True)
class Violation:
    """One fact a scan contradicts."""

    fact: str
    program: str
    scan: int
    detail: str
    #: What failed: a tag's name, or the names a probe blamed.
    key: str = ""

    def __str__(self) -> str:
        return f"{self.program}: {self.fact} fails at scan {self.scan}: {self.detail}"


@dataclass
class Facts:
    """Every fact the check judges, for one program."""

    program: Any
    prior: prove_passes._PriorFacts
    init_constant_reps: dict[str, str]
    done_presets: dict[str, int]
    produced: dict[str, tuple[Any, ...]]
    proofs: static_proofs.ProofFacts
    steerable: frozenset[str]
    reference_constants: frozenset[str]
    transient_rests: dict[str, Any]
    pdg: ProgramGraph
    atoms: list[Atom]
    atoms_by_tag: dict[str, list[Atom]]
    #: Written tags the classification found no finite domain for.  The world key leaves
    #: them out with no claim about them.
    unclassified: frozenset[str] = frozenset()


def compute_facts(program: Any, plc: PLC) -> Facts | Intractable:
    """The facts of *program*, from the entry points the module docstring names."""
    compiled = compile_kernel(program, blockless=True, proof_metadata=True)
    ctx = prove_passes._PassContext(program=program, dt=DT, compiled=compiled)
    prior = prove_passes._run_prior_pipeline(ctx)
    if isinstance(prior, Intractable):
        return prior
    with _prior_given(prior):
        setup = drive_setup.prepare_drive(plc, unlink=None)
    pdg = setup.pdg
    rests: dict[str, Any] = {}
    for name in pdg.writers_of:
        transient, rest = program_facts.scan_transient_rest(name, pdg, program)
        if transient:
            rests[name] = rest
    atoms = _condition_atoms(program, pdg)
    by_tag: dict[str, list[Atom]] = {}
    for atom in atoms:
        by_tag.setdefault(atom.tag, []).append(atom)
        if atom.operand_is_tag and isinstance(atom.operand, str):
            by_tag.setdefault(atom.operand, []).append(atom)
    return Facts(
        program=program,
        prior=prior,
        init_constant_reps={
            name: rep for name, (rep, _how) in (ctx._init_constant_projections or {}).items()
        },
        done_presets=dict(ctx.done_presets or {}),
        produced=value_domains.produced_value_domains(program, pdg),
        proofs=static_proofs.facts_for(setup),
        steerable=setup.steerable,
        reference_constants=program_facts.compute_reference_constants(pdg, program, plc.tags),
        transient_rests=rests,
        pdg=pdg,
        atoms=atoms,
        atoms_by_tag=by_tag,
        unclassified=frozenset(ctx._pending_infeasible_tags) | ctx._unclassified_written,
    )


@contextmanager
def _prior_given(prior: prove_passes._PriorFacts) -> Any:
    """Hand ``prepare_drive`` the facts already built, so it neither rebuilds nor differs."""
    from pyrung.core.analysis import prove

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(prove, "_build_prior_facts", lambda program, *, dt=DT, compiled=None: prior)
        yield


def _condition_atoms(program: Any, pdg: ProgramGraph) -> list[Atom]:
    """Every contact and comparison in the rungs' conditions, once each."""
    found: dict[Any, Atom] = {}
    seen: set[int] = set()

    def visit(expr: Any) -> None:
        if isinstance(expr, Atom):
            found.setdefault(expr._key(), expr)
        elif isinstance(expr, (And, Or)):
            for term in expr.terms:
                visit(term)

    for node in pdg.rung_nodes:
        rung = resolve_rung(program, node)
        if rung is None or id(rung) in seen:
            continue
        seen.add(id(rung))
        tree = rung.sp_tree()
        if tree is not None:
            visit(_sp_to_expr(tree))
    return list(found.values())


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    try:
        return bool(a == b)
    except TypeError:
        return False


def _hashable(value: Any) -> Any:
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite(value: Any) -> bool:
    return _is_number(value) and math.isfinite(value)


def _members(domain: Iterable[Any]) -> list[Any]:
    return [v for v in domain if not (isinstance(v, str) and v == PENDING)]


def _value(tags: Mapping[str, Any], name: str, facts: Facts) -> Any:
    if name in tags:
        return tags[name]
    tag = facts.pdg.tags.get(name)
    if tag is None:
        tag = SYSTEM_TAGS_BY_NAME.get(name)
    return None if tag is None else tag.default


def _answer(atom: Atom, tags: Mapping[str, Any], facts: Facts) -> Any:
    """What *atom* answers on *tags*.  A rise or fall answers nothing here."""
    left = _value(tags, atom.tag, facts)
    right = atom.operand
    if atom.operand_is_tag and isinstance(right, str):
        right = _value(tags, right, facts)
        if _is_number(right):
            right = right * atom.operand_scale + atom.operand_offset
    try:
        if atom.form in ("xic", "truthy"):
            return bool(left)
        if atom.form == "xio":
            return not left
        if atom.form == "eq":
            return left == right
        if atom.form == "ne":
            return left != right
        if atom.form == "lt":
            return left < right
        if atom.form == "le":
            return left <= right
        if atom.form == "gt":
            return left > right
        if atom.form == "ge":
            return left >= right
    except TypeError:
        return "type error"
    return None


def _literals(facts: Facts, name: str) -> list[int | float]:
    """The numbers the program compares *name* with."""
    return [
        atom.operand
        for atom in facts.atoms_by_tag.get(name, ())
        if atom.tag == name and not atom.operand_is_tag and _is_number(atom.operand)
    ]


def _nearest(value: Any, members: list[Any], reach: int = 2) -> list[Any]:
    """The members to swap for *value*, the nearest first.

    A small domain gives all its members.  A large one gives the *reach* nearest on each
    side.  An infinity is nearest the members at its own end.
    """
    if not all(_is_number(m) for m in members):
        return members[:8]
    if _is_number(value) and math.isinf(value):
        return sorted(members, reverse=value > 0)[:8]
    if not _is_finite(value):
        return members[:8]
    ordered = sorted(members, key=lambda m: abs(m - value))
    if len(members) <= 8:
        return ordered
    below = [m for m in ordered if m < value][:reach]
    above = [m for m in ordered if m > value][:reach]
    return sorted(below + above, key=lambda m: abs(m - value))


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def driven_tags(facts: Facts, plc: PLC) -> list[str]:
    """The tags a run writes (see the module docstring)."""
    feedback = {coupling.fb_name for coupling in facts.proofs.couplings}
    names = set(facts.prior.nondeterministic_dims) | set(facts.steerable)
    known = plc.tags
    out = []
    for name in sorted(names):
        tag = known.get(name)
        if tag is None or tag.readonly or name in SYSTEM_TAGS_BY_NAME or name in feedback:
            continue
        if name in facts.reference_constants:
            continue
        if not facts.pdg.written_from_outside(name) and not facts.pdg.writers_of.get(name):
            continue
        out.append(name)
    return out


def candidate_values(name: str, facts: Facts, plc: PLC) -> list[Any]:
    """The values a run gives *name* (see the module docstring)."""
    tag = plc.tags[name]
    if tag.type is TagType.BOOL:
        return [False, True]
    if tag.choices:
        return sorted(tag.choices, key=repr)
    values: list[Any] = [tag.default]
    for value in facts.prior.nondeterministic_dims.get(name, ()):
        if _is_number(value):
            values += [value, value - 1, value + 1]
        elif value != PENDING:
            values.append(value)
    for literal in _literals(facts, name):
        values += [literal, literal - 1, literal + 1]
    if tag.type in _INT_RANGES:
        lo, hi = _INT_RANGES[tag.type]
        if isinstance(tag.min, int) and isinstance(tag.max, int):
            lo, hi = tag.min, tag.max
        else:
            values += [0, lo, hi]
        values = [int(v) for v in values if _is_number(v) and lo <= v <= hi] + [lo, hi]
    elif tag.type is TagType.REAL:
        if tag.min is not None and tag.max is not None:
            values = [float(v) for v in values if _is_number(v) and tag.min <= v <= tag.max]
            values += [float(tag.min), float(tag.max)]
        else:
            values = [float(v) for v in values if _is_number(v)] + [0.0, 0.5, -1.5, 1e6]
        # A Real holds the values between whole numbers too.
        points = sorted({v for v in values if math.isfinite(v)})
        values += [(a + b) / 2 for a, b in itertools.pairwise(points)]
    elif tag.type is TagType.CHAR:
        values = [v for v in values if isinstance(v, str) and len(v) <= 1] + ["A", "0"]
    unique: list[Any] = []
    for value in values:
        if not any(_same(value, u) for u in unique):
            unique.append(value)
    return unique


def hold_scans(program: Any) -> int:
    """Scans a hold lasts, past the longest literal timer preset and at most 400."""
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.validation._common import walk_instructions

    longest = 0
    for instr in walk_instructions(program):
        if isinstance(instr, (OnDelayInstruction, OffDelayInstruction)) and isinstance(
            instr.preset, int
        ):
            longest = max(longest, math.ceil(instr.preset * instr.unit.ns / (DT * 1e9)))
    return min(400, max(30, longest + 10))


@dataclass
class Aim:
    """Inputs that run one counter, timer or drum until its Done turns on, then reset it.

    *run* holds the driven Bools that enable it and keep its reset off.  A counter
    counts edges, so *toggle* names a Bool that flips every scan.  *reset* turns the
    reset on, when one is found.
    """

    run: dict[str, bool]
    toggle: str | None
    reset: dict[str, bool] | None
    scans: int


def _holds(expr: Any, assign: Mapping[str, bool]) -> bool | None:
    """Whether *expr* holds with the Bools in *assign*, or ``None`` if other tags decide.

    A rise or fall reads as its tag, so a Bool that flips every scan gives it edges.
    """
    if expr is None:
        return True
    if isinstance(expr, Atom):
        if expr.tag not in assign:
            return None
        value = assign[expr.tag]
        if expr.form in ("xic", "truthy", "rise"):
            return value
        if expr.form in ("xio", "fall"):
            return not value
        return None
    if not isinstance(expr, (And, Or)):
        return None
    answers = [_holds(term, assign) for term in expr.terms]
    if isinstance(expr, And):
        return False if False in answers else None if None in answers else True
    return True if True in answers else None if None in answers else False


def _tag_names(expr: Any) -> set[str]:
    if isinstance(expr, Atom):
        return {expr.tag}
    if isinstance(expr, (And, Or)):
        return set().union(*(_tag_names(term) for term in expr.terms))
    return set()


def _preset_scans(preset: Any, unit_ns: int | None, values: Mapping[str, list[Any]]) -> int:
    """Scans to reach *preset*, a count for a counter or a time in *unit_ns* for a timer."""
    if _is_number(preset):
        reach = preset
    else:
        drawn = [v for v in values.get(getattr(preset, "name", ""), ()) if _is_number(v)]
        reach = max(drawn, default=getattr(preset, "default", 0) or 0)
    reach = abs(reach)
    if unit_ns is None:
        return int(2 * reach + 6)
    return math.ceil(reach * unit_ns / (DT * 1e9)) + 10


def aims(program: Any, values: Mapping[str, list[Any]]) -> list[Aim]:
    """An aim for each counter, timer and time drum whose inputs the run drives.

    A random walk seldom counts a counter up to its preset.  Each scan that a chattering
    input changes, the reset may change too.
    """
    from pyrung.core.analysis.simplified import _condition_to_expr
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.drums import TimeDrumInstruction
    from pyrung.core.instruction.timers import OnDelayInstruction
    from pyrung.core.validation._common import walk_instructions

    bools = {n for n, v in values.items() if v == [False, True]}

    def expr(condition: Any) -> Any:
        return None if condition is None else _condition_to_expr(condition)

    found = []
    for instr in walk_instructions(program):
        if isinstance(instr, CountUpInstruction):
            run, edges = instr.up_condition, True
            stops = [instr.reset_condition, instr.down_condition]
            scans = _preset_scans(instr.preset, None, values)
        elif isinstance(instr, CountDownInstruction):
            run, stops, edges = instr.down_condition, [instr.reset_condition], True
            scans = _preset_scans(instr.preset, None, values)
        elif isinstance(instr, OnDelayInstruction):
            run, stops, edges = instr.enable_condition, [instr.reset_condition], False
            scans = _preset_scans(instr.preset, instr.unit.ns, values)
        elif isinstance(instr, TimeDrumInstruction):
            run, stops, edges = instr.auto_condition, [instr.reset_condition], False
            scans = sum(_preset_scans(p, instr.unit.ns, values) for p in instr.presets)
        else:
            continue
        run_expr = expr(run)
        stop_exprs = [expr(c) for c in stops if c is not None]
        names = _tag_names(run_expr).union(*map(_tag_names, stop_exprs)) & bools
        if len(names) > 10:
            continue
        ordered = sorted(names)
        rows = [
            dict(zip(ordered, row, strict=True))
            for row in itertools.product((False, True), repeat=len(ordered))
        ]
        aim = _aim_from(rows, run_expr, stop_exprs, edges, min(scans, 400))
        if aim is not None:
            found.append(aim)
    return found


def _aim_from(
    rows: list[dict[str, bool]], run: Any, stops: list[Any], edges: bool, scans: int
) -> Aim | None:
    """The first row that runs the instruction with its stops off, and a row that resets it."""

    def runs(row: Mapping[str, bool]) -> bool:
        return _holds(run, row) is not False and all(_holds(s, row) is False for s in stops)

    for row in rows:
        if not runs(row):
            continue
        toggle = None
        if edges:
            flipped = [{**row, n: not row[n]} for n in row]
            flips = [
                n
                for n, other in zip(row, flipped, strict=True)
                if _holds(run, other) is False and all(_holds(s, other) is False for s in stops)
            ]
            if not flips:
                continue
            toggle = flips[0]
        reset = next((r for r in rows if stops and _holds(stops[0], r) is True), None)
        return Aim(row, toggle, reset, scans)
    return None


class RandomInputs:
    """A seeded walk over each driven tag's candidate values, one mode at a time."""

    def __init__(
        self,
        values: Mapping[str, list[Any]],
        seed: int,
        hold: int,
        aims: Iterable[Aim] = (),
    ) -> None:
        self.values = values
        self.random = random.Random(seed)
        self.hold = hold
        self.aims = list(aims)
        self.turn = seed
        self.left = 0
        self.rate = 0.0
        self.script: list[dict[str, Any]] = []
        self.current: dict[str, Any] = {}

    def next(self) -> dict[str, Any]:
        if self.left == 0 and not self.script:
            modes = ["chatter", "slow", "hold"] + ["aim"] * bool(self.aims)
            mode = self.random.choice(modes)
            if mode == "aim":
                self.script = self._aim_script()
            else:
                self.rate = {"chatter": 0.5, "slow": 0.05, "hold": 0.0}[mode]
                self.left = self.hold if mode == "hold" else self.random.randint(10, 80)
        if self.script:
            # An aim sets only its own inputs, and the others hold still.
            want = self.script.pop(0)
        else:
            self.left -= 1
            want = {
                name: self.random.choice(choices)
                for name, choices in self.values.items()
                if name not in self.current or self.random.random() < self.rate
            }
        changed = {}
        for name, value in want.items():
            if name not in self.current or not _same(value, self.current[name]):
                changed[name] = value
            self.current[name] = value
        return changed

    def _aim_script(self) -> list[dict[str, Any]]:
        """Each scan's inputs for the next aim, then three scans of its reset."""
        aim = self.aims[self.turn % len(self.aims)]
        self.turn += 1
        script = []
        for scan in range(aim.scans):
            row: dict[str, Any] = dict(aim.run)
            if aim.toggle is not None and scan % 2:
                row[aim.toggle] = not aim.run[aim.toggle]
            script.append(row)
        if aim.reset is not None:
            script += [dict(aim.reset) for _ in range(3)]
        return script


# ---------------------------------------------------------------------------
# The judge: what a scan's end state alone shows
# ---------------------------------------------------------------------------


@dataclass
class Tally:
    """How often each fact was judged, so a check that judged nothing shows it."""

    runs: int = 0
    scans: int = 0
    probes: int = 0
    judged: dict[str, int] = field(default_factory=dict)

    def note(self, fact: str, count: int = 1) -> None:
        self.judged[fact] = self.judged.get(fact, 0) + count


class Judge:
    """Judges the facts of one program on the states it is shown."""

    def __init__(self, label: str, facts: Facts) -> None:
        self.label = label
        self.facts = facts
        self.tally = Tally()
        self.violations: list[Violation] = []
        #: Stepping tags no run moved (see the module docstring).
        self.unmoved: list[str] = []
        self._marks: set[str] = set()
        prior = facts.prior
        # An advice-only projection is a hint the walker tries on a fork, and it claims
        # nothing about a scan.  A projected one has left the world key.
        self.projected = {
            name: dep
            for name, dep in prior.functional_dep_projections.items()
            if prior.elided_tags.get(name) == "functional_dep"
        }
        self.init_pairs: dict[str, dict[Any, Any]] = {n: {} for n in facts.init_constant_reps}
        self.stepping_values: dict[str, list[Any]] = {n: [] for n in prior.stepping_tags}
        self._producers: dict[tuple[str, Any], Any] = {}
        self.bools = sorted(
            name
            for name in facts.pdg.writers_of
            if (tag := facts.pdg.tags.get(name)) is not None
            and tag.type is TagType.BOOL
            and name not in SYSTEM_TAGS_BY_NAME
        )
        self.bool_rows: set[tuple[bool, ...]] = set()
        self.became: set[tuple[str, Any]] = set()
        self.start: Mapping[str, Any] = {}
        self.done = _done_relations(facts)
        #: Each Done and the value it turned to, in some scan.
        self.flips: set[tuple[str, bool]] = set()

    def fail(self, fact: str, scan: int, detail: str, key: str) -> None:
        """Record a violation, once for each fact and key."""
        mark = f"{fact}|{key}"
        if mark not in self._marks:
            self._marks.add(mark)
            self.violations.append(Violation(fact, self.label, scan, detail, key))

    def power_on(self, tags: Mapping[str, Any]) -> None:
        self.start = tags

    def see(self, scan: int, tags: Mapping[str, Any], before: Mapping[str, Any]) -> None:
        """Judge the state at the end of *scan*, given the state at its start."""
        facts = self.facts
        self.tally.note("produced domain", len(facts.produced))
        for name, domain in facts.produced.items():
            got = _value(tags, name, facts)
            if not any(_same(got, member) for member in domain):
                self.fail("produced domain", scan, f"{name} = {got!r}, not in {domain!r}", name)
        for name, (source, scale, offset) in self.projected.items():
            self.tally.note("functional dep")
            want = offset + scale * _value(tags, source, facts)
            got = _value(tags, name, facts)
            if not _same(got, want):
                detail = f"{name} = {got!r}, but {offset} + {scale} * {source} = {want!r}"
                self.fail("functional dep", scan, detail, name)
        for name, rest in facts.transient_rests.items():
            self.tally.note("transient rest")
            got = _value(tags, name, facts)
            if not _same(got, rest):
                self.fail("transient rest", scan, f"{name} = {got!r}, rests at {rest!r}", name)
        for name, rep in facts.init_constant_reps.items():
            self.tally.note("init constant")
            pairs = self.init_pairs[name]
            key = _hashable(_value(tags, rep, facts))
            got = _value(tags, name, facts)
            if key in pairs and not _same(pairs[key], got):
                detail = f"{name} = {got!r} and {pairs[key]!r}, both with {rep} = {key!r}"
                self.fail("init constant", scan, detail, name)
            pairs.setdefault(key, got)
        for name, seen in self.stepping_values.items():
            got = _value(tags, name, facts)
            if len(seen) < 2 and not any(_same(got, v) for v in seen):
                seen.append(got)
        for name in facts.reference_constants:
            self.tally.note("reference constant")
            got = _value(tags, name, facts)
            if not _same(got, _value(self.start, name, facts)):
                self.fail("reference constant", scan, f"{name} changed to {got!r}", name)
        self.bool_rows.add(tuple(bool(_value(tags, name, facts)) for name in self.bools))
        self._judge_writes(scan, tags, before)
        self._judge_done(scan, tags, before)

    def _judge_writes(self, scan: int, tags: Mapping[str, Any], before: Mapping[str, Any]) -> None:
        proofs = self.facts.proofs
        for name in self.facts.pdg.writers_of:
            if name in proofs.free:
                continue
            now = _value(tags, name, self.facts)
            if _same(now, _value(before, name, self.facts)):
                continue
            key = (name, _hashable(now))
            self.became.add(key)
            if key not in self._producers:
                self._producers[key] = proofs.producers(name, now)
            self.tally.note("producers")
            if self._producers[key] == ():
                detail = f"{name} became {now!r}, and no rung produces it"
                self.fail("producers", scan, detail, repr(key))

    def _judge_done(self, scan: int, tags: Mapping[str, Any], before: Mapping[str, Any]) -> None:
        for done, rule in self.done.items():
            was = bool(_value(before, done, self.facts))
            now = bool(_value(tags, done, self.facts))
            if was == now:
                continue
            self.flips.add((done, now))
            self.tally.note("done spec")
            if not rule.allows(now, tags, before, self.facts):
                acc = _value(tags, rule.acc, self.facts)
                detail = (
                    f"{done} went {was} -> {now} with {rule.acc} = {acc!r},"
                    f" {rule.kind} preset {rule.preset!r}"
                )
                self.fail("done spec", scan, detail, done)

    def finish(self) -> None:
        """Judge what only all the runs together answer."""
        self.unmoved = sorted(n for n, seen in self.stepping_values.items() if len(seen) < 2)
        self.tally.note("stepping", len(self.stepping_values))
        self._judge_exclusions()
        self._judge_links()

    def _judge_exclusions(self) -> None:
        pairs: set[tuple[tuple[str, bool], tuple[str, bool]]] = set()
        for row in self.bool_rows:
            pairs.update(itertools.combinations(zip(self.bools, row, strict=True), 2))
        for a, b in sorted(pairs):
            self.tally.note("exclusion")
            evidence = static_proofs.exclusion_evidence(self.facts.proofs, a, b)
            if evidence is not None:
                detail = f"{a} and {b} held together, against {evidence}"
                self.fail("exclusion", self.tally.scans, detail, repr((a, b)))

    def _judge_links(self) -> None:
        proofs = self.facts.proofs
        if not proofs.couplings:
            return
        for name, value in sorted(self.became, key=repr):
            self.tally.note("linked block")
            blocked = static_proofs.linked_feedback_block(name, value, proofs)
            if blocked is not None:
                detail = f"{name} became {value!r}, against {blocked}"
                self.fail("linked block", self.tally.scans, detail, repr((name, value)))


@dataclass(frozen=True)
class _DoneRule:
    """When a world-key Done may turn on or off, by the Acc its spec pairs it with.

    *preset* is the fact's literal preset, or the name of the tag the instruction reads it
    from.  A time drum's Done is its completion flag, and *last* is its last step.
    """

    kind: str
    acc: str
    preset: Any
    step: str | None = None
    last: int = 0

    def allows(
        self, now: bool, tags: Mapping[str, Any], before: Mapping[str, Any], facts: Facts
    ) -> bool:
        acc = _value(tags, self.acc, facts)
        if isinstance(self.preset, str):
            # The instruction reads its preset tag during the scan, so either end will do.
            presets = [_value(tags, self.preset, facts), _value(before, self.preset, facts)]
        else:
            presets = [self.preset]
        presets = [p for p in presets if _is_number(p)]
        if not _is_number(acc) or not presets:
            return True
        if self.kind == "time_drum":
            step = _value(tags, self.step or "", facts)
            # The flag turns on at the last step's time, and only a reset turns it off.
            if now:
                return step == self.last and any(acc >= p for p in presets)
            return step == 1
        if self.kind == "off_delay":
            return now or any(acc >= p for p in presets)

        def reached(p: Any) -> bool:
            return acc <= p if self.kind == "count_down" else acc >= p

        # A reset or a disabled timer clears the Acc with Done.
        if now:
            return any(reached(p) for p in presets)
        return acc == 0 or not all(reached(p) for p in presets)


def _done_relations(facts: Facts) -> dict[str, _DoneRule]:
    """Each world-key Done, with what its instruction says about when it turns on or off."""
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.drums import TimeDrumInstruction
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.validation._common import walk_instructions

    kinds = (OnDelayInstruction, OffDelayInstruction, CountUpInstruction, CountDownInstruction)
    owners: dict[str, Any] = {}
    for instr in walk_instructions(facts.program):
        if isinstance(instr, kinds):
            owners[instr.done_bit.name] = instr
        elif isinstance(instr, TimeDrumInstruction):
            owners[instr.completion_flag.name] = instr
    names = facts.prior.stateful_names
    found = {}
    for spec in facts.prior.state_key_done_specs:
        done = names[spec.index]
        instr = owners.get(done)
        if instr is None:
            continue
        if isinstance(instr, TimeDrumInstruction):
            last = instr.presets[-1]
            preset = last if _is_number(last) else last.name
            found[done] = _DoneRule(
                spec.kind, spec.acc_name, preset, instr.current_step.name, len(instr.presets)
            )
        elif _is_number(instr.preset):
            preset = facts.done_presets.get(done, instr.preset)
            found[done] = _DoneRule(spec.kind, spec.acc_name, preset)
        else:
            found[done] = _DoneRule(spec.kind, spec.acc_name, instr.preset.name)
    return found


# ---------------------------------------------------------------------------
# Probes: the same scan on a fork, with values changed at its start
# ---------------------------------------------------------------------------


def _differences(a: PLC, b: PLC, leave_out: Iterable[str]) -> list[str]:
    """The tags and memory keys that differ between *a* and *b*, *leave_out* aside."""
    skip = set(leave_out)
    out = []
    for part in ("tags", "memory"):
        left, right = getattr(a.current_state, part), getattr(b.current_state, part)
        for key in set(left) | set(right):
            if key not in skip and not _same(left.get(key), right.get(key)):
                out.append(f"{key}: {left.get(key)!r} vs {right.get(key)!r}")
    return sorted(out)


def _seen_by_program(plc: PLC, facts: Facts, leave_out: set[str]) -> dict[str, Any]:
    """What the program can see of *plc*'s state: Bools, comparison answers and memory.

    A comparison of a tag in *leave_out* is left out too.
    """
    state = plc.current_state
    seen: dict[str, Any] = {}
    for name, value in state.tags.items():
        if isinstance(value, bool) and name not in leave_out:
            seen[name] = value
    for atom in facts.atoms:
        if (
            atom.form in ("rise", "fall")
            or atom.tag in leave_out
            or _names(atom.operand, leave_out)
        ):
            continue
        seen[repr(atom._key())] = _answer(atom, state.tags, facts)
    for key, value in state.memory.items():
        if key not in leave_out:
            seen[f"memory {key}"] = _hashable(value)
    return seen


def _names(operand: Any, names: set[str]) -> bool:
    return isinstance(operand, str) and operand in names


def _start_answers(
    tags: Mapping[str, Any], facts: Facts, about: set[str] | None = None
) -> dict[str, Any]:
    """What each comparison in a rung condition answers on *tags*, or each one *about* names."""
    return {
        repr(atom._key()): _answer(atom, tags, facts)
        for atom in facts.atoms
        if atom.form not in ("rise", "fall")
        and (about is None or atom.tag in about or _names(atom.operand, about))
    }


def _mismatch(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    return sorted(
        f"{k}: {a.get(k)!r} vs {b.get(k)!r}"
        for k in set(a) | set(b)
        if not _same(a.get(k), b.get(k))
    )


def scan_local_tags(facts: Facts) -> list[str]:
    """The written tags the world key leaves out with no stand-in (see the module docstring)."""
    prior = facts.prior
    projections = {n for n, why in prior.elided_tags.items() if why in _PROJECTION_REASONS}
    absorbed = {spec.acc_name for spec in prior.state_key_done_specs}
    absorbed |= {spec.acc_name for spec in prior.threshold_vector_specs}
    outside = (
        set(facts.pdg.writers_of)
        - set(prior.stateful_names)
        - set(prior.nondeterministic_dims)
        - set(threshold_tags(facts))
        - set(SYSTEM_TAGS_BY_NAME)
        - facts.unclassified
    )
    return sorted((outside | set(prior.elided_tags)) - projections - absorbed)


def threshold_tags(facts: Facts) -> list[str]:
    """Tags the world key keeps only as which side of them an absorbed value is on."""
    found = {
        atom.threshold
        for spec in facts.prior.threshold_vector_specs
        for atom in spec.atoms
        if isinstance(atom.threshold, str) and atom.mode == "abstract"
    }
    keep = set(facts.prior.stateful_names) | set(facts.prior.nondeterministic_dims)
    return sorted(found - keep)


@dataclass
class _Band:
    """The numbers an absorbed value is compared with, and tags read at the time.

    *unit* is how far the value moves in one scan.
    """

    bounds: list[Any]
    unit: int

    def sides(self, value: Any, tags: Mapping[str, Any], facts: Facts) -> Any:
        """-1, 0 or 1 for below, at or above each bound.  ``None`` if a value is not a number."""
        out = []
        for bound in self.bounds:
            at = _value(tags, bound, facts) if isinstance(bound, str) else bound
            if not _is_number(at) or not _is_number(value):
                return None
            out.append((value > at) - (value < at))
        return tuple(out)


def _bands(facts: Facts) -> dict[str, _Band]:
    """The band of every absorbed value, by its name."""
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.out_variants import (
        DebounceInstruction,
        FlasherInstruction,
        TimedInstruction,
    )
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.validation._common import walk_instructions

    timers = (OnDelayInstruction, OffDelayInstruction)
    steps: dict[str, int] = {}
    presets: dict[str, list[Any]] = {}

    def bound(value: Any) -> Any:
        return value if _is_number(value) else value.name

    for instr in walk_instructions(facts.program):
        if isinstance(instr, timers):
            steps[instr.accumulator.name] = math.ceil(DT * 1e9 / instr.unit.ns)
        if isinstance(instr, (*timers, CountUpInstruction, CountDownInstruction)):
            presets[instr.accumulator.name] = [bound(instr.preset)]
        # A coil with its own timers compares each accumulator with both its presets.
        if isinstance(instr, (DebounceInstruction, FlasherInstruction, TimedInstruction)):
            accs = [getattr(instr, f, None) for f in ("on_acc", "off_acc", "timer_acc")]
            ends = [getattr(instr, f, None) for f in ("on_preset", "off_preset")]
            for acc in accs:
                if acc is not None:
                    steps[acc.name] = math.ceil(DT * 1e9 / instr.unit.ns)
                    presets[acc.name] = [bound(end) for end in ends if end is not None]
    prior = facts.prior
    bands: dict[str, _Band] = {}

    def band(acc: str) -> _Band:
        if acc not in bands:
            bands[acc] = _Band([0, *presets.get(acc, ())], steps.get(acc, 1))
        return bands[acc]

    for spec in prior.state_key_done_specs:
        if spec.kind != "time_drum":
            band(spec.acc_name)
    for spec in prior.threshold_vector_specs:
        each = band(spec.acc_name)
        for atom in spec.atoms:
            if atom.threshold not in each.bounds:
                each.bounds.append(atom.threshold)
    for acc, each in bands.items():
        for literal in _literals(facts, acc):
            if literal not in each.bounds:
                each.bounds.append(literal)
    return bands


@dataclass
class _Fork:
    why: str
    change: dict[str, Any]
    plc: PLC
    leave_out: set[str]
    # For a domain judgement: the state before the scan, its patch, and each tag's swaps.
    base: PLC | None = None
    patch: dict[str, Any] = field(default_factory=dict)
    near: dict[str, list[Any]] = field(default_factory=dict)


class Prober:
    """Runs a scan again on forks with changed starting values, and compares the results."""

    def __init__(self, facts: Facts, driven: Iterable[str], seed: int) -> None:
        self.facts = facts
        self.random = random.Random(seed)
        skip = set(driven)
        self.scan_local = [n for n in scan_local_tags(facts) if n not in skip]
        #: Scan-local tags, whose values the world key never sees.
        self.unseen = set(scan_local_tags(facts))
        self.thresholds = [n for n in threshold_tags(facts) if n not in skip]
        self.bands = _bands(facts)
        prior = facts.prior
        self.domains = {
            **{n: ("input domain", _members(d)) for n, d in prior.nondeterministic_dims.items()},
            **{n: ("stateful domain", _members(d)) for n, d in prior.stateful_dims.items()},
        }
        #: The tags each domain tag is compared with in a rung condition.
        self.partners: dict[str, set[str]] = {}
        for atom in facts.atoms:
            other = atom.operand
            if atom.operand_is_tag and isinstance(other, str) and other != atom.tag:
                if atom.tag in self.domains and other in self.domains:
                    self.partners.setdefault(atom.tag, set()).add(other)
                    self.partners.setdefault(other, set()).add(atom.tag)
        #: The tags each domain tag is compared with, directly or through others, itself too.
        self.groups: dict[str, frozenset[str]] = {}
        for name in sorted(self.partners):
            if name in self.groups:
                continue
            group: set[str] = set()
            todo = [name]
            while todo:
                each = todo.pop()
                if each not in group:
                    group.add(each)
                    todo.extend(self.partners.get(each, ()))
            for each in group:
                self.groups[each] = frozenset(group)
        self.judged_cells: set[tuple[str, Any]] = set()
        #: Which scan-local tag the next probe changes alone.
        self.turn = 0
        #: Values found in their domains at a scan's start, by kind of domain.
        self.tally_members: dict[str, int] = {}

    # -- building the forks before the scan ---------------------------------

    def forks(self, plc: PLC, patch: Mapping[str, Any], probe: bool) -> list[_Fork]:
        """The forks to run beside *plc*'s next scan, which *patch* starts."""
        tags = {**plc.current_state.tags, **patch}
        out: list[_Fork] = []
        if probe:
            out.append(self._fork(plc, patch, "none", {}))
            if self.scan_local:
                change = {n: self._other(n, _value(tags, n, self.facts)) for n in self.scan_local}
                out.append(self._fork(plc, patch, "scan-local", change))
                # A change that only reaches other scan-local tags hides in the joint fork.
                one = self.scan_local[self.turn % len(self.scan_local)]
                self.turn += 1
                out.append(self._fork(plc, patch, "scan-local", {one: change[one]}))
            for name, moved in self._band_moves(tags):
                fork = self._fork(plc, patch, "absorbed value", {name: moved})
                fork.leave_out |= self.unseen
                out.append(fork)
        near = self._domain_swaps(tags, probe)
        if near is not None:
            change = {name: _value(tags, name, self.facts) for name in near}
            near = {**near, **self._partner_swaps(near, tags)}
            leave_out = set(near) | {f"_frac:{n}" for n in near}
            fork = _Fork("domain", change, plc, leave_out, base=plc.fork(inherit_log=False))
            fork.patch, fork.near = dict(patch), near
            out.append(fork)
        return out

    def _fork(self, plc: PLC, patch: Mapping[str, Any], why: str, change: dict[str, Any]) -> _Fork:
        fork = plc.fork(inherit_log=False)
        fork.patch({**patch, **change})
        leave_out = set(change) | {f"_frac:{n}" for n in change}
        # A fork drops the patches pending on its source, so _culprits patches the scan again.
        return _Fork(why, change, fork, leave_out, patch=dict(patch))

    def _other(self, name: str, value: Any) -> Any:
        """A value of *name*'s type other than *value*, often one the program compares it with."""
        tag = self.facts.pdg.tags.get(name)
        kind = tag.type if tag is not None else None
        if kind is TagType.BOOL or isinstance(value, bool):
            return not value
        if kind is TagType.CHAR or isinstance(value, str):
            return "Z" if value != "Z" else "A"
        if tag is not None and tag.choices:
            return self.random.choice([c for c in tag.choices if c != value] or [value])
        literals = _literals(self.facts, name)
        options = [v for v in (*literals, value + 1, value - 1, 0, 100) if v != value]
        pick = self.random.choice(options)
        if kind is TagType.REAL:
            return float(pick)
        lo, hi = _INT_RANGES.get(kind, (-32768, 32767))
        return int(min(hi, max(lo, pick)))

    def _band_moves(self, tags: Mapping[str, Any]) -> list[tuple[str, Any]]:
        """For each absorbed value and threshold tag, a move that crosses no threshold."""
        facts = self.facts
        moves = []
        for acc, band in self.bands.items():
            value = _value(tags, acc, facts)
            want = band.sides(value, tags, facts)
            if want is None:
                continue
            for _ in range(8):
                moved = self._step(value, band.unit)
                if band.sides(moved, tags, facts) == want:
                    moves.append((acc, moved))
                    break
        for name in self.thresholds:
            value = _value(tags, name, facts)
            if not _is_number(value):
                continue
            related = [(acc, b) for acc, b in self.bands.items() if name in b.bounds]
            want = [b.sides(_value(tags, acc, facts), tags, facts) for acc, b in related]
            if not related or any(w is None for w in want):
                continue
            for _ in range(8):
                moved = self._step(value, 1)
                moved_tags = {**tags, name: moved}
                now = [b.sides(_value(tags, acc, facts), moved_tags, facts) for acc, b in related]
                if now == want:
                    moves.append((name, moved))
                    break
        return moves

    def _step(self, value: Any, unit: int) -> Any:
        """A move of *value* up or down, from one count to far past a scan's move."""
        size = self.random.choice((1, 2, unit, 2 * unit, 5 * unit, 50 * unit, 200 * unit))
        step = size * self.random.choice((1, -1))
        if isinstance(value, int):
            return int(max(-32768, min(32767, value + step)))
        return value + step

    def _domain_swaps(self, tags: Mapping[str, Any], probe: bool) -> dict[str, list[Any]] | None:
        """The tags outside their domains at a scan's start, each with the values to swap in.

        Each gap between domain values is judged once per run, and again at probe scans.
        """
        outside: dict[str, list[Any]] = {}
        fresh = False
        for name, (kind, members) in self.domains.items():
            value = _value(tags, name, self.facts)
            if not members or any(_same(value, m) for m in members):
                key = f"{kind}, value in it"
                self.tally_members[key] = self.tally_members.get(key, 0) + 1
                continue
            near = _nearest(value, members, self._reach(name))
            outside[name] = near
            cell = (name, tuple(_hashable(v) for v in near))
            if cell not in self.judged_cells:
                self.judged_cells.add(cell)
                fresh = True
        if not outside or not (fresh or probe):
            return None
        return outside

    def _partner_swaps(
        self, near: Mapping[str, list[Any]], tags: Mapping[str, Any]
    ) -> dict[str, list[Any]]:
        """Values for each tag compared with a swapped one, its own value first.

        A domain covers a comparison of two tags only through a pair of values, so the
        partner of a swapped tag may need to move as well.  So may each tag compared with
        that partner.
        """
        out: dict[str, list[Any]] = {}
        for name in near:
            for partner in sorted(self.groups.get(name, ())):
                if partner in near or partner in out:
                    continue
                value = _value(tags, partner, self.facts)
                reach = self._reach(partner)
                others = _nearest(value, self.domains[partner][1], reach)
                out[partner] = [value, *[v for v in others if not _same(v, value)][:reach]]
        return out

    def _reach(self, name: str) -> int:
        """How many values on each side of its own a large domain offers *name*.

        A chain of tags all past the same end of their domains needs a value for each.
        """
        return max(2, len(self.groups.get(name, ())))

    # -- judging the forks after the scan -----------------------------------

    def judge(self, plc: PLC, forks: list[_Fork], judge: Judge, scan: int) -> None:
        """Step the forks and compare each with *plc*, which has run the same scan."""
        if any(f.why != "domain" for f in forks):
            judge.tally.probes += 1
        for fork in forks:
            if fork.why == "domain":
                self._judge_domain(plc, fork, judge, scan)
                continue
            before = fork.plc.fork(inherit_log=False) if fork.why == "scan-local" else None
            fork.plc.step()
            if fork.why == "absorbed value" and not self._same_sides(fork.plc, plc):
                judge.tally.note("absorbed value crossed a threshold, not judged")
                continue
            differs = _differences(fork.plc, plc, fork.leave_out)
            if fork.why == "none":
                assert not differs, f"a fork with nothing changed differs: {differs[:5]}"
                continue
            judge.tally.note(fork.why)
            if differs:
                culprits = self._culprits(before, fork, plc) if before is not None else []
                detail = f"changing {_short(fork.change)} at the start changed {differs[:6]}"
                if culprits:
                    detail += f", and {culprits} each do it alone"
                judge.fail(fork.why, scan, detail, ", ".join(culprits or sorted(fork.change)))

    def _same_sides(self, a: PLC, b: PLC) -> bool:
        facts = self.facts
        ta, tb = a.current_state.tags, b.current_state.tags
        return all(
            band.sides(_value(ta, acc, facts), ta, facts)
            == band.sides(_value(tb, acc, facts), tb, facts)
            for acc, band in self.bands.items()
        )

    def _judge_domain(self, plc: PLC, fork: _Fork, judge: Judge, scan: int) -> None:
        """Pass if some swap of domain values gives what *plc* shows, else blame each tag.

        A swap must answer every comparison as the run's state does at the scan's start,
        and show the same at its end.  A swap whose own comparisons answer otherwise at the
        end crossed a value the domain need not split at, so it judges nothing.  One such
        swap leaves the whole judgement out, though another swap may differ at the end.  A
        tag is to blame when swapping all the others, with it left as it is, does pass.
        """
        assert fork.base is not None
        facts = self.facts
        entry = {**fork.base.current_state.tags, **fork.patch}
        want_start = _start_answers(entry, facts)
        want = _seen_by_program(plc, facts, fork.leave_out)
        swapped = set(fork.near)
        want_own = _start_answers(plc.current_state.tags, facts, about=swapped)
        closest: list[str] = []
        crossed = False

        def passes(near: Mapping[str, list[Any]]) -> bool:
            nonlocal closest, crossed
            limit = _MAX_SWAPS
            if any(self._reach(name) > 2 for name in near):
                limit = _MAX_CHAIN_SWAPS
            for combo in _combos(near, limit):
                mismatch = _mismatch(_start_answers({**entry, **combo}, facts), want_start)
                if not mismatch:
                    alt = _forked(fork.base, {**fork.patch, **combo})
                    alt.step()
                    own = _start_answers(alt.current_state.tags, facts, about=swapped)
                    if own != want_own:
                        crossed = True
                        continue
                    mismatch = _mismatch(_seen_by_program(alt, facts, fork.leave_out), want)
                    if not mismatch:
                        return True
                    mismatch = [f"at the end, {m}" for m in mismatch]
                if not closest or len(mismatch) < len(closest):
                    closest = mismatch
            return False

        for name in fork.change:
            judge.tally.note(f"{self.domains[name][0]}, swapped")
        if passes(fork.near):
            return
        if crossed:
            judge.tally.note("domain swap crossed a comparison, not judged")
            return
        blamed = list(fork.change)
        if len(fork.change) > 1:
            blamed = [
                name
                for name in fork.change
                if passes({n: v for n, v in fork.near.items() if n != name})
            ] or blamed
        for name in blamed:
            kind, members = self.domains[name]
            value = fork.change[name]
            detail = (
                f"{name} = {value!r} at the start behaves like no value of {tuple(members)!r}"
                f" (the nearest differ in {closest[:4]})"
            )
            judge.fail(kind, scan, detail, name)

    def _culprits(self, before: PLC, fork: _Fork, after: PLC) -> list[str]:
        """The changed tags that alone change something outside the changed set."""
        if len(fork.change) == 1:
            return sorted(fork.change)
        found = []
        for name, value in sorted(fork.change.items()):
            alone = before.fork(inherit_log=False)
            alone.patch({**fork.patch, name: value})
            alone.step()
            if _differences(alone, after, fork.leave_out):
                found.append(name)
        return found


def _combos(near: Mapping[str, list[Any]], limit: int = _MAX_SWAPS) -> list[dict[str, Any]]:
    """Joint swaps of every tag in *near*, the nearest values first, at most *limit*."""
    names = list(near)
    values = itertools.product(*(near[n] for n in names))
    return [dict(zip(names, each, strict=True)) for each in itertools.islice(values, limit)]


def _forked(base: PLC, patch: Mapping[str, Any]) -> PLC:
    fork = base.fork(inherit_log=False)
    fork.patch(dict(patch))
    return fork


def _short(change: Mapping[str, Any]) -> str:
    items = sorted(change.items())
    text = ", ".join(f"{k}={v!r}" for k, v in items[:6])
    return text + (f" and {len(items) - 6} more" if len(items) > 6 else "")


# ---------------------------------------------------------------------------
# Declared writes: what each instruction writes in a run
# ---------------------------------------------------------------------------


class WriteRecorder:
    """Records the tags each instruction writes while it runs.

    Each tag goes to the innermost instruction running when it is written.  So a write in a
    forloop body counts for the body's instruction, and the loop's index for the loop.
    """

    #: The executor runs a For box's loop itself and never calls its ``execute()``.  So the
    #: recorder wraps these executor functions as well, or it would miss the loop's writes.
    _LOOP_RUNNERS = ("_execute_for_loop_natural", "_execute_for_loop_instruction")

    def __init__(self) -> None:
        #: Each instruction that wrote, by ``id``, with the tags it wrote and the first scan.
        self.writes: dict[int, tuple[Any, dict[str, int]]] = {}
        self.scan = 0
        self._inner: list[set[str]] = []

    @contextmanager
    def watching(self, program: Any) -> Any:
        """Record every instruction of *program* until the block ends."""
        from pyrung.core import executor
        from pyrung.core.validation._common import walk_instructions

        wrapped = []
        for instr in walk_instructions(program):
            if "execute" not in vars(instr):
                run = instr.execute
                instr.execute = self._wrap(instr, run)
                wrapped.append(instr)
        loops = {name: getattr(executor, name) for name in self._LOOP_RUNNERS}
        for name, real in loops.items():
            setattr(executor, name, self._wrap_loop(real))
        try:
            yield self
        finally:
            for name, real in loops.items():
                setattr(executor, name, real)
            for instr in wrapped:
                del instr.execute

    def _wrap(self, instr: Any, run: Callable[..., None]) -> Callable[..., None]:
        def execute(ctx: Any, enabled: bool) -> None:
            self._record(instr, ctx, lambda: run(ctx, enabled))

        return execute

    def _wrap_loop(self, run: Callable[..., None]) -> Callable[..., None]:
        def loop(program: Any, ctx: Any, index: int, rung: Any, instr: Any, *rest: Any, **kw: Any):
            self._record(instr, ctx, lambda: run(program, ctx, index, rung, instr, *rest, **kw))

        return loop

    def _record(self, instr: Any, ctx: Any, run: Callable[[], None]) -> None:
        journal = ctx._begin_capture()
        self._inner.append(set())
        try:
            run()
        finally:
            assert ctx._capture_stack.pop() is journal
            inner = self._inner.pop()
            if self._inner:
                self._inner[-1].update(journal)
            own = set(journal) - inner
            if own:
                seen = self.writes.setdefault(id(instr), (instr, {}))[1]
                for name in own:
                    seen.setdefault(name, self.scan)


def declared_writes(
    instr: Any, tags: Mapping[str, Any], reach: Mapping[int, tuple[str, ...]]
) -> Callable[[str], bool]:
    """Whether the facts list a tag as one *instr* writes.

    The list holds the tags ``absorb._all_write_targets`` gives the facts, the names
    ``write_sites`` resolves and the fault flags *instr* can set.  The facts expand a pointer
    write into the tags its pointer's bounds reach.  A pointer write they can't expand (a
    block past 1000 elements) covers every tag of its block.
    """
    from pyrung.core.analysis.prove.absorb import _all_write_targets
    from pyrung.core.analysis.prove.classify import _expand_indirect_tag_names
    from pyrung.core.memory_block import IndirectBlockRange, IndirectExprRef, IndirectRef
    from pyrung.core.tag import ImmediateRef

    names = set(instruction_fault_writes(instr))
    names.update(name for name, _kind in _all_write_targets(instr, reach))
    blocks: set[str] = set()
    for target in instruction_write_targets(instr):
        names |= static_write_target_names(target)
        if isinstance(target, ImmediateRef):
            target = target.value
        indirect = (IndirectRef, IndirectExprRef, IndirectBlockRange)
        if isinstance(target, indirect) and not _expand_indirect_tag_names(target, reach):
            blocks.add(target.block.name)

    def covers(name: str) -> bool:
        block = getattr(tags.get(name), "_pyrung_block", None)
        return name in names or getattr(block, "name", None) in blocks

    return covers


def writer_classes(program: Any) -> set[str]:
    """The instruction classes of *program* that have writes in ``write_sites``."""
    from pyrung.core.validation._common import walk_instructions

    return {
        type(instr).__name__
        for instr in walk_instructions(program)
        if instruction_write_targets(instr) or instruction_fault_writes(instr)
    }


def _judge_declared_writes(
    judge: Judge, recorder: WriteRecorder, tags: Mapping[str, Any], program: Any
) -> list[str]:
    """Fail each instruction that wrote a tag the facts do not list for it.

    Returns each instruction class that has writes in ``write_sites`` and that no run saw
    write.  A class the recorder can't see shows up there, not as a pass.
    """
    reach = judge.facts.pdg.indirect_reach
    for instr, written in recorder.writes.values():
        covers = declared_writes(instr, tags, reach)
        judge.tally.note("declared writes")
        missing = sorted((scan, name) for name, scan in written.items() if not covers(name))
        if missing:
            kind = type(instr).__name__
            names = ", ".join(name for _scan, name in missing[:6])
            detail = f"a {kind} wrote {names}, and the facts do not list them"
            judge.fail("declared writes", missing[0][0], detail, f"{kind} {missing[0][1]}")
    seen = {type(instr).__name__ for instr, _written in recorder.writes.values()}
    return sorted(writer_classes(program) - seen)


# ---------------------------------------------------------------------------
# Running a program
# ---------------------------------------------------------------------------


@dataclass
class Report:
    """The result of checking one program."""

    label: str
    violations: list[Violation]
    tally: Tally
    intractable: str | None = None
    unmoved: list[str] = field(default_factory=list)
    #: Kinds of fact the program has that no scan judged, and Dones that never turned
    #: on (``"<Done> on"``) or off.  A fact the runs never reach passes unchecked.
    unjudged: list[str] = field(default_factory=list)
    #: Instruction classes with writes in ``write_sites`` that no run saw write.
    silent_writers: list[str] = field(default_factory=list)

    def problems(self) -> list[str]:
        return [str(v) for v in self.violations]


#: The tally keys that judge each kind of fact whose own name is no tally key.  A key that
#: says "not judged" never counts, though it starts with the kind's name.
_JUDGED_BY = {
    "input domain": ("input domain, swapped", "input domain, value in it"),
    "stateful domain": ("stateful domain, swapped", "stateful domain, value in it"),
}


def _unjudged(judge: Judge, prober: Prober | None) -> list[str]:
    """What :attr:`Report.unjudged` lists (see there)."""
    facts = judge.facts
    has = {
        "produced domain": bool(facts.produced),
        "functional dep": bool(judge.projected),
        "transient rest": bool(facts.transient_rests),
        "init constant": bool(facts.init_constant_reps),
        "reference constant": bool(facts.reference_constants),
        "producers": bool(set(facts.pdg.writers_of) - facts.proofs.free),
        "done spec": bool(judge.done),
        "exclusion": len(judge.bools) > 1,
        "linked block": bool(facts.proofs.couplings),
        "declared writes": bool(facts.pdg.writers_of),
    }
    if prober is not None:
        kinds = {kind for kind, _members in prober.domains.values()}
        has["input domain"] = "input domain" in kinds
        has["stateful domain"] = "stateful domain" in kinds
        has["scan-local"] = bool(prober.scan_local)
        has["absorbed value"] = bool(prober.bands or prober.thresholds)
    judged = judge.tally.judged
    out = [
        kind
        for kind, present in has.items()
        if present and not any(judged.get(key) for key in _JUDGED_BY.get(kind, (kind,)))
    ]
    for done in sorted(judge.done):
        out += [
            f"{done} {word}"
            for word, now in (("on", True), ("off", False))
            if (done, now) not in judge.flips
        ]
    return out


Step = Callable[[Mapping[str, Any]], None]


def check_program(
    label: str,
    build: Callable[[], Any],
    *,
    seeds: Iterable[int] = (1, 2, 3),
    scans: int = 600,
    prepare: Callable[[PLC], None] | None = None,
    driver: Callable[[PLC, Step, RandomInputs], None] | None = None,
) -> Report:
    """Compute the facts of ``build()``, run it, and judge every fact at every scan.

    *prepare* runs on each new PLC before anything else, to install a harness.  *driver*
    replaces the plain random walk.  It gets the PLC, a function that runs one scan with a
    patch, and the random walk.
    """
    program = build()
    plc = _plc(program)
    if prepare is not None:
        prepare(plc)
    facts = compute_facts(program, plc)
    if isinstance(facts, Intractable):
        return Report(label, [], Tally(), intractable=str(facts))
    judge = Judge(label, facts)
    driven = driven_tags(facts, plc)
    values = {name: candidate_values(name, facts, plc) for name in driven}
    # A randint draw differs between a fork and its parent, so a probe would see noise.
    probing = not randint_sites(facts.pdg.tags)
    hold = hold_scans(program)
    aimed = aims(program, values)
    every = max(1, scans // PROBES_PER_RUN)
    prober: Prober | None = None
    recorder = WriteRecorder()
    with recorder.watching(program):
        for seed in seeds:
            run = _plc(program)
            if prepare is not None:
                prepare(run)
            judge.tally.runs += 1
            judge.power_on(run.current_state.tags)
            prober = Prober(facts, driven, seed)
            walk = RandomInputs(values, seed, hold, aimed)
            count = 0

            def one_scan(patch: Mapping[str, Any], run: PLC = run, prober: Prober = prober) -> None:
                nonlocal count
                count += 1
                recorder.scan = count
                before = run.current_state.tags
                # The power-on state is no scan's result, so the domains start at scan 2's start.
                forks = (
                    prober.forks(run, patch, count % every == 0) if probing and count > 1 else []
                )
                if patch:
                    run.patch(dict(patch))
                run.step()
                judge.tally.scans += 1
                judge.see(count, run.current_state.tags, before)
                if forks:
                    prober.judge(run, forks, judge, count)

            if driver is not None:
                driver(run, one_scan, walk)
            else:
                for _ in range(scans):
                    one_scan(walk.next())
            for kind, members in prober.tally_members.items():
                judge.tally.note(kind, members)
    silent = _judge_declared_writes(judge, recorder, plc.tags, program)
    judge.finish()
    unjudged = _unjudged(judge, prober if probing else None)
    return Report(label, judge.violations, judge.tally, None, judge.unmoved, unjudged, silent)
