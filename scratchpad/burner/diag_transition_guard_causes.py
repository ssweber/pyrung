"""Diagnose burner state-transition guards and causal evidence.

This is a scratchpad probe for the PILOT compass work.  It drives the CLICK
burner through the known production startup path, then asks the existing causal
machinery what triggered and enabled the important transitions.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable
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
from pyrung.core.analysis.causal.models import CausalChain  # noqa: E402
from pyrung.core.analysis.pdg import build_program_graph, resolve_rung  # noqa: E402

WATCH_TAGS = (
    "S_UnitModeCurrent",
    "S_StateCurrent",
    "S_StateRequested",
    "S_StateComplete",
    "S_StateCompleteBool",
    "isStateEnbl_Yes",
    "S_Clearing",
    "S_Stopped",
    "S_Resetting",
    "S_Idle",
    "S_Starting",
    "S_Execute",
    "C_CtrlCmd",
    "C_UnitModeChgRequest",
    "C_ProductionMode",
    "C_Clear",
    "C_Reset",
    "C_Start",
    "C_Stop",
    "C_Abort",
    "C_StateComplete",
    "StateComplete",
    "Rotate_xCall",
    "Rotate__x",
    "Rotate_CurStep",
    "Rotate__init",
    "Rotate_Error",
    "Blower_xCall",
    "Blower__x",
    "Blower_CurStep",
    "Blower__init",
    "Blower_Error",
    "HeatDelay_Tmr_Acc",
    "HeatDelay_Tmr_Done",
    "Heat_xCall",
    "Heat__x",
    "Heat_CurStep",
    "Heat__init",
    "Heat_Error",
    "y_BurnerLoop",
)

STRUCTURE_TAGS = (
    "S_StateCurrent",
    "S_StateRequested",
    "S_StateComplete",
    "S_StateCompleteBool",
    "isStateEnbl_Yes",
)

EXPLAIN_ON_CHANGE = {
    "S_StateCurrent",
    "S_StateRequested",
    "S_StateComplete",
    "S_StateCompleteBool",
    "isStateEnbl_Yes",
    "Rotate_CurStep",
    "Blower_CurStep",
    "HeatDelay_Tmr_Done",
    "Heat_xCall",
    "Heat_CurStep",
    "y_BurnerLoop",
}

PHYSICAL_PERMISSIVES = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_RotateSensor": False,
    "x_SailRelay": True,
}


def _known(plc: PLC, tags: Iterable[str]) -> tuple[str, ...]:
    known = getattr(plc, "_known_tags_by_name", {})
    return tuple(tag for tag in tags if tag in known or tag in plc.state.tags)


def _value(plc: PLC, tag: str) -> Any:
    return plc.state.tags.get(tag, "<missing>")


def _snapshot(plc: PLC, watch: Iterable[str]) -> dict[str, Any]:
    return {tag: _value(plc, tag) for tag in watch}


def _transition_text(transition: Any) -> str:
    return (
        f"{transition.tag_name}@{transition.scan_id}: "
        f"{transition.from_value!r}->{transition.to_value!r}"
    )


def _short_values(items: Iterable[Any], *, limit: int = 12) -> str:
    values = list(items)
    if not values:
        return "-"
    rendered = ", ".join(str(value) for value in values[:limit])
    if len(values) > limit:
        rendered += f", ... {len(values) - limit} more"
    return rendered


def _print_chain(label: str, chain: CausalChain | None, *, max_steps: int = 12) -> None:
    print(f"\n--- {label} ---")
    if chain is None:
        print("  (no chain)")
        return

    print(
        f"  mode={chain.mode} confidence={chain.confidence:.3f} "
        f"effect={_transition_text(chain.effect)}"
    )
    if chain.effects:
        print("  effects:")
        for effect in chain.effects[:max_steps]:
            print(f"    - {_transition_text(effect)}")
    if chain.conjunctive_roots:
        print("  conjunctive_roots:")
        for root in chain.conjunctive_roots[:max_steps]:
            print(f"    - {_transition_text(root)}")
    if chain.ambiguous_roots:
        print("  ambiguous_roots:")
        for root in chain.ambiguous_roots[:max_steps]:
            print(f"    - {_transition_text(root)}")
    if chain.blockers:
        print("  blockers:")
        for blocker in chain.blockers[:max_steps]:
            print(f"    - {blocker.to_dict()}")

    print("  steps:")
    for index, step in enumerate(chain.steps[:max_steps]):
        triggers = _short_values(_transition_text(t) for t in step.triggers)
        enablers = _short_values(
            f"{e.tag_name}={e.value!r}@{e.held_since_scan}" for e in step.enablers
        )
        location = f"sub={step.subroutine!r}" if step.subroutine else "main"
        caller = (
            f" caller_rung={step.caller_rung_index + 1}"
            if step.caller_rung_index is not None
            else ""
        )
        instruction = step.instruction or "write"
        kind = f" kind={step.kind}" if step.kind else ""
        print(
            f"    [{index}] rung={step.rung_index + 1} {location}{caller} "
            f"instr={instruction} fidelity={step.fidelity}{kind}"
        )
        print(f"        transition={_transition_text(step.transition)}")
        print(f"        triggers={triggers}")
        print(f"        enablers={enablers}")
    if len(chain.steps) > max_steps:
        print(f"    ... {len(chain.steps) - max_steps} more steps")


def _print_projected_probe(plc: PLC, label: str) -> None:
    if "S_StateCurrent" in plc.state.tags:
        _print_chain(
            f"{label}: projected S_StateCurrent -> 6",
            plc.cause("S_StateCurrent", to=6),
            max_steps=10,
        )
    for tag in ("S_StateComplete", "S_StateCompleteBool"):
        if tag in plc.state.tags:
            _print_chain(
                f"{label}: projected {tag} -> True",
                plc.cause(tag, to=True),
                max_steps=10,
            )
            break
    why_tags = [tag for tag in STRUCTURE_TAGS if tag in plc.state.tags]
    if why_tags:
        _print_chain(
            f"{label}: why({', '.join(why_tags)})",
            plc.why(*why_tags),
            max_steps=14,
        )


def _print_writers(plc: PLC) -> set[int]:
    pdg = build_program_graph(logic)
    interesting_timeline_rungs: set[int] = set()
    print("\n=== Static writers for transition structure ===")
    for tag in STRUCTURE_TAGS:
        if tag not in pdg.tags and tag not in pdg.writers_of:
            continue
        writer_indices = sorted(pdg.writers_of.get(tag, frozenset()))
        timeline_indices = sorted(pdg.timeline_writers_of(tag))
        interesting_timeline_rungs.update(timeline_indices)
        print(
            f"\n{tag}: pdg_writers={writer_indices} "
            f"timeline_capture_rungs={[i + 1 for i in timeline_indices]}"
        )
        for node_index in writer_indices:
            node = pdg.rung_nodes[node_index]
            rung = resolve_rung(logic, node)
            branch = f" branch={node.branch_path}" if node.branch_path else ""
            source = (
                f" source={node.source_file}:{node.source_line}"
                if node.source_file or node.source_line
                else ""
            )
            print(
                f"  node={node_index} rung={node.rung_index + 1} "
                f"scope={node.scope} sub={node.subroutine!r}{branch}{source}"
            )
            print(f"    condition_reads={sorted(node.condition_reads)}")
            print(f"    data_reads={sorted(node.data_reads)}")
            print(f"    writes={sorted(node.writes)} implicit={sorted(node.implicit_writes)}")
            if rung is not None:
                rung_text = " ".join(str(rung).split())
                print(f"    rung={rung_text[:240]}")
    return interesting_timeline_rungs


def _print_selected_firings(
    plc: PLC,
    timeline_rungs: set[int],
    *,
    focus_tags: Iterable[str] = (),
) -> None:
    if not timeline_rungs:
        return
    firings = dict(plc.debug.rung_firings(plc.state.scan_id))
    display_tags = set(WATCH_TAGS) | set(STRUCTURE_TAGS) | set(focus_tags)
    selected: list[tuple[int, dict[str, Any]]] = []
    for idx in sorted(timeline_rungs):
        if idx not in firings:
            continue
        writes = dict(firings[idx])
        focused = {tag: value for tag, value in writes.items() if tag in display_tags}
        if focused:
            selected.append((idx, focused))
    if not selected:
        return
    print("  selected rung firings:")
    for rung_index, writes in selected:
        print(f"    r{rung_index + 1}: {writes}")


def _print_snapshot(plc: PLC, label: str, watch: tuple[str, ...]) -> None:
    fields = ", ".join(f"{tag}={_value(plc, tag)!r}" for tag in watch)
    print(f"\n[{plc.state.scan_id:05d}] {label}")
    print(f"  {fields}")


def _explain_changes(
    plc: PLC,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    timeline_rungs: set[int],
) -> None:
    changes = [(tag, before.get(tag), after.get(tag)) for tag in after if before.get(tag) != after[tag]]
    if not changes:
        return
    print(f"\n[{plc.state.scan_id:05d}] watched changes")
    for tag, old, new in changes:
        print(f"  {tag}: {old!r} -> {new!r}")
    _print_selected_firings(plc, timeline_rungs, focus_tags=(tag for tag, _old, _new in changes))
    for tag, _old, _new in changes:
        if tag in EXPLAIN_ON_CHANGE:
            _print_chain(
                f"recorded cause for {tag} at scan {plc.state.scan_id}",
                plc.cause(tag, scan=plc.state.scan_id),
                max_steps=8,
            )


def _step_observe(
    plc: PLC,
    watch: tuple[str, ...],
    *,
    timeline_rungs: set[int],
    animate_rotate_sensor: bool = False,
    label: str | None = None,
) -> None:
    before = _snapshot(plc, watch)
    if animate_rotate_sensor and "x_RotateSensor" in plc.state.tags:
        plc.force("x_RotateSensor", (plc.state.scan_id // 50) % 2 == 0)
    plc.step()
    after = _snapshot(plc, watch)
    if label is not None:
        _print_snapshot(plc, label, watch)
    _explain_changes(plc, before, after, timeline_rungs=timeline_rungs)


def _run_steps(
    plc: PLC,
    count: int,
    watch: tuple[str, ...],
    *,
    timeline_rungs: set[int],
    animate_rotate_sensor: bool = False,
    stop_when_burner_loop: bool = False,
    periodic: int = 500,
) -> None:
    for local_index in range(1, count + 1):
        _step_observe(
            plc,
            watch,
            timeline_rungs=timeline_rungs,
            animate_rotate_sensor=animate_rotate_sensor,
        )
        if periodic and local_index % periodic == 0:
            _print_snapshot(plc, f"periodic wait +{local_index}", watch)
            _print_projected_probe(plc, f"periodic wait +{local_index}")
        if stop_when_burner_loop and _value(plc, "y_BurnerLoop") is True:
            _print_snapshot(plc, "hit y_BurnerLoop", watch)
            _print_projected_probe(plc, "hit y_BurnerLoop")
            return


def _pulse(
    plc: PLC,
    tag: str,
    watch: tuple[str, ...],
    *,
    timeline_rungs: set[int],
    settle_scans: int = 4,
) -> None:
    print(f"\n=== Pulse {tag}=True ===")
    plc.patch({tag: True})
    _step_observe(plc, watch, timeline_rungs=timeline_rungs, label=f"after {tag} action scan")
    _print_projected_probe(plc, f"after {tag} action scan")
    _run_steps(plc, settle_scans, watch, timeline_rungs=timeline_rungs)
    _print_snapshot(plc, f"after {tag} settle", watch)
    _print_projected_probe(plc, f"after {tag} settle")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--dt", type=float, default=0.010)
    parser.add_argument("--periodic", type=int, default=500)
    args = parser.parse_args()

    print(f"CLICK_PROJECT={CLICK_PROJECT}")
    print(f"seconds={args.seconds} dt={args.dt}")

    plc = PLC(logic, dt=args.dt, record_all_tags=True)
    watch = _known(plc, WATCH_TAGS)
    timeline_rungs = _print_writers(plc)

    print("\n=== Initial and mode setup ===")
    for name, value in PHYSICAL_PERMISSIVES.items():
        if name in plc.state.tags or name in getattr(plc, "_known_tags_by_name", {}):
            plc.force(name, value)
    _step_observe(plc, watch, timeline_rungs=timeline_rungs, label="after first scan + permissives")
    _print_projected_probe(plc, "after first scan + permissives")

    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    _step_observe(plc, watch, timeline_rungs=timeline_rungs, label="after production-mode action scan")
    _run_steps(plc, 3, watch, timeline_rungs=timeline_rungs)
    _print_snapshot(plc, "after production-mode settle", watch)
    _print_projected_probe(plc, "after production-mode settle")

    _pulse(plc, "C_Clear", watch, timeline_rungs=timeline_rungs)
    _pulse(plc, "C_Reset", watch, timeline_rungs=timeline_rungs)
    _pulse(plc, "C_Start", watch, timeline_rungs=timeline_rungs)

    wait_scans = max(0, int(args.seconds / args.dt))
    print(f"\n=== Run for {wait_scans} scans after Start ===")
    _run_steps(
        plc,
        wait_scans,
        watch,
        timeline_rungs=timeline_rungs,
        animate_rotate_sensor=True,
        stop_when_burner_loop=True,
        periodic=args.periodic,
    )
    _print_snapshot(plc, "final", watch)
    _print_projected_probe(plc, "final")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
