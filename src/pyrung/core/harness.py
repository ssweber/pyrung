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

from pyrung.core.physical import Physical
from pyrung.core.tag import TagType

if TYPE_CHECKING:
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.runner import PLC

_profile_registry: dict[str, Callable[..., Any]] = {}


def profile(name: str) -> Callable[..., Any]:
    """Register an analog feedback profile function.

    The decorated function is called once per scan tick for each active
    analog coupling::

        @profile("generic_thermal")
        def generic_thermal(cur, en, dt):
            if en:
                return cur + 0.5 * dt
            return cur
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _profile_registry[name] = fn
        return fn

    return decorator


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

    ``ton`` (on-delay) rises only after the enable has been sustained for
    ``on_delay``; ``tof`` (off-delay) is driven by ``ton``'s done bit and keeps
    the feedback asserted for ``off_delay`` after the enable drops.  The feedback
    tag *is* ``tof``'s done bit, so the only register the program reads is ``Fb``;
    ``ton_acc`` / ``tof_acc`` are the timers' internal accumulators (excluded from
    the fold plateau — they churn every scan but are unobservable).
    """

    en_name: str
    fb_name: str
    on_delay_ms: int
    off_delay_ms: int
    physical: Physical
    trigger_value: int | str | None = None
    ton: OnDelayInstruction | None = None
    tof: OffDelayInstruction | None = None
    ton_acc_name: str = ""
    tof_acc_name: str = ""


@dataclass
class _ProfileCoupling:
    en_name: str
    fb_name: str
    profile_name: str
    physical: Physical
    active: bool = False
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

    Walks all known tags to find link= couplings.  Bool couplings are lowered
    to real on-delay/off-delay timer pairs ticked each pre-scan (dwell); analog
    couplings tick their profile function while their enable edge monitor has
    activated them.

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
        self._build_bool_timers()
        self._seed_bool_state()
        self._install_monitors()
        self._plc._pre_scan_callbacks.append(self._on_pre_scan)
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
        clone._install_monitors()
        plc._pre_scan_callbacks.append(clone._on_pre_scan)
        plc._harness = clone
        return clone

    def uninstall(self) -> None:
        if not self._installed:
            return
        self._installed = False
        for handle in self._monitors:
            handle.remove()
        self._monitors.clear()
        try:
            self._plc._pre_scan_callbacks.remove(self._on_pre_scan)
        except ValueError:
            pass
        if self._plc._harness is self:
            self._plc._harness = None
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

    def coupling_acc_specs(self) -> list[tuple[str, str, int]]:
        """Fold-source descriptors for the bool-timer accumulators.

        Each entry is ``(acc_name, done_name, preset_ms)`` for one on/off-delay
        timer.  The fold builds an ``_AccSource`` per entry so a bool coupling's
        dwell folds exactly like any program timer — bounded at its preset
        crossing (the ``Fb`` flip), excluded from the plateau guard, and advanced
        by the dt knob — instead of stepping scan-by-scan.  Only the *governing*
        accumulator of the active phase has a non-zero per-scan delta, so the
        idle one contributes no spurious bound.
        """
        specs: list[tuple[str, str, int]] = []
        for c in self._bool_couplings:
            if c.ton is None or c.tof is None:
                continue
            specs.append((c.ton_acc_name, c.ton.done_bit.name, c.on_delay_ms))
            specs.append((c.tof_acc_name, c.fb_name, c.off_delay_ms))
        return specs

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

    def _analog_profile(self, c: _ProfileCoupling) -> Any | None:
        """Build the continuous :class:`AccProfile` for one analog coupling.

        ``accumulator`` is the Fb register itself (the consumer reads
        ``Fb <cmp> threshold``, matched via the accumulator).  ``rate_per_scan``
        is derived by sampling the profile's advancing slope: constant slope →
        analytic; slope that varies with the value (first-order / exponential) →
        ``rate_per_scan`` raises, so ``scans_until`` returns ``None`` and the
        resolver falls back to the empirical (fork-and-run) tier.
        """
        from pyrung.core.condition import BitCondition, CompareEq
        from pyrung.core.instruction.accumulating import KIND_APPROACH, AccProfile, _NoDone

        fn = _profile_registry.get(c.profile_name)
        fb_tag = self._plc._known_tags_by_name.get(c.fb_name)
        en_tag = self._plc._known_tags_by_name.get(c.en_name)
        if fn is None or fb_tag is None or en_tag is None:
            return None

        dt = float(getattr(self._plc, "_dt", 0.01) or 0.01)
        try:
            slope_lo = float(fn(0.0, True, dt))  # delta from cur=0.0
            slope_hi = float(fn(100.0, True, dt)) - 100.0
        except Exception:  # noqa: BLE001 — unusable profile → no static read
            return None

        direction = 1 if slope_lo >= 0 else -1
        linear = abs(slope_lo - slope_hi) <= 1e-9
        if linear and dt > 0 and slope_lo != 0.0:
            per_dt = abs(slope_lo) / dt

            def rate_per_scan(step_dt: float) -> float:
                return per_dt * step_dt
        else:

            def rate_per_scan(step_dt: float) -> float:
                raise ValueError("nonlinear analog profile — measure empirically")

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

    def _on_pre_scan(self, ctx: Any) -> None:
        bool_writes = self._tick_bool_timers(ctx)
        analog_details = self._tick_analog_with_provenance()

        if analog_details:
            self._plc.patch({n: v for n, v, _p in analog_details})

        if self.on_patches_applied is not None and (bool_writes or analog_details):
            notifications: list[tuple[str, Any, str]] = [
                (n, v, "harness:nominal") for n, v in bool_writes.items()
            ]
            notifications.extend((n, v, f"harness:analog:{p}") for n, v, p in analog_details)
            self.on_patches_applied(notifications)

    def _tick_bool_timers(self, ctx: Any) -> dict[str, Any]:
        """Advance every bool coupling's on/off-delay timer pair one scan.

        Runs the *real* ``TON`` (rising / on_delay) and ``TOF`` (falling /
        off_delay) **against the live scan context**, before this scan's input
        patches are applied — so the timers read the previous committed ``En``
        (the synthesis-overlay-before-the-program phase) and their accumulator /
        feedback / fractional writes ride the normal commit and persist in state
        like any program timer.  A sustained enable rises ``Fb`` after
        ``on_delay``; a glitch shorter than ``on_delay`` resets the accumulator
        and never fabricates it.

        ``dt`` reads the fold's pending step-override (``_dt_override_for_next_scan``,
        set just before this scan and not yet consumed) so a folded step advances
        the timer the full skip — the dt knob, exactly as program timers fold.
        Returns ``{Fb: value}`` for the on-patches-applied notification.
        """
        if not self._bool_couplings:
            return {}

        override = getattr(self._plc, "_dt_override_for_next_scan", None)
        dt = float(override if override is not None else (getattr(self._plc, "_dt", 0.01) or 0.01))
        ctx.set_memory("_dt", dt)

        writes: dict[str, Any] = {}
        for c in self._bool_couplings:
            if c.ton is None or c.tof is None:
                continue
            en_raw = ctx.get_tag(c.en_name, False)
            en_on = en_raw == c.trigger_value if c.trigger_value is not None else bool(en_raw)

            c.ton.execute(ctx, en_on)
            ton_done = bool(ctx.get_tag(c.ton.done_bit.name, False))
            c.tof.execute(ctx, ton_done)

            writes[c.fb_name] = bool(ctx.get_tag(c.fb_name, False))

        return writes

    def _tick_analog_with_provenance(self) -> list[tuple[str, Any, str]]:
        results: list[tuple[str, Any, str]] = []
        for coupling in self._profile_couplings:
            if not coupling.active:
                continue
            fn = _profile_registry.get(coupling.profile_name)
            if fn is None:
                continue
            state = self._plc.current_state
            cur = state.tags.get(coupling.fb_name, 0.0)
            en_raw = state.tags.get(coupling.en_name, False)
            if coupling.trigger_value is not None:
                en = en_raw == coupling.trigger_value
            else:
                en = bool(en_raw)
            dt = state.memory.get("_dt", self._plc._dt)
            results.append((coupling.fb_name, fn(cur, en, dt), coupling.profile_name))
        return results

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
        if physical.feedback_type == "bool":
            on_ms = physical.on_delay_ms or 0
            off_ms = physical.off_delay_ms or 0
            self._bool_couplings.append(
                _BoolCoupling(
                    en_name, fb_name, on_ms, off_ms, physical, trigger_value=trigger_value
                )
            )
        elif physical.feedback_type == "analog" and physical.profile is not None:
            self._profile_couplings.append(
                _ProfileCoupling(
                    en_name, fb_name, physical.profile, physical, trigger_value=trigger_value
                )
            )

    def _build_bool_timers(self) -> None:
        """Lower each bool coupling to a real on-delay/off-delay timer pair.

        The ``TON`` accumulates while the enable matches (``on_delay`` preset, in
        ms); the ``TOF`` is driven by the ``TON`` done bit (``off_delay`` preset)
        and its own done bit *is* the feedback register.  Presets are the declared
        delays in ms with a ms accumulator unit, so a held enable crosses after
        ``ceil(delay_ms / dt_ms)`` scans — the same scan count the retired heap
        scheduled, floor included (``delay_ms == 0`` ⇒ next scan).
        """
        from pyrung.core.condition import BitCondition, CompareEq
        from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
        from pyrung.core.tag import Bool, Int

        for c in self._bool_couplings:
            en_tag = self._plc._known_tags_by_name.get(c.en_name)
            if en_tag is None:
                continue
            enable = (
                CompareEq(en_tag, c.trigger_value)
                if c.trigger_value is not None
                else BitCondition(en_tag)
            )
            ton_acc = Int(f"__cpl_on__{c.fb_name}", retentive=False)
            ton_done = Bool(f"__cpl_ond__{c.fb_name}", retentive=False)
            tof_acc = Int(f"__cpl_off__{c.fb_name}", retentive=False)
            fb_tag = self._plc._known_tags_by_name.get(c.fb_name)
            if fb_tag is None:
                fb_tag = Bool(c.fb_name)
            c.ton = OnDelayInstruction(ton_done, ton_acc, c.on_delay_ms, enable, unit="Tms")
            c.tof = OffDelayInstruction(fb_tag, tof_acc, c.off_delay_ms, ton_done, unit="Tms")
            c.ton_acc_name = ton_acc.name
            c.tof_acc_name = tof_acc.name

    def _seed_bool_state(self) -> None:
        """Seed each bool timer to the steady state implied by its current enable.

        A fresh timer's accumulators are 0 (cold), but a coupling whose enable is
        already on represents a feedback that settled long ago — so pre-load the
        on-delay accumulator past its preset.  Without this an ``En``-already-True
        coupling would spuriously ramp its feedback from cold on the first scans
        (and, for a ``default=True`` feedback, momentarily drop it).
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
        if seed:
            self._plc._state = self._plc._state.with_tags(seed)
            self._plc._reset_cache(self._plc._state)
            if self._plc._state.scan_id == 0:
                self._plc._initial_state = self._plc._state

    def _install_monitors(self) -> None:
        # Only *analog* couplings need an enable-edge monitor — it latches the
        # profile active on first activation (without it the tick would decay Fb
        # below rest from scan 0).  Bool couplings are real timers ticked every
        # scan, so they need no monitor.
        en_to_analog: dict[str, list[_ProfileCoupling]] = {}
        for coupling in self._profile_couplings:
            en_to_analog.setdefault(coupling.en_name, []).append(coupling)

        for en_name, analog_couplings in en_to_analog.items():
            handle = self._plc.monitor(
                en_name,
                self._make_en_callback(analog_couplings),
            )
            self._monitors.append(handle)

    def _make_en_callback(
        self,
        analog_couplings: list[_ProfileCoupling],
    ) -> Callable[[Any, Any], None]:
        plain_analog = [c for c in analog_couplings if c.trigger_value is None]
        trigger_analog = [c for c in analog_couplings if c.trigger_value is not None]

        def on_en_change(current: Any, previous: Any) -> None:
            if bool(current) != bool(previous):
                for coupling in plain_analog:
                    coupling.active = True

            for coupling in trigger_analog:
                was_match = previous == coupling.trigger_value
                is_match = current == coupling.trigger_value
                if was_match == is_match:
                    continue
                coupling.active = True

        return on_en_change

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
                    "profile": c.profile_name,
                    "active": c.active,
                    "trigger_value": c.trigger_value,
                }
                for c in self._profile_couplings
            ],
            "pending_patches": self.pending_count,
        }
