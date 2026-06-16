"""Probe 14: why doesn't the C_CtrlCmd state-command chain solve?

Phase A — ground truth: single pulses of C_Clear / C_Reset / C_Start from
cold advance S_StateCurrent (9 -> 1 -> 2 -> 15 -> 4 -> 3) with NO bundle:
sm_map_cmd2_val runs unconditionally, so one ack-cleared HMI bit flows
through the whole validity chain in-scan. If true, the handshake bundles
are not even needed here — the question becomes why the S_StateCurrent
corridor explore doesn't find plain pulses.

Phase B — how(S_StateCurrent == 2): one C_Clear pulse should do it.
Phase C — how(S_StateCurrent == 4): C_Clear then C_Reset.
"""

import logging
import sys
import time

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (0032023C)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402
from tags import S_StateCurrent  # noqa: E402

from pyrung import PLC  # noqa: E402

logging.basicConfig(stream=sys.stdout, level=logging.WARNING, format="%(name)s %(message)s")
logging.getLogger("pyrung.core.analysis.walk").setLevel(logging.DEBUG)


def phase_a():
    print("=== Phase A: ground truth (pulse C_Clear / C_Reset / C_Start) ===", flush=True)
    plc = PLC(logic)
    plc.step()
    print(f"cold: S_StateCurrent={plc.state.tags['S_StateCurrent']}")

    for pulse in ("C_Clear", "C_Reset", "C_Start"):
        plc.patch({pulse: True})
        traj = []
        for _ in range(5):
            plc.step()
            traj.append(plc.state.tags["S_StateCurrent"])
        print(f"pulse {pulse}: S_StateCurrent trajectory {traj}")
    print(flush=True)


def phase_how(label, expr, seconds):
    print(f"=== Phase {label}: how({expr!r}, walk_seconds={seconds}) ===", flush=True)
    plc = PLC(logic)
    plc.step()
    t0 = time.monotonic()
    path = plc.how(expr, walk_seconds=seconds)
    print(f"how() returned in {time.monotonic() - t0:.1f}s", flush=True)
    print(str(path)[:3000], flush=True)
    print(flush=True)


phase_a()
phase_how("B", S_StateCurrent == 2, 60)
phase_how("C", S_StateCurrent == 4, 60)
