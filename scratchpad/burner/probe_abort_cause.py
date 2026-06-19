"""Fast, focused probe: why does the work fork fall out of Starting(3)?

Two experiments, no how() (so ~seconds, not 244s):

  EXP-A  Drive to Starting(3), then FORCE the walker's actual holds
         (C_Clear, C_Reset, C_Start, C_ProductionMode, x_BlowerFB) true and
         step ~80 scans.  Does S_StateCurrent self-abort with NO C_Abort?
         Tests the "held commands / alarm" hypothesis.

  EXP-B  Drive to Starting(3), pulse C_Abort once, confirm 3->8->9, then
         trace cause(S_StateCurrent, scan=leaving) and the rules._walk_chain
         recursion with a hand-rolled tracer that prints every level so we
         can see exactly where the chain dies (trigger side vs enabler side).
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

from pyrung.core.analysis.pdg import build_program_graph  # noqa: E402
from pyrung.core.analysis.walk.passes import run_walk_passes  # noqa: E402
from pyrung.core.analysis.walk.priors import _external_bool_inputs, _edge_tags  # noqa: E402
from pyrung.core.analysis.walk.base import _values_match  # noqa: E402


PHYS = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_RotateSensor": False,
    "x_SailRelay": True,
}

WALKER_HOLDS = {
    "C_Clear": True,
    "C_Reset": True,
    "C_Start": True,
    "C_ProductionMode": True,
    "x_BlowerFB": True,
}


def g(plc: PLC, name: str) -> Any:
    return plc.state.tags.get(name, "<?>")


def drive_to_starting(plc: PLC) -> None:
    for k, v in PHYS.items():
        plc.force(k, v)
    plc.step()
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step()
    plc.step()
    for cmd in ("C_Clear", "C_Reset", "C_Start"):
        plc.patch({cmd: True})
        plc.step()
        plc.step()
        plc.step()
        plc.step()
        plc.step()


def transitions(plc: PLC, tag: str) -> list[tuple[int, Any, Any]]:
    h = plc.history
    states = h.range(h.oldest_scan_id, h.newest_scan_id + 1)
    out = []
    for a, b in zip(states, states[1:]):
        if not _values_match(a.tags.get(tag), b.tags.get(tag)):
            out.append((b.scan_id, a.tags.get(tag), b.tags.get(tag)))
    return out


# ---- hand-rolled _walk_chain tracer (mirrors rules._walk_chain) -------------

def make_actionable(plc: PLC):
    program = plc._program if hasattr(plc, "_program") else logic
    pdg = build_program_graph(program)
    advice, _journal = run_walk_passes(program, pdg)
    known = plc._known_tags_by_name
    ext_inputs = set(_external_bool_inputs(pdg, known, program, advice=advice))
    edge_ext = _edge_tags(pdg, program) & ext_inputs

    def actionable(name: str) -> bool:
        return name in ext_inputs or name in edge_ext or not pdg.writers_of.get(name)

    return actionable, pdg, ext_inputs


def trace_chain(plc, chain, actionable, depth=0, seen=None):
    pad = "  " * depth
    if seen is None:
        seen = set()
    key = (chain.effect.tag_name, chain.effect.scan_id)
    if key in seen:
        print(f"{pad}(seen {key})")
        return
    seen.add(key)
    e = chain.effect
    print(f"{pad}* {e.tag_name}@{e.scan_id}: {e.from_value!r}->{e.to_value!r} [{chain.mode}]")
    for i, step in enumerate(chain.steps):
        trg = [f"{t.tag_name}@{t.scan_id}:{t.from_value!r}->{t.to_value!r}" for t in step.triggers]
        enb = [f"{en.tag_name}={en.value!r}@{en.held_since_scan}" for en in step.enablers]
        print(f"{pad}  step{i} rung={step.rung_index} sub={step.subroutine} trig={trg} enb={enb}")
    # expand triggers (mirror _walk_chain)
    for step in chain.steps:
        for t in step.triggers:
            if actionable(t.tag_name):
                print(f"{pad}  -> ROOT(trigger actionable) {t.tag_name}={t.to_value!r}")
                continue
            try:
                sub = plc.cause(t.tag_name, scan=t.scan_id)
            except Exception as exc:  # noqa: BLE001
                print(f"{pad}  -> cause({t.tag_name}@{t.scan_id}) raised {type(exc).__name__}: {exc}")
                continue
            if sub is None:
                print(f"{pad}  -> cause({t.tag_name}@{t.scan_id}) None; ROOT(trigger) {t.tag_name}={t.to_value!r}")
                continue
            trace_chain(plc, sub, actionable, depth + 2, seen)
    # enabler fallback approximation (only show what _skip_enabler would keep)
    for step in chain.steps:
        if step.triggers:
            continue
        for en in step.enablers:
            nm = en.tag_name
            skip = nm.startswith("S_") or nm.endswith(("_StateCurrent", "_UnitModeCurrent", "_CurStep"))
            tag = "SKIP" if skip else "FALLBACK"
            print(f"{pad}  enb-fallback[{tag}] {nm}={en.value!r}@{en.held_since_scan}")
            if skip:
                continue
            try:
                sub = plc.cause(nm, scan=en.held_since_scan) if en.held_since_scan is not None else plc.cause(nm)
            except Exception as exc:  # noqa: BLE001
                print(f"{pad}    cause({nm}@{en.held_since_scan}) raised {type(exc).__name__}: {exc}")
                continue
            if sub is None:
                print(f"{pad}    cause({nm}@{en.held_since_scan}) None -> HELD root {nm}={en.value!r}")
                continue
            trace_chain(plc, sub, actionable, depth + 2, seen)


def exp_a() -> None:
    print("\n================ EXP-A: hold walker holds, NO C_Abort ================")
    plc = PLC(logic)
    drive_to_starting(plc)
    print(f"after drive: S_StateCurrent={g(plc,'S_StateCurrent')!r} (expect 3)")
    # release pulses, then force the walker's actual holds going forward
    for k, v in WALKER_HOLDS.items():
        plc.force(k, v)
    start = plc.history.newest_scan_id
    for _ in range(80):
        plc.step()
        cur = g(plc, "S_StateCurrent")
        if cur != 3:
            print(f"  SELF-ABORTED to {cur!r} at +{plc.history.newest_scan_id - start} scans "
                  f"(C_CtrlCmd={g(plc,'C_CtrlCmd')!r} C_Abort={g(plc,'C_Abort')!r} "
                  f"isCmdValid_Yes={g(plc,'isCmdValid_Yes')!r})")
            break
    else:
        print(f"  stayed in Starting(3) for 80 scans; no self-abort. "
              f"C_CtrlCmd={g(plc,'C_CtrlCmd')!r} S_StateRequested={g(plc,'S_StateRequested')!r}")


def exp_b() -> None:
    print("\n================ EXP-B: pulse C_Abort, trace cause ================")
    plc = PLC(logic)
    drive_to_starting(plc)
    print(f"after drive: S_StateCurrent={g(plc,'S_StateCurrent')!r} (expect 3)")
    plc.patch({"C_Abort": True})
    for _ in range(6):
        plc.step()
    print(f"S_StateCurrent transitions: {transitions(plc,'S_StateCurrent')[-5:]}")
    leaving = None
    for scan, prev, cur in transitions(plc, "S_StateCurrent"):
        if _values_match(prev, 3) and not _values_match(cur, 3):
            leaving = scan
    print(f"leaving-committed (from 3) scan = {leaving}")
    if leaving is None:
        return
    actionable, _pdg, ext = make_actionable(plc)
    print(f"C_Abort actionable={ 'C_Abort' in ext } ; C_CtrlCmd actionable={actionable('C_CtrlCmd')}")
    print("\n-- cause(S_StateCurrent, scan=leaving) recursion --")
    chain = plc.cause("S_StateCurrent", scan=leaving)
    if chain is None:
        print("cause None")
        return
    trace_chain(plc, chain, actionable)
    print("\n-- direct: cause(S_StateRequested, scan=leaving) --")
    sr = plc.cause("S_StateRequested", scan=leaving)
    if sr is not None:
        trace_chain(plc, sr, actionable)
    else:
        print("cause(S_StateRequested) None")
    print("\n-- direct: cause(C_CtrlCmd, scan=leaving) --")
    cc = plc.cause("C_CtrlCmd", scan=leaving)
    if cc is not None:
        trace_chain(plc, cc, actionable)
    else:
        print("cause(C_CtrlCmd) None")


def exp_c() -> None:
    print("\n================ EXP-C: end-of-scan-19 state + writer re-eval ================")
    from pyrung.core.analysis.causal.support import _HistoricalView
    from pyrung.core.analysis.sp_tree import evaluate_sp
    from pyrung.core.analysis.pdg import resolve_rung

    plc = PLC(logic)
    drive_to_starting(plc)
    plc.patch({"C_Abort": True})
    for _ in range(6):
        plc.step()
    # find leaving scan
    leaving = None
    for scan, prev, cur in transitions(plc, "S_StateCurrent"):
        if _values_match(prev, 3) and not _values_match(cur, 3):
            leaving = scan
    print(f"leaving scan = {leaving}")
    state = plc.history.at(leaving)
    for t in ("C_CtrlCmd", "C_Abort", "S_Starting", "S_Aborting", "S_StateCurrent",
              "S_StateRequested", "isCmdValid_Yes", "isStateEnbl_Yes", "C_CmdChgRequestBool"):
        print(f"  end-of-scan {t} = {state.tags.get(t)!r}")

    pdg = build_program_graph(logic)
    view = _HistoricalView(state)

    def _eval(cond):
        return cond.evaluate(view)

    print("\n  writers_of[S_StateRequested] re-eval against end-of-scan state:")
    for node_idx in sorted(pdg.writers_of.get("S_StateRequested", ())):
        node = pdg.rung_nodes[node_idx]
        rung = resolve_rung(logic, node)
        if rung is None:
            continue
        sp = rung.sp_tree()
        ok = sp is None or evaluate_sp(sp, _eval)
        print(f"    {node.subroutine}[r{node.rung_index}] sp_true={ok}")


def exp_d() -> None:
    print("\n================ EXP-D: reconstruct PRE-capture-rung state ================")
    from pyrung.core.analysis.causal.support import _HistoricalView
    from pyrung.core.analysis.sp_tree import evaluate_sp
    from pyrung.core.analysis.pdg import resolve_rung

    plc = PLC(logic)
    drive_to_starting(plc)
    plc.patch({"C_Abort": True})
    for _ in range(6):
        plc.step()
    leaving = None
    for scan, prev, cur in transitions(plc, "S_StateCurrent"):
        if _values_match(prev, 3) and not _values_match(cur, 3):
            leaving = scan
    print(f"leaving scan = {leaving}")
    pdg = build_program_graph(logic)

    def reconstruct_pre_rung(scan: int, capture_idx: int) -> dict:
        """state at start of scan, folded with main rungs [0, capture_idx)."""
        base = dict(plc.history.at(scan - 1).tags)
        firings = plc.rung_firings(scan)
        for ridx in sorted(firings):
            if ridx >= capture_idx:
                break
            for tag, val in firings[ridx].items():
                base[tag] = val
        return base

    def eval_writers(tag: str, scan: int, capture_idx: int):
        pre = reconstruct_pre_rung(scan, capture_idx)

        class _D:
            def get_tag(self, n, d=None):
                return pre.get(n, d)

            def get_memory(self, n, d=None):
                return pre.get(n, d)

        # _HistoricalView wraps a state object; build a tiny shim instead.
        from pyrung.core.state import SystemState  # noqa: F401

        def _eval(cond):
            return cond.evaluate(_view)

        # Use a SystemState-like view: _HistoricalView expects .tags mapping.
        class _StateLike:
            tags = pre

        _view = _HistoricalView(_StateLike())
        print(f"  reconstructed pre-rung[{capture_idx}] for {tag}: "
              f"C_CtrlCmd={pre.get('C_CtrlCmd')!r} C_Abort={pre.get('C_Abort')!r} "
              f"S_Starting={pre.get('S_Starting')!r} S_StateCurrent={pre.get('S_StateCurrent')!r}")
        for node_idx in sorted(pdg.writers_of.get(tag, ())):
            node = pdg.rung_nodes[node_idx]
            if capture_idx not in pdg.timeline_capture_indices_for_node(node_idx):
                continue
            rung = resolve_rung(logic, node)
            if rung is None:
                continue
            sp = rung.sp_tree()
            ok = sp is None or evaluate_sp(sp, _eval)
            if ok:
                print(f"    WRITER (sp_true) {node.subroutine}[r{node.rung_index}]")

    firings = plc.rung_firings(leaving)
    sr_cap = [i for i in firings if firings[i].get("S_StateRequested") == 8]
    cc_cap = [i for i in firings if firings[i].get("C_CtrlCmd") == 8]
    print(f"capture rung for S_StateRequested=8: {sr_cap}; for C_CtrlCmd=8: {cc_cap}")
    if sr_cap:
        print("\n hop1: S_StateRequested=8")
        eval_writers("S_StateRequested", leaving, sr_cap[0])
    if cc_cap:
        print("\n hop2: C_CtrlCmd=8")
        eval_writers("C_CtrlCmd", leaving, cc_cap[0])


def main() -> int:
    print(f"CLICK_PROJECT={CLICK_PROJECT}")
    exp_a()
    exp_b()
    exp_c()
    exp_d()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
