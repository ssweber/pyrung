"""Autoharness: automatic feedback synthesis from Physical + link= declarations.

A bool coupling is **dwell**: feedback responds to a *sustained* command, never
a sub-``on_delay`` glitch.  It is lowered to a real on-delay (``TON``) and
off-delay (``TOF``) timer pair — the same public primitives a hand-written
program would use — executed once per pre-scan as a synthesis overlay.  The
``TON`` rises only after the enable has been held for ``on_delay``; feeding its
done bit through a ``TOF`` keeps the feedback asserted for ``off_delay`` after
the enable drops.  No private transport-delay heap: a glitch resets the
accumulator, so feedback that was never sustained is never fabricated.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.physical import (
    Approach,
    Physical,
    ProfileSpec,
    Pulse,
    Ramp,
    profile_to_token,
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
    """A bool feedback lowered to a real on-delay/off-delay timer pair (dwell).

    The on-delay (``ton``) rises only after the enable has been sustained for
    ``on_delay``; the off-delay (``tof``), driven by the on-delay's done bit,
    keeps the feedback asserted for ``off_delay`` after the enable drops.  The
    feedback tag *is* the off-delay's done bit, so the only register the program
    reads is ``Fb``; ``ton_acc`` / ``tof_acc`` are the timers' internal
    accumulators (excluded from the fold plateau — they churn every scan but are
    unobservable).

    The timer *instructions* themselves live in the runner's synthesis overlay
    (``plc._synthesis.plant``); this record keeps only the metadata and the
    overlay tag names it needs for seeding and pending-count reporting.
    """

    en_name: str
    fb_name: str
    on_delay_ms: int
    off_delay_ms: int
    physical: Physical
    trigger_value: int | str | None = None
    ton_done_name: str = ""
    ton_acc_name: str = ""
    tof_acc_name: str = ""


@dataclass
class _ProfileCoupling:
    en_name: str
    fb_name: str
    # A declarative analog spec (``Ramp`` / ``Approach``), lowered to plant rungs.
    profile: ProfileSpec
    physical: Physical
    trigger_value: int | str | None = None


def _parse_link_spec(link: str) -> tuple[str, str | None]:
    name, _, trigger = link.partition(":")
    return (name, trigger or None)


def _resolve_trigger_value(trigger_raw: str, en_tag: Any) -> int | str:
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


@dataclass
class Harness:
    """Automatic feedback harness driven by Physical + link= declarations.

    Walks all known tags to find link= couplings and lowers each to plant rungs
    scanned pre-logic (the input-read phase): a timing ``Physical`` becomes an
    on-delay/off-delay timer pair (bool dwell), and a declarative profile spec
    becomes calc/timer rungs — ``Ramp``/``Approach`` drive an analog register,
    ``Pulse`` drives a bool pulse train.  No Python tick, no monitors.

    Usage::

        plc = PLC(logic, dt=0.010)
        harness = Harness(plc)
        harness.install()
        plc.run_for(0.5)  # Fb synthesized automatically
    """

    _plc: PLC = field(repr=False)
    _bool_couplings: list[_BoolCoupling] = field(default_factory=list, init=False)
    _profile_couplings: list[_ProfileCoupling] = field(default_factory=list, init=False)
    _monitors: list[Any] = field(default_factory=list, init=False)
    _installed: bool = field(default=False, init=False)

    def __init__(self, plc: PLC) -> None:
        self._plc = plc
        self._bool_couplings: list[_BoolCoupling] = []
        self._profile_couplings: list[_ProfileCoupling] = []
        self._monitors: list[Any] = []
        self._installed = False
        self.on_patches_applied: Callable[[list[tuple[str, Any, str]]], None] | None = None

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        self._discover_couplings()
        self._refresh_synthesis()
        self._seed_bool_state()
        self._install_monitors()
        self._plc._harness = self

    def fork_onto(self, plc: PLC) -> Harness:
        """Create a copy of this harness installed on *plc*.

        The fork inherits the parent's committed state — including each bool
        timer's accumulator registers — so the dwell carries over with no
        re-seed; only the (stateless) timer instructions and the fractional
        remainder are copied.  This is the seam the synthesis-overlay install
        grows into.
        """
        from copy import copy

        clone = Harness.__new__(Harness)
        clone._plc = plc
        clone._bool_couplings = [copy(c) for c in self._bool_couplings]
        clone._profile_couplings = [copy(c) for c in self._profile_couplings]
        clone._installed = True
        clone._monitors = []
        clone.on_patches_applied = None
        # Rebuild the synthesis overlay on the fork (fresh, stateless timer
        # instructions reading the fork's tags); the accumulator *state* rides
        # the inherited committed state, so the dwell carries over with no
        # re-seed.  The brackets survive the fork as a program reference — feedback
        # (bool and analog) is entirely plant rungs now, no private callback.
        clone._refresh_synthesis()
        clone._install_monitors()
        plc._harness = clone
        return clone

    def uninstall(self) -> None:
        if not self._installed:
            return
        self._installed = False
        for handle in self._monitors:
            handle.remove()
        self._monitors.clear()
        if self._plc._harness is self:
            self._plc._harness = None
        if self._plc._synthesis is not None:
            self._plc._synthesis.plant = []
            self._plc._fold_context_cache = None
            self._plc._compiled_replay_kernel = None
            self._plc._soft_exec_program_cache = None
        self._bool_couplings.clear()
        self._profile_couplings.clear()

    def unlink(self, tags: list[str]) -> None:
        """Remove couplings for the named feedback tags.

        After unlinking, the Harness no longer synthesizes feedback for
        these tags — they become free inputs the caller can steer directly.
        Models a broken sensor or fault scenario.
        """
        drop = set(tags)
        self._bool_couplings = [c for c in self._bool_couplings if c.fb_name not in drop]
        self._profile_couplings = [c for c in self._profile_couplings if c.fb_name not in drop]
        if self._installed:
            # Rebuild the overlay and monitors so the dropped couplings stop
            # synthesizing their feedback (the fault-injection point).
            self._refresh_synthesis()
            for handle in self._monitors:
                handle.remove()
            self._monitors.clear()
            self._install_monitors()

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

    def coupling_profiles(self) -> Iterator[Any]:
        """Yield an :class:`AccProfile` per **analog** profile coupling.

        This is the static *reading* of "En drives Fb toward a threshold" that
        PILOT's accumulator resolver consumes exactly like a timer's profile —
        so ``how(Fb >= threshold)`` learns "hold En, coast N scans" without
        running anything.

        Bool couplings are intentionally NOT yielded: under the dwell model they
        are real on/off-delay timer instructions, walked directly by
        ``walk_instructions``.  An analog coupling is a per-scan profile tick
        with no owning instruction, so this adapter is its permanent home.
        """
        for c in self._profile_couplings:
            profile = self._analog_profile(c)
            if profile is not None:
                yield profile

    def _analog_rate_per_scan(
        self, c: _ProfileCoupling
    ) -> tuple[Callable[[float], float], int] | None:
        """The ``(rate_per_scan, direction)`` an analog coupling advances at while enabled.

        A :class:`~pyrung.core.physical.Ramp` gives the exact declared ``up`` rate
        (analytic).  An :class:`~pyrung.core.physical.Approach` is first-order — its
        slope depends on the current value — so ``rate_per_scan`` raises and the
        resolver falls to the empirical (fork-and-run) tier.  ``None`` when there is
        no usable static read (a zero-rate ramp).
        """
        spec = c.profile
        if isinstance(spec, Ramp):
            if spec.up == 0.0:
                return None
            per_second = abs(spec.up)
            direction = 1 if spec.up > 0 else -1
            return (lambda step_dt: per_second * step_dt), direction

        if not isinstance(spec, Approach):
            return None  # a bool Pulse has no analog accumulator profile

        # Approach: nonlinear, so the analytic rate is undefined — measure it.
        fb_now = float(self._plc.current_state.tags.get(c.fb_name, 0.0))
        toward = spec.toward
        target = (
            float(self._plc.current_state.tags.get(toward, 0.0))
            if isinstance(toward, str)
            else float(toward)
        )
        direction = 1 if target >= fb_now else -1

        def rate_per_scan(step_dt: float) -> float:
            raise ValueError("first-order Approach profile — measure empirically")

        return rate_per_scan, direction

    def _analog_profile(self, c: _ProfileCoupling) -> Any | None:
        """Build the continuous :class:`AccProfile` for one analog coupling.

        ``accumulator`` is the Fb register itself (the consumer reads
        ``Fb <cmp> threshold``, matched via the accumulator).  A ``Ramp`` yields an
        exact analytic ``rate_per_scan``; an ``Approach`` yields a raising rate, so
        ``scans_until`` returns ``None`` and the resolver measures empirically.
        """
        from pyrung.core.condition import BitCondition, CompareEq
        from pyrung.core.instruction.accumulating import KIND_APPROACH, AccProfile, _NoDone

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
        return AccProfile(
            kind=KIND_APPROACH,
            advance=advance,
            advance_value=True,
            accumulator=fb_tag,
            done=_NoDone(name=f"__analog_nodone__:{c.fb_name}"),
            preset=0,
            reset=None,
            direction=direction,
            rate_per_scan=rate_per_scan,
        )

    def _discover_couplings(self) -> None:
        seen_runtimes: set[int] = set()
        for tag in list(self._plc._known_tags_by_name.values()):
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
        if en_name not in self._plc._known_tags_by_name:
            return
        trigger_value = None
        if trigger_raw is not None:
            en_tag = self._plc._known_tags_by_name[en_name]
            trigger_value = _resolve_trigger_value(trigger_raw, en_tag)
        self._add_coupling(en_name, tag.name, tag.physical, trigger_value=trigger_value)

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
                self._add_coupling(
                    en_tag.name, fb_tag.name, spec.physical, trigger_value=trigger_value
                )

    def _add_coupling(
        self,
        en_name: str,
        fb_name: str,
        physical: Any,
        *,
        trigger_value: int | str | None = None,
    ) -> None:
        # Route by *how* the feedback is declared, not its Fb type: any declarative
        # profile spec (Ramp / Approach / Pulse) lowers via the profile path; a
        # timing-only Physical (on_delay/off_delay) is a bool dwell coupling.
        if physical.profile is not None:
            self._profile_couplings.append(
                _ProfileCoupling(
                    en_name, fb_name, physical.profile, physical, trigger_value=trigger_value
                )
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
        """(Re)build the bool-feedback plant rungs into the runner's overlay.

        Each bool coupling lowers to a real on-delay/off-delay timer pair, built
        by :func:`pyrung.core.synthesis.bool_feedback_rungs` and placed in
        ``plc._synthesis.plant`` — the rungs the runner scans *before* the user
        program (the pre-logic input-read: feedback is an input reflecting the
        previous commit's command).  The on-delay accumulates while the enable
        matches (``on_delay`` preset, in ms); the off-delay, driven by its done
        bit, holds the feedback for ``off_delay`` after the enable drops.  Presets
        are the declared delays in ms with a ms accumulator unit, so a held enable
        crosses after ``ceil(delay_ms / dt_ms)`` scans (``delay_ms == 0`` ⇒ the
        program sees the feedback next scan, from the scan boundary).

        Idempotent — used at install, on each fork (fresh instructions reading the
        fork's tags; accumulator *state* rides the inherited committed state), and
        after ``unlink`` drops a coupling.  Preserves any ``holds`` PILOT has
        installed on the same overlay.
        """
        from pyrung.core.condition import BitCondition, CompareEq, CompareNe
        from pyrung.core.synthesis import (
            Synthesis,
            analog_approach_rung,
            analog_feedback_rungs,
            bool_feedback_rungs,
            pulse_feedback_rungs,
        )
        from pyrung.core.system_points import system
        from pyrung.core.tag import Bool, Int, Real

        plant: list[Any] = []
        for c in self._bool_couplings:
            en_tag = self._plc._known_tags_by_name.get(c.en_name)
            if en_tag is None:
                continue
            enable = (
                CompareEq(en_tag, c.trigger_value)
                if c.trigger_value is not None
                else BitCondition(en_tag)
            )
            ton_done = Bool(f"__cpl_ond__{c.fb_name}", retentive=False)
            ton_acc = Int(f"__cpl_on__{c.fb_name}", retentive=False)
            tof_acc = Int(f"__cpl_off__{c.fb_name}", retentive=False)
            fb_tag = self._plc._known_tags_by_name.get(c.fb_name)
            if fb_tag is None:
                fb_tag = Bool(c.fb_name)
            c.ton_done_name = ton_done.name
            c.ton_acc_name = ton_acc.name
            c.tof_acc_name = tof_acc.name
            plant.extend(
                bool_feedback_rungs(
                    enable=enable,
                    fb_tag=fb_tag,
                    ton_done=ton_done,
                    ton_acc=ton_acc,
                    tof_acc=tof_acc,
                    on_delay_ms=c.on_delay_ms,
                    off_delay_ms=c.off_delay_ms,
                )
            )

        for c in self._profile_couplings:
            spec = c.profile
            en_tag = self._plc._known_tags_by_name.get(c.en_name)
            if en_tag is None:
                continue
            fb_tag = self._plc._known_tags_by_name.get(c.fb_name)
            if fb_tag is None:
                fb_tag = Bool(c.fb_name) if isinstance(spec, Pulse) else Real(c.fb_name)
            if c.trigger_value is not None:
                enable = CompareEq(en_tag, c.trigger_value)
                disable = CompareNe(en_tag, c.trigger_value)
            else:
                enable = BitCondition(en_tag)
                disable = ~en_tag
            if isinstance(spec, Ramp):
                armed = Bool(f"__cpl_armed__{c.fb_name}", retentive=False)
                plant.extend(
                    analog_feedback_rungs(
                        enable=enable,
                        disable=disable,
                        fb_tag=fb_tag,
                        armed=armed,
                        dt_tag=system.sys.dt,
                        up=spec.up,
                        down=spec.down,
                    )
                )
            elif isinstance(spec, Approach):  # first-order lag toward a constant/setpoint tag
                toward = spec.toward
                toward_operand: Any = (
                    (self._plc._known_tags_by_name.get(toward) or Real(toward))
                    if isinstance(toward, str)
                    else toward
                )
                plant.extend(
                    analog_approach_rung(
                        enable=enable,
                        fb_tag=fb_tag,
                        toward=toward_operand,
                        rate=spec.rate,
                        dt_tag=system.sys.dt,
                    )
                )
            else:  # Pulse — bool pulse train (astable flasher)
                plant.extend(
                    pulse_feedback_rungs(
                        enable=enable,
                        disable=disable,
                        fb_tag=fb_tag,
                        on_done=Bool(f"__pls_ond__{c.fb_name}", retentive=False),
                        on_acc=Int(f"__pls_on__{c.fb_name}", retentive=False),
                        off_done=Bool(f"__pls_offd__{c.fb_name}", retentive=False),
                        off_acc=Int(f"__pls_off__{c.fb_name}", retentive=False),
                        on_dwell_ms=spec.on_dwell_ms,
                        off_dwell_ms=spec.off_dwell_ms,
                    )
                )

        if self._plc._synthesis is None:
            self._plc._synthesis = Synthesis()
        self._plc._synthesis.plant = plant
        # The overlay changed, so caches that snapshot it are stale: the fold
        # context (acc sources) and the soft-exec replay kernel + its bracketed
        # compilation unit — all rebuilt lazily on next use.
        self._plc._fold_context_cache = None
        self._plc._compiled_replay_kernel = None
        self._plc._soft_exec_program_cache = None

    def _seed_bool_state(self) -> None:
        """Seed each bool timer to the steady state implied by its current enable.

        A fresh timer's accumulators are 0 (cold), but a coupling whose enable is
        already on represents a feedback that settled long ago — so pre-load the
        on-delay accumulator past its preset *and* assert its done bit and the
        feedback register.  Without seeding, the pre-logic plant on the first scan
        would read a cold accumulator (``0``) and drive ``Fb`` ``False`` even
        though the enable is already on; seeding the accumulator past preset (and
        ``Fb`` itself) makes an ``En``-already-True coupling steady from cold, as
        if it had settled before the program started.
        """
        seed: dict[str, Any] = {}
        snap = self._plc.current_state.tags
        for c in self._bool_couplings:
            if not c.ton_acc_name:
                continue
            en_raw = snap.get(c.en_name, False)
            en_on = en_raw == c.trigger_value if c.trigger_value is not None else bool(en_raw)
            if en_on:
                seed[c.ton_acc_name] = c.on_delay_ms
                seed[c.ton_done_name] = True
                seed[c.fb_name] = True
        if seed:
            self._plc._state = self._plc._state.with_tags(seed)
            self._plc._reset_cache(self._plc._state)
            if self._plc._state.scan_id == 0:
                self._plc._initial_state = self._plc._state

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
                    "profile": profile_to_token(c.profile),
                    "trigger_value": c.trigger_value,
                }
                for c in self._profile_couplings
            ],
            "pending_patches": self.pending_count,
        }
