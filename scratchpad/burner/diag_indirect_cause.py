"""Instrument: what does cause() see at the indirect-copy boundary?

Questions to answer:
1. Does ds._get_tag(214) return 'A_Alm14_Status' or 'DS214'?
2. Does cause(A_Alm14_Status) find writers? What's in writers_of?
3. Do pack_bits/unpack_to_words hops work through existing footprint-diff?
"""
from __future__ import annotations
import os, sys
from pathlib import Path
CLICK_PROJECT = Path(os.environ.get("PYRUNG_CLICK_PROJECT",
    r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project"))
sys.path.insert(0, str(CLICK_PROJECT))
from main import logic  # noqa: E402
from tags import ds  # noqa: E402
from pyrung import PLC  # noqa: E402


def pulse(plc, name, settle=4):
    plc.patch({name: True}); plc.step()
    for _ in range(settle):
        plc.step()


def drive_to_abort(plc):
    plc.step()
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step(); plc.step()
    pulse(plc, "C_Clear"); pulse(plc, "C_Reset")
    plc.patch({"x_BlowerFB": True, "x_RotateFB": True})
    pulse(plc, "C_Start")
    for _ in range(60):
        plc.step()
        if plc.state.tags.get("S_StateCurrent") in (6, 8, 9):
            return plc.state.scan_id
    return None


def main():
    plc = PLC(logic)
    abort_scan = drive_to_abort(plc)
    state = plc.state
    print(f"Landed: S_StateCurrent={state.tags.get('S_StateCurrent')} scan={abort_scan}")
    print()

    # Q1: What does ds._get_tag(214) return?
    tag_214 = ds._get_tag(214)
    print(f"Q1: ds._get_tag(214) = {tag_214!r}  name={tag_214.name!r}")
    print(f"    A_Alm14_Status value at abort: {state.tags.get('A_Alm14_Status', '<missing>')}")
    print(f"    DS214 value at abort:          {state.tags.get('DS214', '<missing>')}")
    print(f"    tag_214.name value at abort:   {state.tags.get(tag_214.name, '<missing>')}")
    print()

    # Q2: What's in writers_of for A_Alm14_Status vs DS214?
    pdg = plc._ensure_pdg()
    for name in ["A_Alm14_Status", "DS214", tag_214.name]:
        writers = pdg.writers_of.get(name, frozenset())
        print(f"Q2: writers_of[{name!r}] = {len(writers)} nodes: {sorted(writers)}")
        if writers:
            for nidx in sorted(writers):
                node = pdg.rung_nodes[nidx]
                print(f"      node[{nidx}]: rung={node.rung_index} sub={node.subroutine} "
                      f"data_reads={sorted(node.data_reads)[:5]}{'...' if len(node.data_reads) > 5 else ''}")
    print()

    # Q3: What does cause(A_Alm14_Status) return?
    chain = plc.cause("A_Alm14_Status", scan=abort_scan)
    if chain is None:
        print("Q3: cause(A_Alm14_Status) returned None — no transition found")
    else:
        print(f"Q3: cause(A_Alm14_Status) -> {len(chain.steps)} steps, "
              f"{len(chain.conjunctive_roots)} roots, mode={chain.mode}")
        for step in chain.steps:
            print(f"    step: rung={step.rung_index} sub={step.subroutine} "
                  f"triggers={[t.tag_name for t in step.triggers]} "
                  f"enablers={[e.tag_name for e in step.enablers]}")
        for r in chain.conjunctive_roots:
            print(f"    root: {r.tag_name} {r.from_value}->{r.to_value} @scan={r.scan_id}")
    print()

    # Q4: What does cause see for the intermediate tags?
    for tag_name in ["AlmHistorian__bit2status", "AlmHistorian__packedbit"]:
        val = state.tags.get(tag_name, "<missing>")
        chain2 = plc.cause(tag_name, scan=abort_scan)
        if chain2 is None:
            print(f"Q4: cause({tag_name}) = None (val={val})")
        else:
            print(f"Q4: cause({tag_name}) -> {len(chain2.steps)} steps, "
                  f"{len(chain2.conjunctive_roots)} roots (val={val})")
            for step in chain2.steps:
                print(f"    step: rung={step.rung_index} sub={step.subroutine} "
                      f"triggers={[t.tag_name for t in step.triggers]} "
                      f"enablers={[e.tag_name for e in step.enablers]}")
            for r in chain2.conjunctive_roots:
                print(f"    root: {r.tag_name} {r.from_value}->{r.to_value}")
    print()

    # Q5: find the actual transition scan for A_Alm14_Status
    print("Q5: scanning for A_Alm14_Status transition...")
    ids = plc.history.scan_ids()
    for i in range(len(ids) - 1, 0, -1):
        cur = plc.history.at(ids[i]).tags.get("A_Alm14_Status")
        prev = plc.history.at(ids[i - 1]).tags.get("A_Alm14_Status")
        if cur != prev:
            print(f"    transition at scan {ids[i]}: {prev} -> {cur}")
            # Try cause at the correct scan
            chain5 = plc.cause("A_Alm14_Status", scan=ids[i])
            if chain5 is None:
                print(f"    cause(A_Alm14_Status, scan={ids[i]}) = None")
            else:
                print(f"    cause -> {len(chain5.steps)} steps, {len(chain5.conjunctive_roots)} roots")
                for step in chain5.steps:
                    print(f"      step: rung={step.rung_index} sub={step.subroutine} "
                          f"triggers={[t.tag_name for t in step.triggers]}")
                for r in chain5.conjunctive_roots:
                    print(f"      root: {r.tag_name} {r.from_value}->{r.to_value}")
            # Check pointer at that scan
            st = plc.history.at(ids[i])
            ptr = st.tags.get("AlmHistorian__status_idx")
            print(f"    AlmHistorian__status_idx at scan {ids[i]} = {ptr}")
            if isinstance(ptr, (int, float)):
                print(f"      resolves to: {ds._get_tag(int(ptr)).name!r}")
            break
    else:
        print("    no transition found in history")
    print()

    # Q6: what subroutine nodes exist in the PDG? Any indirect-write nodes?
    print("Q6: subroutine nodes that write to ds range:")
    for nidx, node in enumerate(pdg.rung_nodes):
        if node.subroutine == "AlmHistorian" and node.writes:
            # Check if any writes overlap with A_Alm14_Status
            has_target = "A_Alm14_Status" in node.writes
            n_writes = len(node.writes)
            print(f"    node[{nidx}]: rung={node.rung_index} sub={node.subroutine} "
                  f"writes={n_writes} tags, has_A_Alm14_Status={has_target}, "
                  f"data_reads={sorted(node.data_reads)[:4]}{'...' if len(node.data_reads) > 4 else ''}")
    print()

    # Q7: main-scope rung 73 — what is it?
    print("Q7: main-scope rung 73 (node 74):")
    node74 = pdg.rung_nodes[74]
    print(f"    writes: {len(node74.writes)} tags")
    print(f"    sample writes: {sorted(node74.writes)[:5]}")
    print(f"    data_reads: {sorted(node74.data_reads)}")
    print(f"    calls: {node74.calls}")
    if plc._program:
        rung73 = plc._program.rungs[73]
        print(f"    instructions: {[type(i).__name__ for i in rung73._instructions]}")


    # Q8: indirect_writes descriptors from PDG
    print(f"Q8: pdg.indirect_writes = {len(pdg.indirect_writes)} entries")
    for iw in pdg.indirect_writes[:10]:
        node = pdg.rung_nodes[iw.node_index]
        print(f"    node[{iw.node_index}] rung={node.rung_index} sub={node.subroutine} "
              f"ptr={iw.pointer_tag} src={sorted(iw.source_tags)} block={iw.block.name}")
    print()

    # Q9: find the transition scan where pointer resolves to A_Alm14_Status (ds[214])
    print("Q9: all A_Alm14_Status transitions:")
    for i in range(1, len(ids)):
        cur = plc.history.at(ids[i]).tags.get("A_Alm14_Status")
        prev = plc.history.at(ids[i - 1]).tags.get("A_Alm14_Status")
        if cur != prev:
            st = plc.history.at(ids[i])
            ptr = st.tags.get("AlmHistorian__status_idx")
            resolved = ds._get_tag(int(ptr)).name if isinstance(ptr, (int, float)) and ptr else "?"
            print(f"    scan {ids[i]}: {prev} -> {cur}  ptr={ptr} -> {resolved}")
            chain9 = plc.cause("A_Alm14_Status", scan=ids[i])
            if chain9 is None:
                print(f"      cause = None")
            else:
                print(f"      cause = {len(chain9.steps)} steps, {len(chain9.conjunctive_roots)} roots")
                for step in chain9.steps:
                    print(f"        step: rung={step.rung_index} sub={step.subroutine} "
                          f"triggers={[t.tag_name for t in step.triggers]} "
                          f"enablers={[e.tag_name for e in step.enablers]}")
                for r in chain9.conjunctive_roots:
                    print(f"        root: {r.tag_name} {r.from_value}->{r.to_value}")


if __name__ == "__main__":
    main()
