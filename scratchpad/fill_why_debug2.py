"""Debug: step through the fill state machine to see scan-order effects."""

import sys

PROJECT = r"C:\Users\ssweb\AppData\Local\Temp\CLICK (00010C0A)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pdg import build_program_graph  # noqa: E402

plc = PLC(logic)

def dump(label):
    t = plc.state.tags
    print(f"{label}: step={t.get('fill_stepNumber')} even={t.get('calc_isFillStepEven')} "
          f"subStatus={t.get('fill_subStatus')} oneshot={t.get('c_subStatusOneShot')} "
          f"fillOff={t.get('sub_fillOff')} fillOn={t.get('sub_fillOn')} fillFilling={t.get('sub_fillFilling')}")

dump("init")
plc.step()
dump("scan 1 (no inputs)")

plc.patch({"HMI_on": True})
plc.step()
dump("scan 2 (HMI_on=True)")

plc.step()
dump("scan 3")

plc.step()
dump("scan 4")

plc.step()
dump("scan 5")

plc.step()
dump("scan 6")

# Now try what happens if we set fill_subStatus = 1
plc2 = PLC(logic)
plc2.step()
plc2.patch({"HMI_on": True})
plc2.step()
plc2.step()
plc2.step()  # fill_stepNumber should be 3
print(f"\n--- fork at step={plc2.state.tags.get('fill_stepNumber')} ---")

# Force fill_subStatus = 1 to simulate the sub_fillOn timer completing
fork = plc2.fork()
fork.patch({"fill_subStatus": 1})
fork.step()
dump_fork = lambda label: print(f"{label}: step={fork.state.tags.get('fill_stepNumber')} "
    f"even={fork.state.tags.get('calc_isFillStepEven')} "
    f"subStatus={fork.state.tags.get('fill_subStatus')} "
    f"oneshot={fork.state.tags.get('c_subStatusOneShot')}")
dump_fork("after forcing fill_subStatus=1")
fork.step()
dump_fork("next scan")
fork.step()
dump_fork("next scan")
