"""What makes EXECUTE(6) leave? — source-informed, runs the real startup.

`production_states.py` gives two facts the earlier analysis lacked:

  R3  STARTING completes only when Blower__init==1 AND Rotate__init==1
      -> EXECUTE is reached only after rotate+blower init (~700 scans).  The
         walker reaches it by time-folding that wait; a short no-animation
         monitor never does (that's why the first cut stalled at STARTING(3)).

  R5  S_Execute, Or(~i_DoorClosed, ~i_LintDoorClosed) -> copy(CmdHoldRef, C_CtrlCmd)
      -> an OPEN DOOR in EXECUTE forces a command.  R5 is S_Execute-gated, so it
         cannot fire from STARTING(3) — which is why the handoff's "door
         hypothesis dead" (tested from STARTING) was a false negative.

This probe drives the real startup to EXECUTE (rotate animated, like the
reconstitute), then tests what makes it leave:

  RUN-A      all permissives held, no perturbation.        expect: EXECUTE stable
  DROP-DOOR  reach EXECUTE, then drop x_DoorClosed.         expect: R5 fires
  DROP-LINT  reach EXECUTE, then drop x_LintDoorClosed.     expect: R5 fires
  RUN-B      walker holds (x_BlowerFB only).                expect: may never init
                                                            rotate -> never EXECUTE

On leaving EXECUTE we dump C_CtrlCmd and the cause chain, so we see whether the
door drives HOLD(->10/11) or escalates to ABORTED(->9), and how that maps to the
walker's observed 6->9.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


CLICK_PROJECT = Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\Sam\AppData\Local\Temp\CLICK (00010A66)\pyrung_project",
    )
)
sys.path.insert(0, str(CLICK_PROJECT))

from pyrung import PLC  # noqa: E402
from main import logic  # noqa: E402
from pyrung.core.analysis.walk.base import _values_match  # noqa: E402

EXECUTE = 6
ABORTED = 9

ALL_PERMISSIVES: dict[str, Any] = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_RotateSensor": False,
    "x_SailRelay": True,
}
WALKER_PERMISSIVES: dict[str, Any] = {"x_BlowerFB": True}

WATCH = (
    "S_StateCurrent", "S_StateRequested", "C_CtrlCmd", "C_CmdChgRequestBool",
    "isStateEnbl_Yes", "sm__loopindex", "S_Execute", "S_Holding", "S_Held",
    "S_Aborting", "S_Aborted", "i_DoorClosed", "i_LintDoorClosed",
    "x_DoorClosed", "Rotate__init", "Blower__init", "A_AlmExtent",
)


def g(plc: PLC, name: str) -> Any:
    return plc.state.tags.get(name, "<?>")


def _trajectory(plc: PLC, tag: str, last: int = 16) -> list[tuple[int, Any, Any]]:
    h = plc.history
    states = h.range(h.oldest_scan_id, h.newest_scan_id + 1)
    out: list[tuple[int, Any, Any]] = []
    for a, b in zip(states, states[1:]):
        if not _values_match(a.tags.get(tag), b.tags.get(tag)):
            out.append((b.scan_id, a.tags.get(tag), b.tags.get(tag)))
    return out[-last:]


def _dump_cause(plc: PLC, scan: int) -> None:
    h = plc.history
    print(f"    -- state @ scan {scan} (prev -> cur) --", flush=True)
    prev, cur = h.at(scan - 1).tags, h.at(scan).tags
    for name in WATCH:
        pv, cv = prev.get(name, "<?>"), cur.get(name, "<?>")
        mark = "  <==" if not _values_match(pv, cv) else ""
        print(f"      {name}: {pv!r} -> {cv!r}{mark}", flush=True)
    try:
        chain = plc.cause("S_StateCurrent", scan=scan)
    except Exception as exc:  # noqa: BLE001
        print(f"    cause raised {type(exc).__name__}: {exc}", flush=True)
        return
    if chain is None:
        print("    cause = None", flush=True)
        return
    e = chain.effect
    print(f"    cause {e.tag_name} {e.from_value!r}->{e.to_value!r} [{chain.mode}]", flush=True)
    for i, s in enumerate(chain.steps):
        trg = [f"{t.tag_name}:{t.from_value!r}->{t.to_value!r}" for t in s.triggers]
        enb = [f"{en.tag_name}={en.value!r}" for en in s.enablers]
        print(f"      step{i} rung={s.rung_index} sub={s.subroutine} trig={trg} enb={enb}", flush=True)


def _drive(plc: PLC, permissives: dict[str, Any], settle: int) -> None:
    for k, v in permissives.items():
        plc.force(k, v)
    plc.step()
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step()
    plc.step()
    for cmd in ("C_Clear", "C_Reset", "C_Start"):
        plc.patch({cmd: True})
        plc.step()
        for _ in range(settle):
            plc.step()


def run(name: str, permissives: dict[str, Any], *, drop: str | None = None,
        cap: int = 1700, observe: int = 60, settle: int = 4) -> None:
    print(f"\n================ {name} ================", flush=True)
    print(f"  holds={sorted(permissives)} drop_after_execute={drop!r}", flush=True)
    plc = PLC(logic)
    _drive(plc, permissives, settle)
    anim = 0
    exec_scan = None
    for _ in range(cap):
        plc.force("x_RotateSensor", (anim // 50) % 2 == 0)
        plc.step()
        anim += 1
        cur = g(plc, "S_StateCurrent")
        if cur == EXECUTE:
            exec_scan = plc.history.newest_scan_id
            break
        if cur == ABORTED:
            print(f"  ABORTED before EXECUTE at scan {plc.history.newest_scan_id}; "
                  f"traj={_trajectory(plc,'S_StateCurrent')}", flush=True)
            _dump_cause(plc, plc.history.newest_scan_id)
            return
    if exec_scan is None:
        print(f"  NEVER reached EXECUTE in {cap} scans. final S_StateCurrent={g(plc,'S_StateCurrent')!r}; "
              f"Rotate__init={g(plc,'Rotate__init')!r} Blower__init={g(plc,'Blower__init')!r}; "
              f"traj={_trajectory(plc,'S_StateCurrent')}", flush=True)
        return
    print(f"  reached EXECUTE(6) at scan {exec_scan} "
          f"(Rotate__init={g(plc,'Rotate__init')!r} Blower__init={g(plc,'Blower__init')!r})", flush=True)

    if drop is not None:
        plc.force(drop, False)
        print(f"  dropped {drop} -> False", flush=True)

    for _ in range(observe):
        plc.force("x_RotateSensor", (anim // 50) % 2 == 0)
        plc.step()
        anim += 1
        cur = g(plc, "S_StateCurrent")
        if not _values_match(cur, EXECUTE):
            out_scan = plc.history.newest_scan_id
            print(f"  LEFT EXECUTE: 6 -> {cur!r} at scan {out_scan}", flush=True)
            _dump_cause(plc, out_scan)
            return
    print(f"  EXECUTE STABLE for {observe} scans (C_CtrlCmd={g(plc,'C_CtrlCmd')!r}).", flush=True)


def main() -> int:
    print(f"CLICK_PROJECT={CLICK_PROJECT}", flush=True)
    run("RUN-A all-permissives (baseline)", ALL_PERMISSIVES)
    run("DROP-DOOR (x_DoorClosed in EXECUTE)", ALL_PERMISSIVES, drop="x_DoorClosed")
    run("DROP-LINT (x_LintDoorClosed in EXECUTE)", ALL_PERMISSIVES, drop="x_LintDoorClosed")
    run("RUN-B walker-holds (x_BlowerFB only)", WALKER_PERMISSIVES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
