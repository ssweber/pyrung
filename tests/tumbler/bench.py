"""Shared test bench for driving the tumbler fixture like a commissioning rig.

Ported from ``scratchpad/burner/reconstitute_*.py``.  The bench holds the
physical permissives true, keeps the rotate sensor oscillating (the rotate
watchdogs fault a stalled sensor once Rotate_CurStep >= 3: SensorOn WD 2 s,
SensorOff WD 10 s; a 50-scan / 0.5 s half period keeps both fed), and offers
``step_until`` staging primitives.
"""

from __future__ import annotations

from collections.abc import Callable

from pyrung import PLC

#: Physical permissives and feedback.  These are external inputs, not internal
#: shortcuts; ReadInputs maps them into the i_* image bits.
PHYSICAL = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_RotateSensor": False,
    "x_SailRelay": True,
}

#: Alarm status words: A_AlmExtent sums ds[201..300] (main R67).  Any nonzero
#: member drives ProductionErrors R1 -> Abort.  Tracked as a band to prove the
#: completion gates are satisfied without touching any of them.
ALARM_STATUS_TAGS = [f"A_Alm{i}_Status" for i in range(1, 101)]
ALARM_TRIG_BITS = (
    "A_Alm11_Rotate_Trig",
    "A_Alm12_Blower_Trig",
    "A_Alm13_Heat_Trig",
    "A_Alm14_DoorOpen_Trig",
    "A_Alm15_LintOpen_Trig",
    "A_Alm16_Sail_Trig",
    "A_Alm17_HiHeat_Trig",
)

_MISSING = "<missing>"


class Bench:
    """Hand-drive the tumbler program with rotate-sensor animation."""

    def __init__(self, logic) -> None:
        self.plc = PLC(logic, dt=0.010)
        self.scan = 0
        self.burner_hit_scan: int | None = None
        self.alarm_baseline: dict[str, object] = {}
        self.alarm_violations: list[str] = []
        self.almextent_max = 0

    # -- primitives ---------------------------------------------------------
    def get(self, name: str) -> object:
        return self.plc.state.tags.get(name, _MISSING)

    def force(self, name: str, value: object) -> None:
        self.plc.force(name, value)

    def patch(self, mapping: dict) -> None:
        self.plc.patch(mapping)

    def force_physical(self) -> None:
        for name, value in PHYSICAL.items():
            self.force(name, value)

    def _oscillate(self) -> None:
        self.force("x_RotateSensor", (self.scan // 50) % 2 == 0)

    def step(self, count: int = 1) -> None:
        for _ in range(count):
            self._oscillate()
            self.plc.step()
            self.scan += 1
            if self.burner_hit_scan is None and self.get("y_BurnerLoop") is True:
                self.burner_hit_scan = self.scan
            self._check_alarms()

    def step_until(self, pred: Callable[[], bool], limit: int) -> bool:
        for _ in range(limit):
            self.step()
            if pred():
                return True
        return False

    def pulse(self, name: str, settle_scans: int = 4) -> None:
        self.patch({name: True})
        self.step(1 + settle_scans)

    def force_done(self, acc_tag: str, preset: int) -> None:
        """Fast-forward a self-advancing timer by writing its accumulator to preset.

        Mirrors PILOT let-run/zoom: once the dwell's guard holds, jump the
        governing register to completion rather than stepping the
        (minute-scale) real dwell.
        """
        self.patch({acc_tag: preset})

    # -- alarm red-herring tracking ----------------------------------------
    def snapshot_alarms(self) -> None:
        self.alarm_baseline = {t: self.get(t) for t in ALARM_STATUS_TAGS}
        self.alarm_baseline.update({t: self.get(t) for t in ALARM_TRIG_BITS})

    def _check_alarms(self) -> None:
        if not self.alarm_baseline:
            return
        extent = self.get("A_AlmExtent")
        if isinstance(extent, int) and extent > self.almextent_max:
            self.almextent_max = extent
        for tag, base in self.alarm_baseline.items():
            cur = self.get(tag)
            if cur != base:
                note = f"scan {self.scan}: {tag} {base!r} -> {cur!r}"
                if not any(
                    v.split(":")[1].strip().startswith(tag + " ") for v in self.alarm_violations
                ):
                    self.alarm_violations.append(note)

    # -- reporting ----------------------------------------------------------
    def snapshot(self, names: tuple[str, ...]) -> str:
        return ", ".join(f"{n}={self.get(n)!r}" for n in names)
