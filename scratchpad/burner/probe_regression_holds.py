"""Probe why regression mining does or does not install protective holds.

Runs the burner how() probe with a wrapper around agenda.mine_regression_holds.
For each detected committed-goal regression, it dumps:

- work.cause() summary for the regressed tag
- recursive roots returned by rules._walk_chain()
- whether each root is actionable, protected, unchanged, or eligible
- the actual result returned by mine_regression_holds()
"""

from __future__ import annotations

import os
import sys
import time
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
from tags import y_BurnerLoop  # noqa: E402

from pyrung.core.analysis.walk import agenda, rules  # noqa: E402
from pyrung.core.analysis.walk.base import _StepMonitors, _values_match  # noqa: E402


ORIGINAL_MINE = agenda.mine_regression_holds


def _fmt(value: Any) -> str:
    return repr(value)


def _transition_summary(item: Any) -> str:
    return (
        f"{item.tag_name}@{item.scan_id}: "
        f"{_fmt(item.from_value)} -> {_fmt(item.to_value)}"
    )


def _dump_chain(chain: Any) -> None:
    print(f"  chain.mode={getattr(chain, 'mode', None)!r}", flush=True)
    effect = getattr(chain, "effect", None)
    if effect is not None:
        print(f"  chain.effect={_transition_summary(effect)}", flush=True)
    for i, step in enumerate(getattr(chain, "steps", ())):
        triggers = [_transition_summary(t) for t in getattr(step, "triggers", ())]
        enablers = [
            f"{e.tag_name}={getattr(e, 'value', getattr(e, 'held_value', None))!r}"
            for e in getattr(step, "enablers", ())
        ]
        print(
            f"  step {i}: rung={getattr(step, 'rung_index', None)} "
            f"triggers={triggers} enablers={enablers}",
            flush=True,
        )
    roots = [_transition_summary(t) for t in getattr(chain, "conjunctive_roots", ())]
    ambiguous = [_transition_summary(t) for t in getattr(chain, "ambiguous_roots", ())]
    print(f"  conjunctive_roots={roots}", flush=True)
    print(f"  ambiguous_roots={ambiguous}", flush=True)


def _tag_transitions(work: Any, tag: str) -> list[tuple[int, Any, Any]]:
    history = getattr(work, "_history")
    transitions: list[tuple[int, Any, Any]] = []
    states = history.range(history.oldest_scan_id, history.newest_scan_id + 1)
    for prev_state, state in zip(states, states[1:]):
        prev = prev_state.tags.get(tag)
        cur = state.tags.get(tag)
        if not _values_match(prev, cur):
            transitions.append((state.scan_id, prev, cur))
    return transitions


def _dump_roots_for_chain(ctx: Any, work: Any, chain: Any, prefix: str) -> None:
    monitors = _StepMonitors()
    protected = rules._protected_names(ctx, monitors)
    stay_context = rules._stay_context_from_monitors(monitors)
    roots = rules._walk_chain(ctx, work, chain, protected, stay_context, set(), 0)
    ext_set = set(ctx.ext_inputs) | ctx.edge_ext
    print(f"{prefix}protected={sorted(protected)}", flush=True)
    print(f"{prefix}ext_inputs_hit={sorted(set(t.tag_name for t in roots) & ext_set)}", flush=True)
    print(f"{prefix}walked_roots({len(roots)}):", flush=True)
    for root in roots:
        actionable = rules._is_actionable_root(ctx, root.tag_name)
        unchanged = root.from_value is not None and _values_match(root.from_value, root.to_value)
        reasons: list[str] = []
        if not actionable:
            reasons.append("not-actionable")
        if root.from_value is None:
            reasons.append("from-is-None")
        if unchanged:
            reasons.append("unchanged")
        if not reasons:
            reasons.append("ELIGIBLE")
        writer_count = len(ctx.pdg.writers_of.get(root.tag_name, ()))
        print(
            f"{prefix}  "
            f"{_transition_summary(root)} "
            f"actionable={actionable} ext={root.tag_name in ext_set} "
            f"writers={writer_count} reasons={reasons}",
            flush=True,
        )


def _dump_regression_scan_causes(
    ctx: Any,
    work: Any,
    regressed_goal: tuple[str, Any],
) -> None:
    tag, committed = regressed_goal
    try:
        transitions = _tag_transitions(work, tag)
    except Exception as exc:  # noqa: BLE001 - diagnostic probe
        print(f"  history transitions raised {type(exc).__name__}: {exc}", flush=True)
        return

    tail = [
        f"{scan}: {_fmt(prev)} -> {_fmt(cur)}"
        for scan, prev, cur in transitions[-8:]
    ]
    leaving = [
        (scan, prev, cur)
        for scan, prev, cur in transitions
        if _values_match(prev, committed) and not _values_match(cur, committed)
    ]
    print(f"  transitions_tail={tail}", flush=True)
    print(
        "  leaving_committed_scans="
        f"{[(scan, _fmt(prev), _fmt(cur)) for scan, prev, cur in leaving[-5:]]}",
        flush=True,
    )

    diag_tags = (
        "C_CtrlCmd", "C_Abort", "C_Stop", "C_CmdChgRequestBool", "isCmdValid_Yes",
        "A_AlmExtent", "A_Alm11_Rotate_Trig", "A_Alm12_Blower_Trig",
        "A_Alm13_Heat_Trig", "A_Alm14_DoorOpen_Trig", "A_Alm15_LintOpen_Trig",
        "A_Alm16_Sail_Trig", "x_DoorClosed", "i_DoorClosed",
        "x_LintDoorClosed", "i_LintDoorClosed", "Rotate_Error", "Blower_Error", "Heat_Error",
    )
    for scan, prev, cur in leaving[-3:]:
        print(
            f"  -- cause at leaving scan {scan}: {_fmt(prev)} -> {_fmt(cur)}",
            flush=True,
        )
        # Raw state at the leaving scan and the one before — disambiguates
        # command-pulse abort (A) vs dropped-permissive alarm abort (B).
        try:
            hist = getattr(work, "_history")
            st_prev = hist.at(scan - 1)
            st_cur = hist.at(scan)
            for name in diag_tags:
                pv = st_prev.tags.get(name, "<?>")
                cv = st_cur.tags.get(name, "<?>")
                mark = "  <== changed" if not _values_match(pv, cv) else ""
                print(f"     state {name}: {pv!r} -> {cv!r}{mark}", flush=True)
        except Exception as exc:  # noqa: BLE001 - diagnostic probe
            print(f"     state dump raised {type(exc).__name__}: {exc}", flush=True)
        for ctrl in ("C_CtrlCmd",):
            try:
                cc = work.cause(ctrl, scan=scan)
            except Exception as exc:  # noqa: BLE001 - diagnostic probe
                print(f"     cause({ctrl}@{scan}) raised {type(exc).__name__}: {exc}", flush=True)
                cc = None
            if cc is not None:
                print(f"     -- cause({ctrl}@{scan}) --", flush=True)
                _dump_chain(cc)
        try:
            chain = work.cause(tag, scan=scan)
        except Exception as exc:  # noqa: BLE001 - diagnostic probe
            print(f"     cause(scan={scan}) raised {type(exc).__name__}: {exc}", flush=True)
            continue
        if chain is None:
            print(f"     cause(scan={scan}) returned None", flush=True)
            continue
        _dump_chain(chain)
        _dump_roots_for_chain(ctx, work, chain, "     ")


def traced_mine(ctx: Any, work: Any, regressed_goal: tuple[str, Any]) -> list[tuple[str, Any]]:
    print("\n=== mine_regression_holds probe ===", flush=True)
    print(
        f"goal={regressed_goal!r} current={work.state.tags.get(regressed_goal[0])!r}",
        flush=True,
    )
    try:
        chain = work.cause(regressed_goal[0])
    except Exception as exc:  # noqa: BLE001 - diagnostic probe
        print(f"cause({regressed_goal[0]!r}) raised {type(exc).__name__}: {exc}", flush=True)
        return ORIGINAL_MINE(ctx, work, regressed_goal)
    if chain is None:
        print("cause returned None", flush=True)
        return ORIGINAL_MINE(ctx, work, regressed_goal)

    _dump_chain(chain)
    result = ORIGINAL_MINE(ctx, work, regressed_goal)
    print(f"  mined={result!r}", flush=True)
    _dump_roots_for_chain(ctx, work, chain, "  ")
    _dump_regression_scan_causes(ctx, work, regressed_goal)
    return result


def main() -> int:
    agenda.mine_regression_holds = traced_mine
    print(f"CLICK_PROJECT={CLICK_PROJECT}", flush=True)
    print("RUN=PLC(logic).how(y_BurnerLoop, walk_seconds=90, debug=True)", flush=True)
    t0 = time.monotonic()
    path = PLC(logic).how(y_BurnerLoop, walk_seconds=90, debug=True)
    elapsed = time.monotonic() - t0
    print(f"\nRESULT elapsed={elapsed:.3f}s reachable={path.reachable}", flush=True)
    print(f"RESULT reason={getattr(path, 'reason', None)!r}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
