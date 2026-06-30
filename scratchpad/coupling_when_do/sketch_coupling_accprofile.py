"""SKETCH 2 — the *reader* adapter: couplings expose ``accumulating_profile()``.

This is the load-bearing half. The executor (Sketch 1) only makes the fork *run*
the coupling; it leaves trace blind. What makes ``how(Fb==True)`` / ``how(Temp>=5)``
resolvable is the coupling exposing an :class:`AccProfile` so the EXISTING pilot
resolver (``accumulators.resolve_profile`` / ``scans_to_eject``) consumes it the
same way it consumes a timer — no isinstance, no special path.

This sketch imports the real ``AccProfile`` and reproduces the real resolver match
logic so it runs against the codebase and the wrinkles are honest, not hand-waved:

    (W1) bool coupling has no live accumulator REGISTER — its "elapsed" lives in
         the scheduler heap, not state.tags. So acc_now is always 0: the profile
         reports the FULL delay from a fresh assert. Correct for planning (PILOT
         newly holds En); an overestimate mid-flight.
    (W2) a bool coupling yields TWO profiles on the SAME done tag (Fb): an
         on-delay for Fb==True and an off-delay for Fb==False. resolve_profile
         matches on tag NAME only — it must become target-value-aware, or the
         coupling must hand back the direction-matched profile at resolve time.
    (W3) analog nonlinear profile -> rate_per_scan raises -> scans_until returns
         None -> the existing empirical Tier-2 fallback. No new fallback path.
    (W4) the ONE integration edit: iter_profiles(program) walks instructions;
         couplings aren't instructions. It must also yield harness coupling
         profiles. That single change wires both reading payoffs.

Run:  uv run python scratchpad/coupling_when_do/sketch_coupling_accprofile.py
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from pyrung.core.instruction.accumulating import KIND_OFF_DELAY, KIND_ON_DELAY, AccProfile

# ───────────────────────────────────────────────────────────────────────────
# Tiny stand-ins (resolver only reads `.name`; _resolve_int reads `.default`).
# Real code uses Tags / Conditions here.
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _NamedTag:
    name: str
    default: Any = 0


@dataclass(frozen=True)
class _Cond:
    """Placeholder for a real Condition (En, or En == trigger_value). The reader
    never evaluates it — only PILOT's hold-installation reads it, to know what to
    hold. Kept opaque here on purpose."""

    desc: str


KIND_APPROACH = "approach"  # NOTE: a new KIND_* for analog. Timers/counters reuse
# the prover's done-kind vocab; analog has no done bit, so this kind only labels
# the profile — it does not feed the prover's done-abstraction.


# ───────────────────────────────────────────────────────────────────────────
# Adapter A — bool link  ->  on/off-delay AccProfile  (shape-identical to a TON)
# ───────────────────────────────────────────────────────────────────────────


def _virtual_elapsed(fb_name: str) -> _NamedTag:
    """W1: a synthetic accumulator that is NEVER in state.tags, so _resolve_int
    falls back to its default (0). The bool coupling's progress is in the heap,
    not a register; this makes scans_until report the full delay from acc=0."""
    return _NamedTag(name=f"__fb_elapsed:{fb_name}", default=0)


def bool_coupling_profiles(
    fb: _NamedTag, en: _Cond, *, on_delay_ms: int, off_delay_ms: int
) -> tuple[AccProfile, AccProfile]:
    """Return (rising_profile, falling_profile).  W2: both share done=Fb.

    Units mirror a real ms timer EXACTLY: accumulator/preset in ms,
    rate_per_scan = dt_to_units = dt*1000 (ms per scan). So the bool coupling's
    AccProfile is structurally indistinguishable from OnDelayInstruction's —
    which is the whole point: the on-delay resolver already knows how to walk it.
    """
    ms_per_scan = lambda dt: dt * 1000.0  # noqa: E731 — == TimeUnit("ms").dt_to_units

    rising = AccProfile(
        kind=KIND_ON_DELAY,
        advance=en,
        advance_value=True,  # Fb rises while En held
        accumulator=_virtual_elapsed(fb.name),
        done=fb,
        preset=on_delay_ms,
        reset=en,  # En going False resets the rising ramp
        direction=1,
        rate_per_scan=ms_per_scan,
    )
    falling = AccProfile(
        kind=KIND_OFF_DELAY,
        advance=en,
        advance_value=False,  # Fb falls while En stays False
        accumulator=_virtual_elapsed(fb.name),
        done=fb,
        preset=off_delay_ms,
        reset=en,
        direction=1,
        rate_per_scan=ms_per_scan,
    )
    return rising, falling


# ───────────────────────────────────────────────────────────────────────────
# Adapter B — analog link  ->  continuous AccProfile (accumulator = Fb itself)
# ───────────────────────────────────────────────────────────────────────────


def analog_coupling_profile(
    fb: _NamedTag, en: _Cond, profile_fn: Any, *, ref_cur: float = 0.0, dt_ref: float = 0.01
) -> AccProfile:
    """The Fb register IS the accumulator. Consumer reads ``Fb >= threshold`` ->
    matches via accumulator (via_done=False), threshold supplied by the caller.

    rate_per_scan is derived by sampling the profile's advancing slope. If the
    slope is constant (linear approach) -> analytic. If it varies with cur
    (first-order / exponential) -> rate raises -> scans_until returns None -> W3
    empirical fallback. Direction is the sign of the advancing slope."""
    d_lo = profile_fn(ref_cur, True, dt_ref) - ref_cur
    d_hi = profile_fn(ref_cur + 100.0, True, dt_ref) - (ref_cur + 100.0)
    linear = math.isclose(d_lo, d_hi, abs_tol=1e-9)
    direction = 1 if d_lo >= 0 else -1

    if linear:
        per_dt = abs(d_lo) / dt_ref

        def rate_per_scan(dt: float) -> float:
            return per_dt * dt
    else:

        def rate_per_scan(dt: float) -> float:
            raise ValueError("nonlinear profile — measure empirically (Tier 2)")

    return AccProfile(
        kind=KIND_APPROACH,
        advance=en,
        advance_value=True,
        accumulator=fb,  # ← Fb is the live register; mid-flight acc_now is real
        done=None,  # no latch; consumer matches via accumulator + threshold
        preset=0,  # unused for analog (threshold comes from the read comparison)
        reset=None,
        direction=direction,
        rate_per_scan=rate_per_scan,
    )


# ───────────────────────────────────────────────────────────────────────────
# Mirror of accumulators.resolve_profile, but over a PROFILE LIST instead of a
# program. W4: the real edit is making iter_profiles ALSO yield these. The match
# logic itself is unchanged — that's the claim being demonstrated.
# ───────────────────────────────────────────────────────────────────────────


def resolve_among(
    profiles: list[AccProfile], consumer_tag: str, *, want_value: Any = None
) -> tuple[AccProfile, bool] | None:
    """Returns (profile, via_done). W2: when want_value is given we disambiguate
    the two same-done bool profiles by direction (True->on-delay, False->off-delay).
    This is the ONLY resolver change bool couplings force."""
    matches: list[tuple[AccProfile, bool]] = []
    for p in profiles:
        if consumer_tag == getattr(p.done, "name", None):
            matches.append((p, True))
        elif consumer_tag == getattr(p.accumulator, "name", None):
            matches.append((p, False))
    if not matches:
        return None
    if len(matches) > 1 and want_value is not None:
        # bool coupling: rising profile establishes True, falling establishes False
        want_rising = bool(want_value)
        for p, via in matches:
            if (p.kind == KIND_ON_DELAY) == want_rising:
                return p, via
    return matches[0]


def _scans_until(profile: AccProfile, target: float, acc_now: float, dt: float) -> int | None:
    return profile.scans_until(int(target), acc_now=int(acc_now), dt=dt)


# ───────────────────────────────────────────────────────────────────────────
# Demo
# ───────────────────────────────────────────────────────────────────────────


def _linear_thermal(cur: float, en: bool, dt: float) -> float:
    return cur + 0.5 * dt if en else cur  # +0.5 units/s — constant slope


def _first_order(cur: float, en: bool, dt: float) -> float:
    setpoint = 10.0
    return cur + (setpoint - cur) * 0.4 * dt if en else cur  # slope depends on cur


def _demo() -> None:
    dt = 0.01

    print("=" * 68)
    print("BOOL LINK  -- En -> MotorRunning, on_delay=2s off_delay=500ms")
    print("=" * 68)
    fb = _NamedTag("MotorRunning")
    en = _Cond("MotorCmd")
    rising, falling = bool_coupling_profiles(fb, en, on_delay_ms=2000, off_delay_ms=500)
    pool = [rising, falling]

    # how(MotorRunning == True): resolve, then scans-to-eject from a fresh assert.
    hit = resolve_among(pool, "MotorRunning", want_value=True)
    assert hit is not None
    prof, via_done = hit
    scans = _scans_until(prof, target=prof.preset, acc_now=0, dt=dt)
    print(f"  how(MotorRunning==True)  -> kind={prof.kind:<9} via_done={via_done}")
    print(f"     advance to hold       = {prof.advance.desc}=True")
    print(f"     scans_to_eject        = {scans}  (= 2000ms / (0.01*1000) = 200 scans)")

    hit = resolve_among(pool, "MotorRunning", want_value=False)
    assert hit is not None
    prof, _ = hit
    scans = _scans_until(prof, target=prof.preset, acc_now=0, dt=dt)
    print(f"  how(MotorRunning==False) -> kind={prof.kind:<9}")
    print(f"     advance to hold       = {prof.advance.desc}=False")
    print(f"     scans_to_eject        = {scans}  (= 500ms / 10ms = 50 scans)")

    print()
    print("=" * 68)
    print("ANALOG LINK -- Enable -> Temp (the motivating how(Temp>=5.0) case)")
    print("=" * 68)
    temp = _NamedTag("Temp")
    enable = _Cond("Enable")

    lin = analog_coupling_profile(temp, enable, _linear_thermal)
    hit = resolve_among([lin], "Temp")  # consumer reads Temp >= 5.0 -> via accumulator
    assert hit is not None
    prof, via_done = hit
    scans = _scans_until(prof, target=5.0, acc_now=0, dt=dt)
    print(f"  linear profile  -> kind={prof.kind} via_done={via_done} dir={prof.direction:+d}")
    print(f"     advance to hold = {prof.advance.desc}=True")
    print(f"     scans_to_eject  = {scans}  (analytic: 5.0 / 0.5units/s / 0.01dt = 1000 scans)")

    fo = analog_coupling_profile(temp, enable, _first_order)
    scans = _scans_until(fo, target=5.0, acc_now=0, dt=dt)
    print(f"  first-order     -> scans_to_eject = {scans}  (W3: None -> empirical Tier-2 fork)")

    print()
    print("Independence check: nothing above ran a scan. resolve_profile +")
    print("scans_until are PURE READS. The executor (Sketch 1) is never touched.")


if __name__ == "__main__":
    _demo()
