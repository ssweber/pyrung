"""Instrument: does the pilot correctly identify the *advance trigger* per edge?

For the governing register (S_StateCurrent), enumerate every static ingress
route and classify its trigger:

  - COMMAND   : route has steerable action_tags (e.g. C_CtrlCmd) -> the pilot
                must STEER it.  Identified transparently from the trace.
  - COMPLETION: no steerable actions, gated by a completion condition
                (e.g. S_StateCompleteBool / S_StateComplete) -> the pilot must
                COAST (let-run) until that gate goes true.
  - OPAQUE    : destination_value is None (indirect jump table) -> trace returns
                UNKNOWN; only sandbox can resolve the landing value.

Then, at the Starting state, transparently trace the completion trigger
(S_StateCompleteBool == 1) to see whether the advanced trace surfaces the
*enabling frontier* (Blower__init / Rotate__init) and descends into the
self-advancing leaves (timers) + steerable leaves (feedback) -- the thing the
informed let-run needs.

Sometimes the trigger is "StateRequested + CtrlCmd" (command), sometimes it is
just "StateComplete" (completion).  This prints which one the pilot lands on.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

CLICK_PROJECT = Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project",
    )
)
sys.path.insert(0, str(CLICK_PROJECT))

from main import logic  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pdg import build_program_graph  # noqa: E402
from pyrung.core.analysis.pilot.compass import detect_opaque_loop  # noqa: E402
from pyrung.core.analysis.pilot.evidence import (  # noqa: E402
    build_transition_evidence,
    expand_routes,
    infer_pipeline_roles,
)
from pyrung.core.analysis.pilot.physical import install_harness  # noqa: E402
from pyrung.core.analysis.pilot.pilot import _build_pilot_context  # noqa: E402
from pyrung.core.analysis.pilot.trace import (  # noqa: E402
    compute_reference_constants,
    compute_steerable,
    trace_back,
)

PHYSICAL_PERMISSIVES = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_RotateSensor": False,
    "x_SailRelay": True,
}

GOVERNING_TAG = "S_StateCurrent"
COMPLETION_GATES = {"S_StateCompleteBool", "S_StateComplete"}


def _known(plc: PLC, tag: str) -> bool:
    return tag in plc._known_tags_by_name or tag in plc.state.tags


def _pulse(plc: PLC, tag: str, settle: int = 8) -> None:
    plc.patch({tag: True})
    plc.step()
    for _ in range(settle):
        plc.step()


def _classify(route: Any, steerable: frozenset[str]) -> str:
    steer_actions = set(route.action_tags) & steerable
    gates = {t for t, _ in (*route.enablers, *route.call_site_gates)}
    parts = []
    if route.destination_value is None:
        parts.append("OPAQUE(sandbox)")
    if steer_actions:
        parts.append(f"COMMAND[{','.join(sorted(steer_actions))}]")
    if gates & COMPLETION_GATES:
        parts.append("COMPLETION")
    if not parts:
        parts.append("AUTO/other")
    return " ".join(parts)


def _fmt(pairs: tuple[tuple[str, Any], ...]) -> str:
    return ", ".join(f"{t}={v!r}" for t, v in pairs) or "-"


def _dump_node(node: Any, snap: dict[str, Any], *, indent: int = 0, limit: int = 80) -> int:
    flags = []
    if node.satisfied:
        flags.append("ok")
    if node.is_steerable:
        flags.append("STEER")
    if getattr(node, "self_advancing", False):
        flags.append("COAST")
    if getattr(node, "pipeline_internal", False):
        flags.append("internal")
    suffix = f" [{' '.join(flags)}]" if flags else ""
    print(f"{'  ' * indent}- {node.tag}={node.value!r} have={snap.get(node.tag)!r}{suffix}")
    count = 1
    for child in node.children:
        if count >= limit:
            print(f"{'  ' * (indent + 1)}...")
            break
        count += _dump_node(child, snap, indent=indent + 1, limit=limit - count)
    return count


def main() -> int:
    plc = PLC(logic)
    for tag, value in PHYSICAL_PERMISSIVES.items():
        if _known(plc, tag):
            plc.force(tag, value)
    plc.step()

    pdg = build_program_graph(logic)
    harness_fb = install_harness(plc)
    ref_consts = compute_reference_constants(pdg, logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic) - harness_fb - ref_consts
    opaque_loop = detect_opaque_loop(pdg, logic)

    _nd, _kc, evidence = _build_pilot_context(logic, dict(plc.state.tags))
    if evidence is None:
        evidence = build_transition_evidence(None)

    print(f"governing_tag = {GOVERNING_TAG}")
    print(f"opaque_loop   = {sorted(opaque_loop)}")
    print(f"steerable ({len(steerable)}): {sorted(steerable)[:12]}...")

    # ----- Route view: every ingress edge + trigger classification -----------
    print(f"\n=== expand_routes({GOVERNING_TAG}) — trigger per edge ===")
    routes = expand_routes(GOVERNING_TAG, pdg, logic, steerable, opaque_loop, evidence)
    print(f"{len(routes)} routes\n")
    for r in routes:
        kind = _classify(r, steerable)
        req = f"{r.request_tag}={r.request_value!r}" if r.request_tag else "(direct)"
        print(f"  [{kind}]")
        print(f"      dest={r.destination_tag}={r.destination_value!r}  via {req}  in {r.writer_subroutine}")
        print(f"      enablers   : {_fmt(r.enablers)}")
        print(f"      call_gates : {_fmt(r.call_site_gates)}")
        print(f"      actions    : {sorted(r.action_tags) or '-'}")
        print(f"      src_constr : {_fmt(r.source_constraints)}")

    # ----- Drive to Starting: enter Production mode, THEN Clear/Reset/Start ---
    # Starting (3) then dwells for hundreds of scans while the SFCs init, so a
    # snapshot right after the Start pulse lands inside the Starting window.
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step()
    plc.step()
    for cmd in ("C_Clear", "C_Reset", "C_Start"):
        _pulse(plc, cmd)
    snap = dict(plc.state.tags)

    print(f"\nAfter Production + Clear/Reset/Start: S_StateCurrent={snap.get('S_StateCurrent')} "
          f"S_Starting={snap.get('S_Starting')} S_StateCompleteBool={snap.get('S_StateCompleteBool')}")
    print(f"  Blower__init={snap.get('Blower__init')} Rotate__init={snap.get('Rotate__init')}  "
          f"Blower_CurStep={snap.get('Blower_CurStep')} Rotate_CurStep={snap.get('Rotate_CurStep')}  "
          f"Rotate_xCall={snap.get('Rotate_xCall')} Blower_xCall={snap.get('Blower_xCall')}")

    roles = []
    for tag in sorted(opaque_loop):
        if evidence is not None and not evidence.is_stepping(tag):
            continue
        role = infer_pipeline_roles(tag, pdg, logic, steerable, opaque_loop, evidence)
        if role.request_tags:
            roles.append(role)
    pit = frozenset(t for r in roles for t in r.trace_internal_tags)

    # Which writer does _rank_writers pick for S_StateCompleteBool, and is its
    # guard consistent with the held state (S_Starting=True)?
    from pyrung.core.analysis.pdg import resolve_rung  # noqa: E402
    from pyrung.core.analysis.pilot.trace import _rank_writers  # noqa: E402
    from pyrung.core.analysis.simplified import _sp_to_expr  # noqa: E402
    from pyrung.core.analysis.sp_values import _extract_condition_values  # noqa: E402

    scb_writers = pdg.writers_of.get("S_StateCompleteBool", frozenset())
    print("\n=== S_StateCompleteBool writers — guard consistency at Starting ===")
    for ri in sorted(scb_writers):
        node = pdg.rung_nodes[ri]
        ro = resolve_rung(logic, node)
        if ro is None:
            continue
        sp = ro.sp_tree()
        conds = _extract_condition_values(_sp_to_expr(sp)) if sp is not None else {}
        guard = ", ".join(
            f"{t}={sorted(vs)}{'<ok>' if snap.get(t) in vs else '<NO:%r>' % snap.get(t)}"
            for t, vs in conds.items()
        )
        print(f"  rung {ri} ({node.subroutine or 'main'}): {guard or '(always)'}")
    ranked = _rank_writers(scb_writers, pdg, logic, "S_StateCompleteBool", 1, snap, opaque_loop)
    print(f"  _rank_writers picks first: rung {ranked[0] if ranked else None} "
          f"({pdg.rung_nodes[ranked[0]].subroutine or 'main' if ranked else '-'})")

    print("\n=== trace_back(S_StateCompleteBool == 1) at Starting ===")
    print("    (does the advanced trace surface the enabling frontier + descend to")
    print("     timers/feedback?  STEER leaves = influenceable; bare leaves = self-advancing)")
    tree = trace_back(
        "S_StateCompleteBool", 1, snap, pdg, logic, steerable,
        opaque_loop=opaque_loop, pipeline_internal_tags=pit, choice=None,
    )
    _dump_node(tree, snap)

    leaves = tree.leaves()
    steer_leaves = [(n.tag, n.value) for n in leaves if n.is_steerable and not n.satisfied]
    coast_leaves = [(n.tag, n.value) for n in leaves
                    if getattr(n, "self_advancing", False) and not n.satisfied]
    bare_leaves = [(n.tag, n.value) for n in leaves
                   if not n.is_steerable and not getattr(n, "self_advancing", False)
                   and not n.satisfied]
    print(f"\n  unmet STEER  leaves (influence): {steer_leaves or '-'}")
    print(f"  unmet COAST  leaves (let-run)  : {coast_leaves or '-'}")
    print(f"  unmet bare   leaves (blocked?) : {bare_leaves or '-'}")

    # ----- Where does the descent stop before the timer/feedback leaves? -----
    # The real CurStep 1->2 advance is via Trans (R17 <- R8: CurStep==1,
    # tmr.Acc>2, i_*FB).  If these traces don't surface tmr.Acc / i_*FB, the
    # descent is blocked there.
    for tgt, val in (("Blower_CurStep", 2), ("Blower_Trans", 1), ("Rotate_Trans", 1)):
        print(f"\n=== trace_back({tgt} == {val}) at Starting ===")
        t = trace_back(
            tgt, val, snap, pdg, logic, steerable,
            opaque_loop=opaque_loop, pipeline_internal_tags=pit, choice=None,
        )
        _dump_node(t, snap)

    # ----- Advance to the STUCK sub-state (Blower_CurStep == 1) and re-trace --
    # The even-step rung only self-blocks at an odd step; the affine fix should
    # now pick Blower_Trans here and surface Blower_tmr_Acc=2 [COAST].
    for _ in range(800):
        t = plc.state.tags
        # Stable stuck state: CurStep odd AND valstepisodd settled to 1 (it lags
        # CurStep by a scan), so the even-step rung is genuinely self-blocked.
        if t.get("Blower_CurStep") == 1 and t.get("Blower__valstepisodd") == 1:
            break
        plc.step()
    snap2 = dict(plc.state.tags)
    print(f"\n\n##### at Blower_CurStep=1 (scan {plc.state.scan_id}) #####")
    print(f"  Blower_CurStep={snap2.get('Blower_CurStep')} "
          f"Blower__valstepisodd={snap2.get('Blower__valstepisodd')} "
          f"Blower_tmr_Acc={snap2.get('Blower_tmr_Acc')} Blower_Trans={snap2.get('Blower_Trans')}")
    for tgt, val in (("S_StateCompleteBool", 1), ("Blower_CurStep", 2)):
        print(f"\n=== trace_back({tgt} == {val}) at Blower_CurStep=1 ===")
        t = trace_back(
            tgt, val, snap2, pdg, logic, steerable,
            opaque_loop=opaque_loop, pipeline_internal_tags=pit, choice=None,
        )
        _dump_node(t, snap2)

    # ----- Debug: how does _rank_writers rank Blower_CurStep=2 writers? -------
    from pyrung.core.analysis.pilot.trace import (  # noqa: E402
        _guard_self_blocked, _rank_writers, _written_value_for_tag,
    )
    from pyrung.core.crossing import Affine  # noqa: E402

    print("\n=== Blower_CurStep=2 writers @ Blower_CurStep=1 ===")
    bw = pdg.writers_of.get("Blower_CurStep", frozenset())
    for ri in sorted(bw):
        node = pdg.rung_nodes[ri]
        ro = resolve_rung(logic, node)
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, "Blower_CurStep")
        is_aff = isinstance(wv, Affine)
        src = wv.source if is_aff else None
        sb = _guard_self_blocked(ro, "Blower_CurStep", snap2, pdg, logic) if is_aff else False
        sp = ro.sp_tree()
        from pyrung.core.analysis.simplified import _sp_to_expr as _s2e
        from pyrung.core.analysis.sp_values import _extract_condition_values as _ecv
        conds = _ecv(_s2e(sp)) if sp is not None else {}
        print(f"  rung {ri} ({node.subroutine}): wv={type(wv).__name__} src={src} "
              f"self_blocked={sb}  guard_eq={dict(conds)}")
    ranked = _rank_writers(bw, pdg, logic, "Blower_CurStep", 2, snap2, opaque_loop)
    print(f"  ranked: {ranked}  -> picks {ranked[0] if ranked else None}")

    # Deep-dive rung 137 (even-step) guard atoms.
    from pyrung.core.analysis.pilot.trace import _guard_atoms, _expr_satisfied  # noqa: E402
    ro137 = resolve_rung(logic, pdg.rung_nodes[137])
    print("\n  rung 137 guard atoms:")
    for atom in _guard_atoms(_s2e(ro137.sp_tree())):
        print(f"    tag={atom.tag!r} form={atom.form!r} operand={getattr(atom,'operand',None)!r} "
              f"satisfied={_expr_satisfied(atom, snap2)}")
    vw = pdg.writers_of.get("Blower__valstepisodd", frozenset())
    print(f"  Blower__valstepisodd writers: {sorted(vw)}")
    for wi in sorted(vw):
        wn = pdg.rung_nodes[wi]
        reads = wn.condition_reads | wn.data_reads | wn.exclusive_reads
        print(f"    rung {wi}: reads={sorted(reads)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
