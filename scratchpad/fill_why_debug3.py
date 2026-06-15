"""Debug: examine the SP-tree of R3 and what why() sees."""

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

print(f"fill_stepNumber = {plc.state.tags.get('fill_stepNumber')}")
print(f"calc_isFillStepEven = {plc.state.tags.get('calc_isFillStepEven')}")

program = getattr(plc, "_program")
pdg = build_program_graph(program)

# Look at writers of fill_stepNumber
writers = pdg.writers_of.get("fill_stepNumber", frozenset())
print(f"\nWriters of fill_stepNumber: {writers}")

for ri in writers:
    node = pdg.rung_nodes[ri]
    from pyrung.core.analysis.pdg import resolve_rung
    rung = resolve_rung(program, node)
    sp = rung.sp_tree()
    print(f"\n--- Rung {ri} (sub={node.subroutine}) ---")
    print(f"  SP-tree: {sp}")
    print(f"  ote_writes: {node.ote_writes}")

    # Check what _collect_sp_leaves returns
    from pyrung.core.analysis.causal.why import _collect_sp_leaves
    if sp is not None:
        leaves = _collect_sp_leaves(sp)
        print(f"  SP leaves ({len(leaves)}):")
        for leaf in leaves:
            cond = leaf.condition
            tag_name = None
            if hasattr(cond, "target") and hasattr(cond.target, "name"):
                tag_name = cond.target.name
            elif hasattr(cond, "left") and hasattr(cond.left, "name"):
                tag_name = cond.left.name
            print(f"    leaf: {leaf} cond={cond} tag={tag_name}")

    # Evaluate the rung
    from pyrung.core.analysis.causal.support import _HistoricalView
    view = _HistoricalView(plc.state)

    from pyrung.core.analysis.sp_tree import evaluate_sp
    fires = evaluate_sp(sp, lambda c: c.evaluate(view))
    print(f"  fires: {fires}")

# Also check: what are the writers of c_subStatusOneShot?
print(f"\nWriters of c_subStatusOneShot: {pdg.writers_of.get('c_subStatusOneShot', frozenset())}")
print(f"Writers of calc_isFillStepEven: {pdg.writers_of.get('calc_isFillStepEven', frozenset())}")
