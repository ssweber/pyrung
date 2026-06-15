"""Debug: run projected_cause on fill_stepNumber -> 4 at the stuck state."""

import sys

PROJECT = r"C:\Users\ssweb\AppData\Local\Temp\CLICK (00010C0A)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pdg import build_program_graph  # noqa: E402

plc = PLC(logic)
plc.step()
plc.patch({"HMI_on": True})
plc.step()
plc.step()
plc.step()
# Now step once more to let calc_isFillStepEven recompute (3%2=1)
plc.step()

print(f"fill_stepNumber = {plc.state.tags.get('fill_stepNumber')}")
print(f"calc_isFillStepEven = {plc.state.tags.get('calc_isFillStepEven')}")
print(f"c_subStatusOneShot = {plc.state.tags.get('c_subStatusOneShot')}")
print(f"fill_subStatus = {plc.state.tags.get('fill_subStatus')}")

pdg = build_program_graph(plc._program)

from pyrung.core.analysis.causal.projected import projected_cause  # noqa: E402

chain = projected_cause(
    logic=plc._logic,
    history=plc._history,
    tag="fill_stepNumber",
    to_value=4,
    pdg=pdg,
    timelines=plc._rung_firing_timelines,
    program=plc._program,
)

print(f"\n=== projected_cause(fill_stepNumber, to=4) ===")
print(f"mode: {chain.mode}")
print(f"steps: {len(chain.steps)}")
for step in chain.steps:
    print(f"  step: {step.transition.tag_name}={step.transition.to_value} "
          f"kind={step.kind}")
    for trig in step.triggers:
        print(f"    trigger: {trig.tag_name}={trig.to_value}")
    for en in step.enablers:
        print(f"    enabler: {en.tag_name}={en.to_value}")

blockers = getattr(chain, "blockers", ())
print(f"blockers: {len(blockers)}")
for b in blockers:
    print(f"  blocker: {b.blocked_tag}={b.needed_value}")
    rel = getattr(b, "relation", None)
    if rel:
        print(f"    relation: {rel}")
    subs = getattr(b, "sub_blockers", ())
    for s in subs:
        print(f"    sub: {s.blocked_tag}={s.needed_value}")
