"""Characterization / parity oracle for harness coupling executors.

Pins the per-scan feedback the harness synthesizes, so the executor refactor
(analog coupling -> ``when().do()``; bool coupling -> real TON/TOF *dwell*) is
held to account: an intended diff is a visible, reviewed change; an accidental
one fails here.

Bool couplings are **dwell** — feedback responds to a *sustained* command, never
a glitch.  The response floor is one scan: ``on_delay == 0`` turns Fb on the
*next* scan (the current ``max(1, on_delay_scans)`` arithmetic already gives the
right timing).  The one behaviour the dwell refactor *changes* is glitch
suppression: today's transport-delay heap fabricates Fb from a sub-``on_delay``
pulse — a bug, encoded below as an ``xfail`` that flips to ``xpass`` when 2c lands.
"""

from __future__ import annotations

import pytest

from pyrung import Bool, Int, Program, Real, Rung, copy
from pyrung.core.harness import Harness, _profile_registry
from pyrung.core.physical import Physical
from pyrung.core.runner import PLC


def _thermal(cur: float, en: bool, dt: float) -> float:
    return cur + (1.0 if en else -0.5) * dt  # +1.0/s enabled, -0.5/s decaying


@pytest.fixture(autouse=True)
def _register_parity_thermal() -> None:
    # Re-register per test: other harness suites clear ``_profile_registry`` in
    # their setup, so a module-level registration alone is order-dependent.
    _profile_registry.setdefault("parity_thermal", _thermal)


def _run(prog: Program, en_seq: list[bool], fb: str, dt: float = 0.1) -> list:
    """Drive Enable per *en_seq* (one value/scan); return Fb after each scan."""
    plc = PLC(prog, dt=dt)
    Harness(plc).install()
    out = []
    for en in en_seq:
        plc.patch({"Enable": en})
        plc.step()
        v = plc.state.tags[fb]
        out.append(round(v, 4) if isinstance(v, float) else v)
    return out


def _analog_prog() -> Program:
    Enable = Bool("Enable", external=True)
    Temp = Real("Temp", physical=Physical("T", profile="parity_thermal"), link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Temp >= 5.0):
            copy(1, Stage)
    return prog


def _bool_prog(on_delay: str = "0.2s", off_delay: str = "0.1s") -> Program:
    Enable = Bool("Enable", external=True)
    Fb = Bool("Fb", physical=Physical("M", on_delay=on_delay, off_delay=off_delay), link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Fb):
            copy(1, Stage)
    return prog


# ── analog ── must survive the analog -> when().do() swap (modulo a documented
#    phase decision; a 1-scan lag would shift this and should be reviewed) ──────


def test_analog_ramp_and_decay_parity() -> None:
    # 1-scan activation floor, then +0.1/scan while enabled, -0.05/scan decaying.
    assert _run(_analog_prog(), [True] * 5 + [False] * 3, "Temp") == [
        0.0,
        0.1,
        0.2,
        0.3,
        0.4,
        0.5,
        0.45,
        0.4,
    ]


# ── bool dwell ── sustained command, 1-scan floor on each edge ────────────────


def test_bool_sustained_rise_and_fall_parity() -> None:
    # on_delay 0.2s = 2 scans, off_delay 0.1s = 1 scan; En held 4 scans then 3 off.
    assert _run(_bool_prog(), [True] * 4 + [False] * 3, "Fb") == [
        False,
        False,
        True,
        True,
        True,
        False,
        False,
    ]


def test_bool_on_delay_zero_is_next_scan() -> None:
    # on_delay == 0 -> Fb on the NEXT scan; off_delay == 0 -> off the next scan.
    assert _run(_bool_prog(on_delay="0ms", off_delay="0ms"), [True] * 3 + [False] * 2, "Fb") == [
        False,
        True,
        True,
        True,
        False,
    ]


def test_bool_glitch_is_suppressed_under_dwell() -> None:
    # on_delay 0.3s = 3 scans > off_delay 0.1s; a 1-scan glitch (< on_delay)
    # leaves Fb False forever under dwell.  (The retired transport-delay heap
    # fabricated [.,.,.,True,True,True,True] from this sub-on_delay pulse.)
    assert (
        _run(_bool_prog(on_delay="0.3s", off_delay="0.1s"), [True] + [False] * 6, "Fb")
        == [False] * 7
    )


# ── bool dwell folds like a program timer ─────────────────────────────────────
#    The on/off-delay timers are registered as ordinary fold sources, so a long
#    dwell collapses to a handful of real scans (dt-knob) and lands *bit-equal*
#    to stepping scan-by-scan — not stepped one scan at a time.


def _bool_fold_plc(dt: float = 0.010):
    Enable = Bool("Enable", external=True)
    # 2s on-delay at dt=10ms = 200 scans of dwell to fold through.
    Fb = Bool("Fb", physical=Physical("M", on_delay="2s", off_delay="500ms"), link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Fb):
            copy(1, Stage)
    plc = PLC(prog, dt=dt)
    Harness(plc).install()
    return plc


def test_bool_dwell_folds_to_few_real_scans() -> None:
    plc = _bool_fold_plc()
    real = [0]
    orig = plc._run_single_scan

    def _counted(*a, **k):
        real[0] += 1
        return orig(*a, **k)

    plc._run_single_scan = _counted  # type: ignore[method-assign]
    plc.force("Enable", True)
    plc.run_until(lambda s: s.tags.get("Fb") is True, max_cycles=5000, fold=True)

    assert plc.state.tags["Fb"] is True
    assert plc.state.scan_id >= 200  # the dwell really was ~200 scans long
    assert real[0] < 20  # …yet folded into a handful of real scans (not stepped)


def test_bool_dwell_fold_matches_stepping() -> None:
    folded = _bool_fold_plc()
    folded.force("Enable", True)
    folded.run_until(lambda s: s.tags.get("Fb") is True, max_cycles=5000, fold=True)

    stepped = _bool_fold_plc()
    stepped.force("Enable", True)
    stepped.run_until(lambda s: s.tags.get("Fb") is True, max_cycles=5000, fold=False)

    # Same scan the feedback rose, same accumulator — fold is sound, not approximate.
    assert folded.state.scan_id == stepped.state.scan_id
    assert folded.state.tags["Stage"] == stepped.state.tags["Stage"] == 1
