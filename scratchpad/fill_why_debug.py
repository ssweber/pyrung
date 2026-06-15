"""Debug: run why() on fill_stepNumber at value 3 to see what the frontier-
terminated regression surfaces as sub-goals.
"""

import sys

PROJECT = r"C:\Users\ssweb\AppData\Local\Temp\CLICK (00010C0A)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402
from tags import fill_stepNumber, HMI_on  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pdg import build_program_graph  # noqa: E402

plc = PLC(logic)
plc.step()

# Drive to fill_stepNumber == 3 (same as the walk does)
plc.patch({"HMI_on": True})
plc.step()
plc.step()
plc.step()

print(f"fill_stepNumber = {plc.state.tags.get('fill_stepNumber')}")
print(f"c_subStatusOneShot = {plc.state.tags.get('c_subStatusOneShot')}")
print(f"calc_isFillStepEven = {plc.state.tags.get('calc_isFillStepEven')}")
print(f"fill_subStatus = {plc.state.tags.get('fill_subStatus')}")

program = getattr(plc, "_program")
pdg = build_program_graph(program)

# Get the walk's ext_inputs and edge_ext for frontier
from pyrung.core.analysis.walk.priors import _external_bool_inputs, _edge_tags  # noqa: E402
from pyrung.core.analysis.walk.passes import run_walk_passes  # noqa: E402

advice, journal = run_walk_passes(program, pdg)
known = plc._known_tags_by_name
ext_inputs = _external_bool_inputs(pdg, known, program, advice=advice)
edge_ext = _edge_tags(pdg, program) & set(ext_inputs)

print(f"\next_inputs: {ext_inputs}")
print(f"edge_ext: {edge_ext}")

# Run why_cause with frontier
from pyrung.core.analysis.causal.why import why_cause  # noqa: E402

def frontier(name):
    if name == "fill_stepNumber":
        return False
    return name in set(ext_inputs) | edge_ext

chain = why_cause(
    logic=plc._logic,
    state=plc.state,
    tags=["fill_stepNumber"],
    pdg=pdg,
    program=program,
    frontier=frontier,
)

print(f"\n=== why(fill_stepNumber) at value 3 ===")
print(f"conjunctive_roots: {[(r.tag_name, r.to_value) for r in chain.conjunctive_roots]}")
print(f"ambiguous_roots: {[(r.tag_name, r.to_value) for r in chain.ambiguous_roots]}")
for step in chain.steps:
    print(f"  step: {step.transition.tag_name}={step.transition.to_value} kind={step.kind} "
          f"triggers={[(t.tag_name, t.to_value) for t in step.triggers]} "
          f"enablers={[(e.tag_name, e.to_value) for e in step.enablers]}")

# Now compute goals the same way _why_regression_goals does
tags_snap = plc.state.tags
goals = []
visited_goals = frozenset()
for root in chain.conjunctive_roots:
    name = root.tag_name
    if name == "fill_stepNumber":
        continue
    current = tags_snap.get(name)
    if current is not None and not isinstance(current, bool):
        print(f"  SKIPPED (non-bool): {name}={current!r}")
        continue
    needed = not bool(current)
    key = (name, needed)
    if key in visited_goals:
        continue
    if current == needed:
        continue
    goals.append(key)

print(f"\nRegression goals: {goals}")
