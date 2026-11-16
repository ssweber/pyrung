"""Check the static facts how() reads against real runs of the program.

The prover's search used to be the only check of these facts.  This module checks them
against plain stepping instead.  It computes a program's facts, drives the program with
chosen inputs, and judges every fact at every scan of every run.  A fact that a scan
contradicts is a soundness bug in the fact, not in the run.

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
  the nearest values of its domain, runs the scan on a fork, and compares what the program
  can see.  That is every Bool, every comparison in a rung condition and every memory key.
- **Produced value domains** (``produced_value_domains``).  These list every value exactly,
  so each value a tag holds is one of them.
- **Scan-local tags.**  These are the written tags outside the world key
  (``stateful_names``), the elided tags and the absorbed values below excepted.  Their value
  at the start of a scan reaches no other tag and no memory by its end.  A probe changes
  them all on a fork and compares the two results.
- **Absorbed values** (``state_key_done_specs``, ``threshold_vector_specs``).  The world key
  keeps which side of each threshold a timer, counter or progress value is on, not the
  value itself.  A probe moves the value, or a threshold tag, without crossing a threshold.
  Then nothing else changes.  Each Done also turns on or off only when its Acc is on the
  matching side of a literal preset.
- **Projections.**  A tag elided as ``functional_dep`` equals ``offset + scale * source``
  after every scan.  An init constant has one value for each value of its representative.
- **Stepping tags.**  Each one takes at least two values over the runs.
- **CANNOT_REACH facts** (``static_proofs``).  A tag only becomes a value that some rung in
  ``ProofFacts.producers`` writes.  Two values that ``exclusion_evidence`` keeps apart never
  hold together.  A value that ``linked_feedback_block`` says is blocked never appears.
- **Program facts** (``program_facts``).  A reference constant never changes.  A tag that
  ``scan_transient_rest`` says rests at one value holds it at the end of every scan.

How the inputs are chosen.  The run drives each nondeterministic or steerable tag that the
outside can write.  That is a tag marked ``external``, a physical input, or a tag the
program writes too.  A tag the program never writes and that is not external is a
constant, so the run leaves it alone.  Each driven tag draws from its declared ``choices``
or ``min``/``max``, every value of its input domain and those values plus and minus one.
It also draws every literal the program compares it with, plus and minus one.  An integer
without declared bounds also gets 0 and its type's extremes.  A seeded random walk changes
the inputs in stretches.  A stretch chatters (each input changes on half the scans), changes
slowly or holds still.  Chatter gives every edge combination.  A hold outlasts the longest
literal timer preset, so each timer and counter reaches Done.  The same seed gives the same
run every time.
"""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
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
from pyrung.core.expression import randint_sites
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.core.tag import TagType

#: The scan time ``_build_prior_facts`` assumes, and every run uses.
DT = 0.010

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


@dataclass(frozen=True)
class Violation:
    """One fact a scan contradicts."""

    fact: str
    program: str
    scan: int
    detail: str

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


def compute_facts(program: Any, plc: PLC) -> Facts | Intractable:
    """The facts of *program*, from the entry points the module docstring names."""
    compiled = compile_kernel(program, blockless=True, proof_metadata=True)
    ctx = prove_passes._PassContext(
        program=program,
        scope=None,
        project=None,
        extra_exprs=None,
        dt=DT,
        compiled=compiled,
        domains_only=True,
    )
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


def _nearest(value: Any, members: list[Any]) -> list[Any]:
    """The members to swap for *value*, the nearest first.

    A small domain gives all its members.  A large one gives the two nearest on each side.
    """
    if not (_is_number(value) and all(_is_number(m) for m in members)):
        return members[:8]
    ordered = sorted(members, key=lambda m: abs(m - value))
    if len(members) <= 8:
        return ordered
    below = [m for m in ordered if m < value][:2]
    above = [m for m in ordered if m > value][:2]
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
        outside = tag.external or facts.pdg.is_physical_input(name)
        if not outside and not facts.pdg.writers_of.get(name):
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
    elif tag.type is TagType.CHAR:
        values = [v for v in values if isinstance(v, str) and len(v) <= 1] + ["A", "0"]
    unique: list[Any] = []
    for value in values:
        if not any(_same(value, u) for u in unique):
            unique.append(value)
    return unique


def hold_scans(program: Any) -> int:
    """Scans a hold lasts: past the longest literal timer preset, at most 400."""
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.validation._common import walk_instructions

    longest = 0
    for instr in walk_instructions(program):
        if isinstance(instr, (OnDelayInstruction, OffDelayInstruction)) and isinstance(
            instr.preset, int
        ):
            longest = max(longest, math.ceil(instr.preset * instr.unit.ns / (DT * 1e9)))
    return min(400, max(30, longest + 10))


class RandomInputs:
    """A seeded walk over each driven tag's candidate values, in stretches."""

    def __init__(self, values: Mapping[str, list[Any]], seed: int, hold: int) -> None:
        self.values = values
        self.random = random.Random(seed)
        self.hold = hold
        self.left = 0
        self.rate = 0.0
        self.current: dict[str, Any] = {}

    def next(self) -> dict[str, Any]:
        if self.left == 0:
            mode = self.random.choice(("chatter", "slow", "hold"))
            self.rate = {"chatter": 0.5, "slow": 0.05, "hold": 0.0}[mode]
            self.left = self.hold if mode == "hold" else self.random.randint(10, 80)
        self.left -= 1
        changed = {}
        for name, choices in self.values.items():
            if name not in self.current or self.random.random() < self.rate:
                value = self.random.choice(choices)
                if name not in self.current or not _same(value, self.current[name]):
                    changed[name] = value
                self.current[name] = value
        return changed


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

    def fail(self, fact: str, scan: int, detail: str, key: str) -> None:
        """Record a violation, once for each fact and key."""
        mark = f"{fact}|{key}"
        if mark not in self._marks:
            self._marks.add(mark)
            self.violations.append(Violation(fact, self.label, scan, detail))

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
        for done, (kind, acc, preset) in self.done.items():
            was = bool(_value(before, done, self.facts))
            now = bool(_value(tags, done, self.facts))
            if was == now:
                continue
            self.tally.note("done spec")
            value = _value(tags, acc, self.facts)
            reached = value >= preset
            # An on-delay or up counter turns Done on at its preset and off below it.  An
            # off-delay turns Done off once its Acc reaches the preset.
            ok = reached == now if kind in ("on_delay", "count_up") else now or reached
            if not ok:
                detail = f"{done} went {was} -> {now} with {acc} = {value!r}, preset {preset}"
                self.fail("done spec", scan, detail, done)

    def finish(self) -> None:
        """Judge what only all the runs together answer."""
        for name, seen in self.stepping_values.items():
            self.tally.note("stepping")
            if len(seen) < 2:
                detail = f"{name} only ever held {seen!r}"
                self.fail("stepping", self.tally.scans, detail, name)
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


def _done_relations(facts: Facts) -> dict[str, tuple[str, str, int]]:
    """Each world-key Done with a literal preset above 0, whose Acc a run can judge it by."""
    names = facts.prior.stateful_names
    found = {}
    for spec in facts.prior.state_key_done_specs:
        done = names[spec.index]
        preset = facts.done_presets.get(done)
        if spec.kind in ("on_delay", "count_up", "off_delay") and preset is not None and preset > 0:
            found[done] = (spec.kind, spec.acc_name, preset)
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
    """What the program can see of *plc*'s state: Bools, comparison answers and memory."""
    state = plc.current_state
    seen: dict[str, Any] = {}
    for name, value in state.tags.items():
        if isinstance(value, bool) and name not in leave_out:
            seen[name] = value
    for atom in facts.atoms:
        if atom.form not in ("rise", "fall"):
            seen[repr(atom._key())] = _answer(atom, state.tags, facts)
    for key, value in state.memory.items():
        if key not in leave_out:
            seen[f"memory {key}"] = _hashable(value)
    return seen


def _mismatch(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    return sorted(
        f"{k}: {a.get(k)!r} vs {b.get(k)!r}" for k in set(a) | set(b) if not _same(a.get(k), b.get(k))
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
    """The values an absorbed value is compared with: numbers, or tags read at the time."""

    bounds: list[Any]
    margin: int

    def sides(self, value: Any, tags: Mapping[str, Any], facts: Facts, margin: int) -> Any:
        """Which side of each bound *value* is on, or ``None`` within *margin* of one."""
        out = []
        for bound in self.bounds:
            at = _value(tags, bound, facts) if isinstance(bound, str) else bound
            if not _is_number(at) or not _is_number(value) or abs(value - at) <= margin:
                return None
            out.append(value > at)
        return tuple(out)


def _bands(facts: Facts) -> dict[str, _Band]:
    """The band of every absorbed value, by its name."""
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.validation._common import walk_instructions

    timers = (OnDelayInstruction, OffDelayInstruction)
    steps: dict[str, int] = {}
    presets: dict[str, Any] = {}
    for instr in walk_instructions(facts.program):
        if isinstance(instr, timers):
            steps[instr.accumulator.name] = math.ceil(DT * 1e9 / instr.unit.ns)
        if isinstance(instr, (*timers, CountUpInstruction, CountDownInstruction)):
            preset = instr.preset
            presets[instr.accumulator.name] = preset if _is_number(preset) else preset.name
    prior = facts.prior
    bands: dict[str, _Band] = {}

    def band(acc: str) -> _Band:
        if acc not in bands:
            bands[acc] = _Band([0], steps.get(acc, 1) + 1)
            if acc in presets:
                bands[acc].bounds.append(presets[acc])
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
    alternatives: list[PLC] = field(default_factory=list)


class Prober:
    """Runs a scan again on forks with changed starting values, and compares the results."""

    def __init__(self, facts: Facts, driven: Iterable[str], seed: int) -> None:
        self.facts = facts
        self.random = random.Random(seed)
        skip = set(driven)
        self.scan_local = [n for n in scan_local_tags(facts) if n not in skip]
        self.thresholds = [n for n in threshold_tags(facts) if n not in skip]
        self.bands = _bands(facts)
        prior = facts.prior
        self.domains = {
            **{n: ("input domain", _members(d)) for n, d in prior.nondeterministic_dims.items()},
            **{n: ("stateful domain", _members(d)) for n, d in prior.stateful_dims.items()},
        }
        self.judged_cells: set[tuple[str, Any]] = set()

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
            for name, moved in self._band_moves(tags):
                out.append(self._fork(plc, patch, "absorbed value", {name: moved}))
        swaps = self._domain_swaps(tags, probe)
        if swaps is not None:
            change, combos = swaps
            fork = _Fork("domain", change, plc, set(change) | {f"_frac:{n}" for n in change})
            for combo in combos:
                alt = plc.fork(inherit_log=False)
                alt.patch({**patch, **combo})
                fork.alternatives.append(alt)
            out.append(fork)
        return out

    def _fork(self, plc: PLC, patch: Mapping[str, Any], why: str, change: dict[str, Any]) -> _Fork:
        fork = plc.fork(inherit_log=False)
        fork.patch({**patch, **change})
        leave_out = set(change) | {f"_frac:{n}" for n in change}
        return _Fork(why, change, fork, leave_out)

    def _other(self, name: str, value: Any) -> Any:
        """A value of *name*'s type other than *value*, often one the program compares it with."""
        tag = self.facts.pdg.tags.get(name)
        kind = tag.type if tag is not None else None
        if kind is TagType.BOOL or isinstance(value, bool):
            return not value
        if kind is TagType.CHAR or isinstance(value, str):
            return "Z" if value != "Z" else "A"
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
            want = band.sides(value, tags, facts, band.margin)
            if want is None:
                continue
            for _ in range(8):
                moved = self._step(value, band.margin)
                if band.sides(moved, tags, facts, band.margin) == want:
                    moves.append((acc, moved))
                    break
        for name in self.thresholds:
            value = _value(tags, name, facts)
            if not _is_number(value):
                continue
            related = [(acc, b) for acc, b in self.bands.items() if name in b.bounds]
            want = [b.sides(_value(tags, acc, facts), tags, facts, b.margin) for acc, b in related]
            if not related or any(w is None for w in want):
                continue
            for _ in range(8):
                moved = self._step(value, 2)
                moved_tags = {**tags, name: moved}
                now = [
                    b.sides(_value(tags, acc, facts), moved_tags, facts, b.margin)
                    for acc, b in related
                ]
                if now == want:
                    moves.append((name, moved))
                    break
        return moves

    def _step(self, value: Any, margin: int) -> Any:
        size = self.random.choice((1, 2, 3, 5, 10, 50, 200)) * margin
        step = size * self.random.choice((1, -1))
        if isinstance(value, int):
            return int(max(-32768, min(32767, value + step)))
        return value + step

    def _domain_swaps(
        self, tags: Mapping[str, Any], probe: bool
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """The values outside their domains at a scan's start, and the swaps to try for them.

        Each gap between domain values is judged once per run, and again at probe scans.
        """
        outside: dict[str, list[Any]] = {}
        fresh = False
        for name, (_kind, members) in self.domains.items():
            value = _value(tags, name, self.facts)
            if not members or any(_same(value, m) for m in members):
                continue
            near = _nearest(value, members)
            outside[name] = near
            cell = (name, tuple(_hashable(v) for v in near))
            if cell not in self.judged_cells:
                self.judged_cells.add(cell)
                fresh = True
        if not outside or not (fresh or probe):
            return None
        change = {name: _value(tags, name, self.facts) for name in outside}
        combos = [
            dict(zip(outside, values, strict=True))
            for values in itertools.islice(itertools.product(*outside.values()), _MAX_SWAPS)
        ]
        return change, combos

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
                judge.fail(fork.why, scan, detail, repr(culprits or sorted(fork.change)))

    def _same_sides(self, a: PLC, b: PLC) -> bool:
        facts = self.facts
        ta, tb = a.current_state.tags, b.current_state.tags
        return all(
            band.sides(_value(ta, acc, facts), ta, facts, 0)
            == band.sides(_value(tb, acc, facts), tb, facts, 0)
            for acc, band in self.bands.items()
        )

    def _judge_domain(self, plc: PLC, fork: _Fork, judge: Judge, scan: int) -> None:
        want = _seen_by_program(plc, self.facts, fork.leave_out)
        closest: list[str] | None = None
        for alt in fork.alternatives:
            alt.step()
            mismatch = _mismatch(_seen_by_program(alt, self.facts, fork.leave_out), want)
            if not mismatch:
                for name in fork.change:
                    judge.tally.note(self.domains[name][0])
                return
            if closest is None or len(mismatch) < len(closest):
                closest = mismatch
        for name, value in fork.change.items():
            kind, members = self.domains[name]
            judge.tally.note(kind)
            detail = (
                f"{name} = {value!r} at the start behaves like no value of {tuple(members)!r}"
                f" (the nearest differ in {(closest or [])[:4]})"
            )
            judge.fail(kind, scan, detail, name)

    def _culprits(self, before: PLC, fork: _Fork, after: PLC) -> list[str]:
        """The changed tags that alone change something outside the changed set."""
        if len(fork.change) == 1:
            return sorted(fork.change)
        found = []
        for name, value in sorted(fork.change.items()):
            alone = before.fork(inherit_log=False)
            alone.patch({name: value})
            alone.step()
            if _differences(alone, after, fork.leave_out):
                found.append(name)
        return found


def _short(change: Mapping[str, Any]) -> str:
    items = sorted(change.items())
    text = ", ".join(f"{k}={v!r}" for k, v in items[:6])
    return text + (f" and {len(items) - 6} more" if len(items) > 6 else "")


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

    def problems(self) -> list[str]:
        return [str(v) for v in self.violations]


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
    plc = PLC(program, dt=DT)
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
    for seed in seeds:
        run = PLC(program, dt=DT)
        if prepare is not None:
            prepare(run)
        judge.tally.runs += 1
        judge.power_on(run.current_state.tags)
        prober = Prober(facts, driven, seed)
        walk = RandomInputs(values, seed, hold)
        every = max(1, scans // PROBES_PER_RUN)
        count = 0

        def one_scan(patch: Mapping[str, Any], run: PLC = run, prober: Prober = prober) -> None:
            nonlocal count
            count += 1
            before = run.current_state.tags
            # The power-on state is no scan's result, so the domains start at scan 2's start.
            forks = prober.forks(run, patch, count % every == 0) if probing and count > 1 else []
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
    judge.finish()
    return Report(label, judge.violations, judge.tally)
