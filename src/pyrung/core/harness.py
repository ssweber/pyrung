"""Autoharness: automatic feedback synthesis from Physical + link= declarations.

Each feedback runs as the one CLICK coil the device behaves like, in a plant rung
scanned before the program.  A bool feedback with ``on_delay``/``off_delay`` is a
``debounce`` coil under its enable, and one with ``flasher_on``/``flasher_off`` is a
``flasher`` coil.  A ``timed`` or ``toggle`` feedback is the coil of that name.  The
ramp and approach kinds are ``math`` rows.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.physical import Physical
from pyrung.core.synthesis import (
    TIMER_ACC_MAX,
    coil_timer,
    plant_acc_name,
    plant_acc_registers,
    plant_timer,
    plant_unit_ms,
)
from pyrung.core.tag import TagType

if TYPE_CHECKING:
    from pyrung.core.runner import PLC


@dataclass(frozen=True)
class Coupling:
    """Public view of one enable→feedback coupling discovered by the harness."""

    en_name: str
    fb_name: str
    physical: Physical
    trigger_value: int | str | None = None


@dataclass
class _BoolCoupling:
    """A bool feedback that runs as a debounce coil under its enable.

    The coil itself lives in the runner's synthesis overlay
    (``plc._synthesis.plant``).  This record keeps the names of the coil's
    accumulators and of its memory of the rung, which seeding and
    :meth:`Harness.pending_ms` read.
    """

    en_name: str
    fb_name: str
    on_delay_ms: int
    off_delay_ms: int
    physical: Physical
    trigger_value: int | str | None = None
    on_acc_name: str = ""
    off_acc_name: str = ""
    rung_memory_key: str = ""


@dataclass
class _ProfileCoupling:
    """A flasher, timed, toggle, ramp or approach feedback, lowered to plant rungs.

    A timed or toggle coil keeps its memory of the rung under ``rung_memory_key``,
    and a timed coil counts in ``acc_name``.  Seeding a settled plant reads both.
    """

    en_name: str
    fb_name: str
    physical: Physical
    trigger_value: int | str | None = None
    rung_memory_key: str = ""
    acc_name: str = ""


#: Raw trigger for ``~Name``: drive while the bit enable is OFF (normally closed).
NC_TRIGGER = "~"


def _parse_link_spec(link: str) -> tuple[str, str | None]:
    """Split a ``link=`` value into the enable name and its raw trigger.

    ``Name`` drives while the enable is on, ``Name:trigger`` while it equals the
    trigger, and ``~Name`` while a bit enable is off.
    """
    if link.startswith(NC_TRIGGER):
        name = link[len(NC_TRIGGER) :]
        if ":" in name:
            raise ValueError(f"Link {link!r} cannot combine ~ with a :trigger.")
        return (name, NC_TRIGGER)
    name, _, trigger = link.partition(":")
    return (name, trigger or None)


def format_link_spec(en_name: str, trigger_value: int | str | None, *, bit: bool) -> str:
    """The canonical ``link=`` value: ``~Name`` for a bit enable that drives while off."""
    if trigger_value is None:
        return en_name
    if bit and trigger_value == 0:
        return f"{NC_TRIGGER}{en_name}"
    return f"{en_name}:{trigger_value}"


def _resolve_trigger_value(trigger_raw: str, en_tag: Any) -> int | str:
    if trigger_raw == NC_TRIGGER:
        if getattr(en_tag, "type", None) != TagType.BOOL:
            raise ValueError(
                f"~ marks a normally-closed bit enable, but {en_tag.name!r} is "
                f"{en_tag.type.value}; write {en_tag.name}:0 to drive while it is 0."
            )
        return 0
    try:
        return int(trigger_raw)
    except ValueError:
        pass
    choices = getattr(en_tag, "choices", None)
    if choices is not None:
        for key, label in choices.items():
            if label == trigger_raw:
                return int(key) if isinstance(key, (int, float)) else key
    if getattr(en_tag, "type", None) == TagType.CHAR:
        return trigger_raw
    if choices is None:
        raise ValueError(
            f"Trigger value {trigger_raw!r} is not an int literal and "
            f"enable tag {en_tag.name!r} has no choices map."
        )
    raise ValueError(
        f"Trigger label {trigger_raw!r} not found in choices for "
        f"{en_tag.name!r}. Available: {list(choices.values())}."
    )


def link_problem(plc: PLC, coupling: Coupling) -> str | None:
    """Why *coupling* cannot run as an override on *plc*, or ``None`` if it can.

    Both ends must be known tags, the feedback must suit the response (bool
    for on/off delay and flasher, numeric for ramp and approach), and nothing
    in the program may write the feedback: plant rungs and logic would fight
    over it every scan.
    """
    known = plc._known_tags_by_name
    fb_tag = known.get(coupling.fb_name)
    if fb_tag is None:
        return f"unknown tag: {coupling.fb_name}"
    if coupling.en_name not in known:
        return f"unknown tag: {coupling.en_name}"
    if coupling.en_name == coupling.fb_name:
        return f"{coupling.fb_name} cannot drive itself"
    if fb_tag.readonly:
        return f"{coupling.fb_name} is readonly"
    if coupling.physical.feedback_type == "bool":
        if fb_tag.type != TagType.BOOL:
            return (
                f"{coupling.fb_name} is {fb_tag.type.value}; "
                "on_delay/off_delay, flasher, timed and toggle drive a bool feedback"
            )
    elif fb_tag.type in (TagType.BOOL, TagType.CHAR):
        return f"{coupling.fb_name} is {fb_tag.type.value}; ramp and approach drive a number"
    writer = _program_writer(plc, coupling.fb_name)
    if writer is not None:
        return f"{coupling.fb_name} is written by the program at {writer}"
    return None


_NS_PER_MS = 1_000_000


CoilWiring = tuple[str, str, int | str | None]
"""A coil's memory key, the enable it reads, and the value of it that counts as on."""


def _wiring(c: _BoolCoupling | _ProfileCoupling) -> CoilWiring:
    return (c.rung_memory_key, c.en_name, c.trigger_value)


def _carry_ns(state: Any, acc_name: str, unit_ns: int) -> Any:
    """A plant timer's carry, in ns as the timer keeps it.  A saved float is of its unit."""
    carry = state.memory.get(f"_frac:{acc_name}", 0) or 0
    return round(carry * unit_ns) if carry.__class__ is float else carry


def _program_writer(plc: PLC, tag_name: str) -> str | None:
    """Location of the first program instruction that writes *tag_name*, if any."""
    from pyrung.core.validation._common import _collect_write_sites, compact_site_location
    from pyrung.core.validation.readonly_write import _any_write_targets

    program = plc._program
    if program is None:
        return None
    for site in _collect_write_sites(program, target_extractor=_any_write_targets):
        if site.target_name == tag_name:
            return compact_site_location(site)
    return None


@dataclass
class Harness:
    """Automatic feedback harness driven by Physical + link= declarations.

    Walks all known tags to find link= couplings and lowers each to plant rungs
    scanned pre-logic (the input-read phase): an ``on_delay``/``off_delay``
    ``Physical`` becomes a ``debounce`` coil, a ``flasher_on``/``flasher_off`` one a
    ``flasher`` coil, a ``timed`` or ``toggle`` one the coil of that name, and
    ``ramp_on``/``approach_to`` ``math`` rows on an analog register.  No Python tick,
    no monitors.

    The couplings found in the program are the *declared* ones.  Overrides
    (:meth:`set_overrides`) replace or cut a declared coupling, or add one,
    keyed by feedback tag, without touching the program; the harness runs
    declared couplings plus overrides.  ``unlink`` is an override that cuts.

    Usage::

        plc = PLC(logic, dt=0.010)
        harness = Harness(plc)
        harness.install()
        plc.run_for(0.5)  # Fb synthesized automatically
    """

    _plc: PLC = field(repr=False)
    _bool_couplings: list[_BoolCoupling] = field(default_factory=list, init=False)
    _profile_couplings: list[_ProfileCoupling] = field(default_factory=list, init=False)
    _declared: list[Coupling] = field(default_factory=list, init=False)
    _overrides: dict[str, Coupling | None] = field(default_factory=dict, init=False)
    _monitors: list[Any] = field(default_factory=list, init=False)
    _installed: bool = field(default=False, init=False)
    _other_unit_registers: tuple[str, ...] | None = field(default=None, init=False)

    def __init__(self, plc: PLC) -> None:
        self._plc = plc
        self._bool_couplings: list[_BoolCoupling] = []
        self._profile_couplings: list[_ProfileCoupling] = []
        self._declared: list[Coupling] = []
        self._overrides: dict[str, Coupling | None] = {}
        self._monitors: list[Any] = []
        self._installed = False
        self._other_unit_registers = None
        self.on_patches_applied: Callable[[list[tuple[str, Any, str]]], None] | None = None

    def install(self) -> None:
        """Put the plant on the runner, a feedback whose enable is on as long since settled."""
        self._install()
        self._settle()

    def _install(self) -> None:
        """Install without settling, so a feedback stays as the state has it.

        Its timers keep counting from there. A caller that puts overrides in afterwards
        settles once they are in, with :meth:`_settle`.
        """
        if self._installed:
            return
        self._installed = True
        self._discover_couplings()
        self._rebuild_couplings()
        self._warn_of_unknown_setpoints()
        self._plc._note_plant_change()
        self._refresh_synthesis()
        self._seed_plant_defaults()
        self._carry_counts_to_current_units()
        self._install_monitors()
        self._plc._harness = self

    def fork_onto(self, plc: PLC, *, as_recorded: bool = False) -> Harness:
        """Create a copy of this harness installed on *plc*.

        The fork inherits the parent's committed state — including each bool
        timer's accumulator registers — so the dwell carries over with no
        re-seed; only the (stateless) timer instructions and the carry are
        copied.  This is the seam the synthesis-overlay install
        grows into.

        A state from before a link changed may hold a count in another unit's register,
        and it moves here unless *as_recorded*: a replay of the scans as they ran keeps
        the state they ran from.
        """
        from copy import copy

        clone = Harness.__new__(Harness)
        clone._plc = plc
        clone._bool_couplings = [copy(c) for c in self._bool_couplings]
        clone._profile_couplings = [copy(c) for c in self._profile_couplings]
        clone._declared = list(self._declared)
        clone._overrides = dict(self._overrides)
        clone._installed = True
        clone._other_unit_registers = self._other_unit_registers
        clone._monitors = []
        clone.on_patches_applied = None
        # Rebuild the synthesis overlay on the fork (fresh, stateless timer
        # instructions reading the fork's tags); the accumulator *state* rides
        # the inherited committed state, so the dwell carries over with no
        # re-seed.  The brackets survive the fork as a program reference — feedback
        # (bool and analog) is entirely plant rungs now, no private callback.
        clone._refresh_synthesis()
        if not as_recorded:
            clone._carry_counts_to_current_units()
        clone._install_monitors()
        plc._harness = clone
        return clone

    def _detach(self) -> None:
        """Off the runner, leaving its plant rungs: they no longer belong to this harness."""
        self._installed = False
        for handle in self._monitors:
            handle.remove()
        self._monitors.clear()
        if self._plc._harness is self:
            self._plc._harness = None

    def uninstall(self) -> None:
        if not self._installed:
            return
        self._detach()
        if self._plc._synthesis is not None:
            self._plc._note_plant_change()
            self._plc._synthesis.plant = []
            self._plc._compiled_replay_kernel = None
            self._plc._soft_exec_program_cache = None
            self._plc._causal_lineage.invalidate_current_epoch()
        self._forget_plant_state()
        self._bool_couplings.clear()
        self._profile_couplings.clear()
        self._declared.clear()
        self._overrides.clear()

    def _forget_plant_state(self) -> None:
        """Clear each coil's memory of its rung, so no later coil reads it.

        An install settles its coils whatever the state holds.  A link added after it
        is not settled.  Were the memory kept, that link would read the old coil's
        memory, and an enable already on might not fire it (see :meth:`set_overrides`).
        The timer counts stay.  An install with the enable on settles them, and with it off
        a running delay runs out.
        """
        from pyrung.core.site_memory import PLANT_FEEDBACK_SITE

        drop = [name for name in self._plc._state.memory if f":{PLANT_FEEDBACK_SITE}" in name]
        if drop:
            self._plc._edit_tip_tags({}, drop_memory=drop)

    def _coil_wiring(self) -> frozenset[CoilWiring]:
        """Each coil this harness runs, for a reload to carry on.

        A coil is its memory key with the enable it reads.  A reload that links a
        feedback to another enable, or to another value of it, settles the coil afresh.
        """
        couplings = [*self._bool_couplings, *self._profile_couplings]
        return frozenset(_wiring(c) for c in couplings if c.rung_memory_key)

    def _settle(self, carry: Collection[CoilWiring] = (), skip: Collection[str] = ()) -> None:
        """Settle each coupling that runs, as long since settled under the enable it reads.

        Every install settles here once, after its overrides are in.  A coupling an override
        replaced is gone by then, so its feedback is seeded for the coil that runs.  A coil
        in *carry* carries on from the state, as a reload passes the coils the old harness
        ran (:meth:`_coil_wiring`).  A feedback in *skip* is not settled either.
        """
        self._seed_bool_state(frozenset(carry), frozenset(skip))

    def unlink(self, tags: list[str]) -> None:
        """Remove couplings for the named feedback tags.

        After unlinking, the Harness no longer synthesizes feedback for
        these tags — they become free inputs the caller can steer directly.
        Models a broken sensor or fault scenario.
        """
        self.set_overrides({**self._overrides, **dict.fromkeys(tags)})

    def declared(self) -> tuple[Coupling, ...]:
        """The couplings the program declares (``physical=`` + ``link=``), before overrides."""
        return tuple(self._declared)

    @property
    def overrides(self) -> Mapping[str, Coupling | None]:
        """Overrides by feedback tag: a coupling to run instead, or ``None`` to cut."""
        return dict(self._overrides)

    def set_overrides(self, overrides: Mapping[str, Coupling | None]) -> None:
        """Replace all overrides and resynthesize.

        Each key is a feedback tag name.  A ``Coupling`` value runs instead of
        any declared coupling on that feedback (or adds one); ``None`` cuts the
        declared coupling, leaving the feedback a free input.  A coupling on a
        feedback no timer has counted for starts cold: under a held enable the feedback
        follows after its on-delay, as freshly wired hardware would.  A coupling
        replaced, or cut and linked again, keeps the time its timers have counted,
        whatever unit the new delays count in.  If its enable turned on while it was
        cut, it counts its on-delay from 0.  A timed or toggle coupling new to its
        feedback takes an enable already on as a rise, so it fires on the next scan.
        """
        for fb_name, coupling in overrides.items():
            if coupling is not None and coupling.fb_name != fb_name:
                raise ValueError(f"Override for {fb_name!r} drives {coupling.fb_name!r}.")
        self._overrides = dict(overrides)
        self._rebuild_couplings()
        if self._installed:
            # Rebuild the overlay and monitors so changed couplings take
            # effect from the next scan (cut ones stop synthesizing).
            self._warn_of_unknown_setpoints()
            self._plc._note_plant_change()
            self._refresh_synthesis()
            self._seed_plant_defaults()
            self._carry_counts_to_current_units()
            for handle in self._monitors:
                handle.remove()
            self._monitors.clear()
            self._install_monitors()

    def _warn_of_unknown_setpoints(self) -> None:
        """Warn when ``approach_to`` names a tag that nothing gives a value.

        It can be meant: an ambient temperature a test patches is in no rung.  A name
        already in the state has its value, whoever wrote it, and is not unknown.
        """
        plc = self._plc
        for c in self._profile_couplings:
            name = c.physical.approach_to
            if not isinstance(name, str):
                continue
            if name in plc._state.tags or plc._tag_known_or_reached(name) is not None:
                continue
            warnings.warn(
                f"{c.fb_name} approaches {name}, which no rung names: "
                "it starts from 0 until something writes it.",
                stacklevel=3,
            )

    def _plant_timers(self) -> Iterator[tuple[str, str, str]]:
        """``(stem, feedback, unit)`` of each plant timer the couplings run."""
        for c in self._bool_couplings:
            unit = coil_timer(c.on_delay_ms, c.off_delay_ms)[2]
            yield "__cpl_on", c.fb_name, unit
            yield "__cpl_off", c.fb_name, unit
        for c in self._profile_couplings:
            if c.physical.kind == "flasher":
                unit = coil_timer(c.physical.flasher_on_ms or 0, c.physical.flasher_off_ms or 0)[2]
                yield "__pls_on", c.fb_name, unit
                yield "__pls_off", c.fb_name, unit
            elif c.physical.kind == "timed":
                yield "__tmd_on", c.fb_name, plant_timer(c.physical.timed_ms or 0)[1]

    def _carry_counts_to_current_units(self) -> None:
        """Move a count sitting in another unit's register to the one its timer uses now.

        A delay staying in its unit finds its count where it is, however the state got
        here: a link replaced, cut and linked again, a reload, a fork from before a
        change.  This makes a change of unit do the same, from the state alone.  A move
        leaves zero behind, so a zero has nothing to give, and a state with its counts in
        place is not touched.  Were two registers to hold a count, the longer time stands.
        """
        state = self._plc._state
        # Every fork comes through here, and almost never has anything to move.
        if self._other_unit_registers is None:
            self._other_unit_registers = tuple(
                name
                for stem, fb_name, unit in self._plant_timers()
                for name, _unit_ms in plant_acc_registers(stem, fb_name)
                if name != plant_acc_name(stem, fb_name, unit)
            )
        if not any(name in state.tags for name in self._other_unit_registers):
            return
        tags: dict[str, Any] = {}
        memory: dict[str, Any] = {}
        for stem, fb_name, unit in self._plant_timers():
            current = plant_acc_name(stem, fb_name, unit)
            counted_ns: dict[str, Any] = {}
            for name, unit_ms in plant_acc_registers(stem, fb_name):
                unit_ns = unit_ms * _NS_PER_MS
                ns = int(state.tags.get(name, 0) or 0) * unit_ns + _carry_ns(state, name, unit_ns)
                if ns != 0:
                    counted_ns[name] = ns
            if not counted_ns.keys() - {current}:
                continue
            for name in counted_ns:
                tags[name] = 0
                memory[f"_frac:{name}"] = 0
            new_count, new_frac = divmod(max(counted_ns.values()), plant_unit_ms(unit) * _NS_PER_MS)
            if new_count > TIMER_ACC_MAX:
                new_count, new_frac = TIMER_ACC_MAX, 0
            tags[current] = new_count
            memory[f"_frac:{current}"] = new_frac
        if tags:
            self._plc._edit_tip_tags(tags, memory=memory)

    def pending(self, fb_name: str) -> bool:
        """True while a bool coupling on *fb_name* is still counting toward a flip."""
        snap = self._plc.current_state.tags
        return any(
            self._bool_mid_transition(c, snap) for c in self._bool_couplings if c.fb_name == fb_name
        )

    def pending_ms(self) -> dict[str, int]:
        """Simulated ms left before each mid-transition bool feedback flips.

        Keyed by feedback name.  A feedback that agrees with its enable is absent.
        On a rise the on side counts.  On a fall the off side counts, and the
        debounce drops the feedback a scan after that count is done.
        """
        state = self._plc.current_state
        snap = state.tags
        left: dict[str, int] = {}
        for c in self._bool_couplings:
            if not self._bool_mid_transition(c, snap):
                continue
            on_preset, off_preset, _unit, unit_ms = coil_timer(c.on_delay_ms, c.off_delay_ms)
            unit_ns = unit_ms * _NS_PER_MS
            # The coil sees an enable change on the next scan.  It counts that side
            # from 0.  The debounce clears its on side at a rise, and its off side is
            # 0 on every true scan.
            seen = bool(state.memory.get(c.rung_memory_key, False)) != bool(
                snap.get(c.fb_name, False)
            )
            if snap.get(c.fb_name, False):
                preset, acc_name, scan_ns = (
                    off_preset,
                    c.off_acc_name,
                    round(self._plc._dt * 1_000 * _NS_PER_MS),
                )
            else:
                preset, acc_name, scan_ns = on_preset, c.on_acc_name, 0
            remaining_ns = preset * unit_ns
            if seen:
                remaining_ns -= int(snap.get(acc_name, 0) or 0) * unit_ns
                remaining_ns -= _carry_ns(state, acc_name, unit_ns)
            left[c.fb_name] = round((max(0, remaining_ns) + scan_ns) / _NS_PER_MS)
        return left

    @property
    def pending_count(self) -> int:
        """How many bool couplings are mid-transition this scan.

        Under the dwell model there is no schedule heap; a bool coupling is
        "pending" while its feedback has not yet reached the value its
        *currently sustained* enable implies — i.e. an on/off-delay timer is
        still counting toward a Fb flip.  PILOT's settle/coast (``run_until``
        ``pending_count == 0``) advances scans until every bool feedback has
        caught up to its held command, exactly as draining the old heap did.
        """
        snap = self._plc.current_state.tags
        return sum(1 for c in self._bool_couplings if self._bool_mid_transition(c, snap))

    def _bool_mid_transition(self, c: _BoolCoupling, snap: Any) -> bool:
        """True while *c*'s feedback disagrees with its sustained enable."""
        en_raw = snap.get(c.en_name, False)
        want = en_raw == c.trigger_value if c.trigger_value is not None else bool(en_raw)
        return bool(snap.get(c.fb_name, False)) != want

    def couplings(self) -> Iterator[Coupling]:
        """Iterate over all discovered couplings (bool and profile)."""
        for c in self._bool_couplings:
            yield Coupling(c.en_name, c.fb_name, c.physical, c.trigger_value)
        for c in self._profile_couplings:
            yield Coupling(c.en_name, c.fb_name, c.physical, c.trigger_value)

    def advance_profiles(self) -> Iterator[Any]:
        """Yield every harness-owned :class:`AdvanceProfile`.

        This is the static *reading* of "En drives Fb toward a threshold" that
        PILOT's accumulator resolver consumes exactly like a timer's profile —
        so ``how(Fb >= threshold)`` learns "hold En, coast N scans" without
        running anything.

        A bool coupling is a debounce coil, and the coil's own profile holds the
        enable at the value wanted.  The coil is not in the user's
        :class:`Program`, so a walk of the program alone cannot find it.  A
        flasher coil has no profile and an analog coupling has no instruction to
        give one, so their profiles are built here.  So are those of a timed or
        toggle coil (see :meth:`_edge_profile`).
        """
        from pyrung.core.instruction.out_variants import TimedInstruction, ToggleInstruction

        synthesis = self._plc._synthesis
        if synthesis is not None:
            for rung in synthesis.plant:
                for instruction in rung._instructions:
                    if isinstance(instruction, (TimedInstruction, ToggleInstruction)):
                        continue
                    profile = instruction.advance_profile()
                    if profile is not None:
                        yield profile
        for c in self._profile_couplings:
            kind = c.physical.kind
            if kind in ("timed", "toggle"):
                profile = self._edge_profile(c)
            elif kind == "flasher":
                profile = self._pulse_profile(c)
            else:
                profile = self._analog_profile(c)
            if profile is not None:
                yield profile

    def _analog_rate_per_scan(
        self, c: _ProfileCoupling
    ) -> tuple[Callable[[float], float], int] | None:
        """The ``(rate_per_scan, direction)`` an analog coupling advances at while enabled.

        A ramp gives the exact declared ``ramp_on`` rate (analytic).  An approach is
        first-order — its slope depends on the current value — so ``rate_per_scan``
        raises and the resolver falls to the empirical (fork-and-run) tier.  ``None``
        when there is no usable static read (a zero-rate ramp).
        """
        physical = c.physical
        if physical.kind == "ramp":
            up = physical.ramp_on or 0.0
            if up == 0.0:
                return None
            per_second = abs(up)
            direction = 1 if up > 0 else -1
            return (lambda step_dt: per_second * step_dt), direction

        if physical.kind != "approach":
            return None  # a bool flasher has no analog accumulator profile

        # Approach: nonlinear, so the analytic rate is undefined — measure it.
        fb_now = float(self._plc.current_state.tags.get(c.fb_name, 0.0))
        toward = physical.approach_to
        assert toward is not None
        target = (
            float(self._plc.current_state.tags.get(toward, 0.0))
            if isinstance(toward, str)
            else float(toward)
        )
        direction = 1 if target >= fb_now else -1

        def rate_per_scan(step_dt: float) -> float:
            raise ValueError("first-order Approach profile; measure empirically")

        return rate_per_scan, direction

    def _pulse_profile(self, c: _ProfileCoupling) -> Any | None:
        """The next edge of a bool pulse train: keep the enable on and it comes.

        Its flasher coil is the plant's and the program has no writer for ``Fb``, so without
        this a trace reads the feedback as a dead end.  No scalar is exposed: the dwells are
        the plant's business.  A dwell of 0 keeps ``Fb`` off, so there is no plan to turn it on.
        """
        from pyrung.core.condition import BitCondition, CompareEq
        from pyrung.core.crossing import Eq
        from pyrung.core.instruction.advance import AdvanceProfile, AdvanceStep, ConditionDemand

        fb_tag = self._plc._known_tags_by_name.get(c.fb_name)
        en_tag = self._plc._known_tags_by_name.get(c.en_name)
        if fb_tag is None or en_tag is None:
            return None
        demand = ConditionDemand(
            CompareEq(en_tag, c.trigger_value)
            if c.trigger_value is not None
            else BitCondition(en_tag)
        )

        spec = c.physical
        on_preset, off_preset, _unit, _ = coil_timer(
            spec.flasher_on_ms or 0, spec.flasher_off_ms or 0
        )
        dark = on_preset <= 0 or off_preset <= 0

        def plan(constraint: Any, snapshot: Any) -> AdvanceStep | None:
            if not isinstance(constraint, Eq) or len(constraint.values) != 1:
                return None
            if dark and bool(next(iter(constraint.values))):
                return None
            return AdvanceStep(until=constraint, holds=(demand,), progress=demand)

        return AdvanceProfile(channels=(fb_tag,), plan=plan)

    def _edge_profile(self, c: _ProfileCoupling) -> Any | None:
        """A timed or toggle feedback changes when its enable rises.

        The coil's own profile always asks for a pulse, so a rung that needs the
        enable on beside the feedback found no plan in it.  Here an enable that is off
        is held on, which gives the rise, as a flasher's enable is held.  An enable
        that is on is pulsed, which takes a fall first.  A timed pulse ends by itself,
        and a maintained one once the enable is held off.  An on time of a scan or
        less gives no pulse, so there is no plan to turn it on.
        """
        from pyrung.core.condition import BitCondition, CompareEq
        from pyrung.core.crossing import Eq
        from pyrung.core.instruction.advance import (
            AdvanceProfile,
            AdvanceStep,
            ConditionDemand,
            constraint_holds,
        )

        fb_tag = self._plc._known_tags_by_name.get(c.fb_name)
        en_tag = self._plc._known_tags_by_name.get(c.en_name)
        if fb_tag is None or en_tag is None:
            return None
        trigger = c.trigger_value
        rung = CompareEq(en_tag, trigger) if trigger is not None else BitCondition(en_tag)
        physical = c.physical
        timed = physical.kind == "timed"
        maintained = bool(physical.maintained)
        if timed:
            preset, _unit, unit_ms = plant_timer(physical.timed_ms or 0)
            dark = preset * unit_ms <= self._plc._dt * 1000
        else:
            dark = False

        def plan(constraint: Any, snapshot: Any) -> AdvanceStep | None:
            if not isinstance(constraint, Eq) or len(constraint.values) != 1:
                return None
            desired = next(iter(constraint.values))
            if not isinstance(desired, bool) or constraint_holds(constraint, snapshot) is True:
                return None
            if timed and not desired:
                if maintained:
                    return AdvanceStep(until=constraint, holds=(ConditionDemand(rung, False),))
                return AdvanceStep(until=constraint)
            if dark:
                return None
            en_raw = snapshot.get(c.en_name)
            en_on = en_raw == trigger if trigger is not None else bool(en_raw)
            if en_on:
                return AdvanceStep(until=constraint, pulse=ConditionDemand(rung))
            demand = ConditionDemand(rung)
            return AdvanceStep(until=constraint, holds=(demand,), progress=demand)

        return AdvanceProfile(channels=(fb_tag,), plan=plan)

    def _analog_profile(self, c: _ProfileCoupling) -> Any | None:
        """Build the continuous :class:`AdvanceProfile` for one analog coupling.

        ``accumulator`` is the Fb register itself (the consumer reads
        ``Fb <cmp> threshold``, matched via the accumulator).  A ramp yields an
        exact analytic ``rate_per_scan``; an approach yields a raising rate, so
        ``scans_until`` returns ``None`` and the resolver measures empirically.
        """
        from pyrung.core.condition import BitCondition, CompareEq
        from pyrung.core.instruction.advance import (
            ConditionDemand,
            monotone_profile,
        )

        fb_tag = self._plc._known_tags_by_name.get(c.fb_name)
        en_tag = self._plc._known_tags_by_name.get(c.en_name)
        if fb_tag is None or en_tag is None:
            return None

        reader = self._analog_rate_per_scan(c)
        if reader is None:
            return None
        rate_per_scan, direction = reader

        advance = (
            CompareEq(en_tag, c.trigger_value)
            if c.trigger_value is not None
            else BitCondition(en_tag)
        )
        demand = ConditionDemand(advance)
        return monotone_profile(
            channels=(fb_tag,),
            accumulator=fb_tag,
            done=None,
            progress=demand,
            preset=0,
            direction=direction,
            rate_per_scan=rate_per_scan,
            advance=demand,
        )

    def _discover_couplings(self) -> None:
        seen_runtimes: set[int] = set()
        reached = self._plc._tags_rungs_reach.declared_with(self._plc._known_tags_by_name)
        for tag in reached.values():
            runtime = getattr(tag, "_pyrung_structure_runtime", None)
            if runtime is None:
                self._try_add_flat_coupling(tag)
                continue
            rt_id = id(runtime)
            if rt_id in seen_runtimes:
                continue
            seen_runtimes.add(rt_id)
            self._discover_structure_couplings(runtime)

    def _try_add_flat_coupling(self, tag: Any) -> None:
        if tag.link is None or tag.physical is None:
            return
        en_name, trigger_raw = _parse_link_spec(tag.link)
        en_tag = self._plc._tag_known_or_reached(en_name)
        if en_tag is None:
            return
        self._plc._register_known_tag(en_tag)
        self._plc._register_known_tag(tag)
        trigger_value = None
        if trigger_raw is not None:
            trigger_value = _resolve_trigger_value(trigger_raw, en_tag)
        self._declared.append(Coupling(en_name, tag.name, tag.physical, trigger_value))

    def _discover_structure_couplings(self, runtime: Any) -> None:
        field_specs = runtime._field_specs
        blocks = runtime._blocks
        count = getattr(runtime, "count", 1)
        for spec in field_specs.values():
            if spec.link is None or spec.physical is None:
                continue
            en_field_name, trigger_raw = _parse_link_spec(spec.link)
            en_block = blocks.get(en_field_name)
            fb_block = blocks.get(spec.name)
            if en_block is None or fb_block is None:
                continue
            for idx in range(1, count + 1):
                try:
                    en_tag = en_block[idx]
                    fb_tag = fb_block[idx]
                except (KeyError, IndexError):
                    continue
                self._plc._register_known_tag(en_tag)
                self._plc._register_known_tag(fb_tag)
                trigger_value = None
                if trigger_raw is not None:
                    trigger_value = _resolve_trigger_value(trigger_raw, en_tag)
                self._declared.append(
                    Coupling(en_tag.name, fb_tag.name, spec.physical, trigger_value)
                )

    def _rebuild_couplings(self) -> None:
        """Lower the declared couplings plus overrides into the working lists."""
        self._other_unit_registers = None
        self._bool_couplings = []
        self._profile_couplings = []
        for c in self._declared:
            if c.fb_name not in self._overrides:
                self._add_coupling(c.en_name, c.fb_name, c.physical, trigger_value=c.trigger_value)
        for c in self._overrides.values():
            if c is not None:
                self._add_coupling(c.en_name, c.fb_name, c.physical, trigger_value=c.trigger_value)

    def _add_coupling(
        self,
        en_name: str,
        fb_name: str,
        physical: Any,
        *,
        trigger_value: int | str | None = None,
    ) -> None:
        # Route by the kind, not the Fb type. A flasher, ramp or approach takes the
        # profile path. An on/off delay is a bool dwell coupling.
        if physical.kind != "delay":
            self._profile_couplings.append(
                _ProfileCoupling(en_name, fb_name, physical, trigger_value=trigger_value)
            )
        else:
            on_ms = physical.on_delay_ms or 0
            off_ms = physical.off_delay_ms or 0
            self._bool_couplings.append(
                _BoolCoupling(
                    en_name, fb_name, on_ms, off_ms, physical, trigger_value=trigger_value
                )
            )

    def _refresh_synthesis(self) -> None:
        """(Re)build the plant rungs into the runner's overlay.

        Each bool coupling is a debounce coil, built by
        :func:`pyrung.core.synthesis.bool_feedback_rungs` and placed in
        ``plc._synthesis.plant``, the rungs the runner scans *before* the user
        program.  So the feedback is an input that reflects the previous scan's
        command.  Presets are the declared delays, in ms unless one is too long
        for it (see :func:`~pyrung.core.synthesis.coil_timer`), so a held enable
        turns the feedback on after ``ceil(on_delay_ms / dt_ms)`` scans
        (``on_delay_ms == 0`` gives the program the feedback the next scan).

        Idempotent — used at install, on each fork (fresh instructions reading the
        fork's tags; accumulator *state* rides the inherited committed state), and
        after overrides change (``set_overrides`` / ``unlink``).  Preserves any ``holds`` PILOT has
        installed on the same overlay.
        """
        from pyrung.core.condition import BitCondition, CompareEq, CompareNe
        from pyrung.core.site_memory import (
            PLANT_FEEDBACK_SITE,
            PLANT_NAMESPACE,
            _first_tag_name,
            bind_sites,
            instruction_memory,
        )
        from pyrung.core.synthesis import (
            Synthesis,
            analog_approach_rung,
            analog_feedback_rungs,
            bool_feedback_rungs,
            pulse_feedback_rungs,
            timed_feedback_rungs,
            toggle_feedback_rungs,
        )
        from pyrung.core.system_points import system
        from pyrung.core.tag import Bool, Int, Real

        plant: list[Any] = []
        coils: list[tuple[_BoolCoupling, Any]] = []
        rung_coils: list[tuple[_ProfileCoupling, Any, str]] = []
        for c in self._bool_couplings:
            en_tag = self._plc._known_tags_by_name.get(c.en_name)
            if en_tag is None:
                continue
            enable = (
                CompareEq(en_tag, c.trigger_value)
                if c.trigger_value is not None
                else BitCondition(en_tag)
            )
            unit = coil_timer(c.on_delay_ms, c.off_delay_ms)[2]
            on_acc = Int(plant_acc_name("__cpl_on", c.fb_name, unit), retentive=False)
            off_acc = Int(plant_acc_name("__cpl_off", c.fb_name, unit), retentive=False)
            fb_tag = self._tag_rungs_know(c.fb_name)
            if fb_tag is None:
                fb_tag = Bool(c.fb_name)
            c.on_acc_name = on_acc.name
            c.off_acc_name = off_acc.name
            rungs = bool_feedback_rungs(
                enable=enable,
                fb_tag=fb_tag,
                on_done=Bool(f"__cpl_ond__{c.fb_name}", retentive=False),
                on_acc=on_acc,
                off_done=Bool(f"__cpl_offd__{c.fb_name}", retentive=False),
                off_acc=off_acc,
                on_delay_ms=c.on_delay_ms,
                off_delay_ms=c.off_delay_ms,
            )
            coils.append((c, rungs[0]._instructions[0]))
            plant.extend(rungs)

        for c in self._profile_couplings:
            spec = c.physical
            en_tag = self._plc._known_tags_by_name.get(c.en_name)
            if en_tag is None:
                continue
            fb_tag = self._tag_rungs_know(c.fb_name)
            if fb_tag is None:
                fb_tag = Bool(c.fb_name) if spec.feedback_type == "bool" else Real(c.fb_name)
            if c.trigger_value is not None:
                enable = CompareEq(en_tag, c.trigger_value)
                disable = CompareNe(en_tag, c.trigger_value)
            else:
                enable = BitCondition(en_tag)
                disable = ~en_tag
            if spec.kind == "ramp":
                armed = Bool(f"__cpl_armed__{c.fb_name}", retentive=False)
                plant.extend(
                    analog_feedback_rungs(
                        enable=enable,
                        disable=disable,
                        fb_tag=fb_tag,
                        armed=armed,
                        dt_tag=system.sys.dt,
                        up=spec.ramp_on or 0.0,
                        down=spec.ramp_off or 0.0,
                    )
                )
            elif spec.kind == "timed":
                timer_acc = Int(
                    plant_acc_name("__tmd_on", c.fb_name, plant_timer(spec.timed_ms or 0)[1]),
                    retentive=False,
                )
                c.acc_name = timer_acc.name
                rungs = timed_feedback_rungs(
                    enable=enable,
                    fb_tag=fb_tag,
                    timer_done=Bool(f"__tmd_ond__{c.fb_name}", retentive=False),
                    timer_acc=timer_acc,
                    on_ms=spec.timed_ms or 0,
                    maintained=bool(spec.maintained),
                )
                rung_coils.append((c, rungs[0]._instructions[0], "_timed_rung"))
                plant.extend(rungs)
            elif spec.kind == "toggle":
                rungs = toggle_feedback_rungs(enable=enable, fb_tag=fb_tag)
                rung_coils.append((c, rungs[0]._instructions[0], "_toggle"))
                plant.extend(rungs)
            elif spec.kind == "approach":  # first-order lag toward a constant/setpoint tag
                toward = spec.approach_to
                toward_operand: Any = toward
                if isinstance(toward, str):
                    toward_operand = self._tag_rungs_know(toward)
                    if toward_operand is None:
                        toward_operand = Real(toward)
                plant.extend(
                    analog_approach_rung(
                        enable=enable,
                        fb_tag=fb_tag,
                        toward=toward_operand,
                        rate=spec.approach_rate or 0.0,
                        dt_tag=system.sys.dt,
                    )
                )
            else:  # flasher, a flasher coil
                on_ms = spec.flasher_on_ms or 0
                off_ms = spec.flasher_off_ms or 0
                unit = coil_timer(on_ms, off_ms)[2]
                plant.extend(
                    pulse_feedback_rungs(
                        enable=enable,
                        fb_tag=fb_tag,
                        on_done=Bool(f"__pls_ond__{c.fb_name}", retentive=False),
                        on_acc=Int(plant_acc_name("__pls_on", c.fb_name, unit), retentive=False),
                        off_done=Bool(f"__pls_offd__{c.fb_name}", retentive=False),
                        off_acc=Int(plant_acc_name("__pls_off", c.fb_name, unit), retentive=False),
                        on_dwell_ms=on_ms,
                        off_dwell_ms=off_ms,
                    )
                )

        if self._plc._synthesis is None:
            self._plc._synthesis = Synthesis()
        # Plant rungs keep their memory under their own ``plant:`` site keys.
        bind_sites(plant, namespace=PLANT_NAMESPACE)
        # An instruction's place in the plant moves when a link before it is added
        # or cut, so each one that keeps memory is keyed by the tag it drives.  Then
        # a fork from before the change, or a reinstall, reads its own memory.
        for rung in plant:
            for instr in rung._instructions:
                if instruction_memory(instr):
                    instr._state_key = f"{PLANT_FEEDBACK_SITE}{_first_tag_name(instr)}"
        for c, coil in coils:
            c.rung_memory_key = coil.memory_key("_debounce_rung")
        for pc, coil, prefix in rung_coils:
            pc.rung_memory_key = coil.memory_key(prefix)
        self._plc._synthesis.plant = plant
        # The overlay changed, so caches that snapshot it are stale: the soft-exec
        # replay kernel + its bracketed compilation unit, rebuilt lazily on next use.
        self._plc._compiled_replay_kernel = None
        self._plc._soft_exec_program_cache = None
        self._plc._causal_lineage.invalidate_current_epoch()

    def _tag_rungs_know(self, name: str) -> Any | None:
        """The program's tag for *name*, a slot only a pointer reaches included.

        A stand-in in its place would seed the slot with the wrong default, and the
        program's own read of the slot would change with it.
        """
        tag = self._plc._tag_known_or_reached(name)
        if tag is not None:
            self._plc._register_known_tag(tag)
        return tag

    def _seed_plant_defaults(self) -> None:
        """Put each tag the plant uses in the state, at its default."""
        current = self._plc._state.tags
        seed = {
            name: default
            for name, default in self._plc._plant_tag_defaults().items()
            if name not in current
        }
        if seed:
            self._plc._edit_tip_tags(seed)

    def _seed_bool_state(
        self, carry: frozenset[CoilWiring], skip: frozenset[str] = frozenset()
    ) -> None:
        """Seed each bool coil whose enable is already on as if it had settled long ago.

        A debounce's feedback is on and its on side has counted its preset.  A timed
        coil's on time is over, so its feedback is on only when maintained.  A toggle
        keeps its feedback.  Each coil saw its rung true on the last scan, so the
        first scan is no rise.  Without this the first scans would show the device off
        while it is commanded on, or a timed or toggle coil would fire at install.

        A coil in *carry* ran before the reload that installs it.  It carries on from
        its memory, so a running pulse or delay runs out.  Whether a key is in the state
        decides nothing, because an absent key reads as its default.  A coupling on a
        feedback in *skip* does not settle.
        """
        seed: dict[str, Any] = {}
        memory: dict[str, Any] = {}
        state = self._plc.current_state
        snap = state.tags

        def settles(c: _BoolCoupling | _ProfileCoupling) -> bool:
            if c.fb_name in skip:
                return False
            if _wiring(c) in carry:
                return False
            en_raw = snap.get(c.en_name, False)
            return en_raw == c.trigger_value if c.trigger_value is not None else bool(en_raw)

        for c in self._bool_couplings:
            if c.on_acc_name and settles(c):
                seed[c.on_acc_name] = coil_timer(c.on_delay_ms, c.off_delay_ms)[0]
                seed[c.fb_name] = True
                memory[c.rung_memory_key] = True
        for pc in self._profile_couplings:
            if not pc.rung_memory_key or not settles(pc):
                continue
            memory[pc.rung_memory_key] = True
            if pc.physical.kind == "timed":
                seed[pc.acc_name] = plant_timer(pc.physical.timed_ms or 0)[0]
                memory[f"_frac:{pc.acc_name}"] = 0
                seed[pc.fb_name] = bool(pc.physical.maintained)
        if seed or memory:
            self._plc._edit_tip_tags(seed, memory=memory)

    def _install_monitors(self) -> None:
        # Analog couplings are declarative specs lowered to plant rungs (their
        # arm/advance is rung state, deterministic under replay), so they need no
        # executor monitor.
        #
        # *Bool* couplings are real timers in the plant overlay; they need no
        # executor monitor.  But the DAP capture/console provenance
        # (``on_patches_applied``) wants each synthesized feedback reported as it
        # changes — a per-Fb monitor fires that notification post-commit (a no-op
        # when no listener is attached, i.e. everywhere but the DAP live console).
        for c in self._bool_couplings:
            handle = self._plc.monitor(c.fb_name, self._make_fb_callback(c.fb_name))
            self._monitors.append(handle)

    def _make_fb_callback(self, fb_name: str) -> Callable[[Any, Any], None]:
        def on_fb_change(current: Any, previous: Any) -> None:  # noqa: ARG001
            if self.on_patches_applied is not None:
                self.on_patches_applied([(fb_name, current, "harness:nominal")])

        return on_fb_change

    def coupling_summary(self) -> dict[str, Any]:
        return {
            "installed": self._installed,
            "bool_couplings": [
                {
                    "en": c.en_name,
                    "fb": c.fb_name,
                    "on_delay_ms": c.on_delay_ms,
                    "off_delay_ms": c.off_delay_ms,
                    "trigger_value": c.trigger_value,
                }
                for c in self._bool_couplings
            ],
            "profile_couplings": [
                {
                    "en": c.en_name,
                    "fb": c.fb_name,
                    "kind": c.physical.kind,
                    "keys": ", ".join(c.physical.tokens()),
                    "trigger_value": c.trigger_value,
                }
                for c in self._profile_couplings
            ],
            "pending_patches": self.pending_count,
        }
