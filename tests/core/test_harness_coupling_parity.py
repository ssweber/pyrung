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

if "parity_thermal" not in _profile_registry:

    def _thermal(cur: float, en: bool, dt: float) -> float:
        return cur + (1.0 if en else -0.5) * dt  # +1.0/s enabled, -0.5/s decaying

    _profile_registry["parity_thermal"] = _thermal


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


@pytest.mark.xfail(
    reason="harness: bool coupling must be dwell -- a sub-on_delay glitch must not "
    "fabricate Fb. Today's transport-delay heap leaves Fb stuck True. Fixed by the "
    "executor refactor (bool -> real TON/TOF, 2c).",
    strict=False,
)
def test_bool_glitch_is_suppressed_under_dwell() -> None:
    # on_delay 0.3s = 3 scans > off_delay 0.1s; a 1-scan glitch (< on_delay) must
    # leave Fb False forever.  Today it fabricates [.,.,.,True,True,True,True].
    assert (
        _run(_bool_prog(on_delay="0.3s", off_delay="0.1s"), [True] + [False] * 6, "Fb")
        == [False] * 7
    )
