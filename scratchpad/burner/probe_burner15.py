"""Probe 15: what does the recovery oracle see at the stuck-Resetting state?

Reproduce phase C's stuck state by hand (mode 3, pulse C_Clear then C_Reset
-> S_StateCurrent parks at 15 because production_states never runs outside
mode 1), then interrogate cause(S_StateCurrent, to=4) — the exact call
_recheck_prereqs makes when _recover hits this state.  Phase C of probe14
died with "no-recovery-goals", i.e. this call returned nothing minable.
"""

import sys

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (0032023C)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402

from pyrung import PLC  # noqa: E402

plc = PLC(logic)
plc.step()
plc.patch({"C_Clear": True})
plc.step()
plc.step()
print(f"after C_Clear: S_StateCurrent={plc.state.tags['S_StateCurrent']}")
plc.patch({"C_Reset": True})
plc.step()
for _ in range(10):
    plc.step()
print(f"after C_Reset + settle: S_StateCurrent={plc.state.tags['S_StateCurrent']}")
print(f"S_UnitModeCurrent={plc.state.tags['S_UnitModeCurrent']}")
print(f"S_StateRequested={plc.state.tags['S_StateRequested']}")
print()


def dump_chain(label, chain):
    print(f"--- {label} ---")
    if chain is None:
        print("  chain is None")
        return
    print(f"  type={type(chain).__name__}")
    steps = getattr(chain, "steps", ())
    print(f"  steps: {len(steps)}")
    for i, step in enumerate(steps):
        trigs = [(t.tag_name, t.to_value) for t in step.triggers]
        print(f"    step {i}: triggers={trigs}")
    blockers = getattr(chain, "blockers", ())
    print(f"  blockers: {len(blockers)}")
    for b in blockers:
        subs = [(s.blocked_tag, s.needed_value) for s in getattr(b, "sub_blockers", ())]
        print(f"    blocked_tag={b.blocked_tag!r} needed={b.needed_value!r} subs={subs}")
    print(f"  str: {str(chain)[:800]}")
    print()


for to_val in (4, 2):
    try:
        chain = plc.cause("S_StateCurrent", to=to_val)
        dump_chain(f"cause(S_StateCurrent, to={to_val})", chain)
    except Exception as e:  # noqa: BLE001
        print(f"cause(S_StateCurrent, to={to_val}) RAISED {type(e).__name__}: {e}")

# The intermediate links the oracle would need to name.
for tag, to_val in (("S_StateRequested", 4), ("S_StateComplete", True), ("S_StateCompleteBool", 1)):
    try:
        chain = plc.cause(tag, to=to_val)
        dump_chain(f"cause({tag}, to={to_val})", chain)
    except Exception as e:  # noqa: BLE001
        print(f"cause({tag}, to={to_val}) RAISED {type(e).__name__}: {e}")
