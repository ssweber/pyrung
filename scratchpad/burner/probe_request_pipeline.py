"""Probe the shared request pipeline behind an observed opaque transition.

This scratchpad script intentionally observes one representative transition,
then switches to static expansion:

1. Observe ``target`` changing once.
2. Infer the request/source tag consumed by the target writer.
3. Enumerate sibling writers of that request tag.

For the burner, this shows that ``S_StateComplete`` and command-change routes
are peer ingress paths into the same ``S_StateRequested -> S_StateCurrent``
pipeline.  The algorithm here is generic; the burner-specific part is only the
setup sequence that creates a useful representative observation.
"""

from __future__ import annotations

import argparse
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
from pyrung.core.analysis.causal.models import CausalChain, ChainStep  # noqa: E402
from pyrung.core.analysis.pdg import ProgramGraph, build_program_graph, resolve_rung  # noqa: E402
from pyrung.core.analysis.simplified import _sp_to_expr  # noqa: E402
from pyrung.core.analysis.sp_values import (  # noqa: E402
    _extract_condition_values,
    _written_value_for_tag,
)
from pyrung.core.crossing import UNKNOWN, Affine, Literal  # noqa: E402

PHYSICAL_PERMISSIVES = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_RotateSensor": False,
    "x_SailRelay": True,
}


def _known(plc: PLC, tag: str) -> bool:
    known = getattr(plc, "_known_tags_by_name", {})
    return tag in known or tag in plc.state.tags


def _force_permissives(plc: PLC) -> None:
    for name, value in PHYSICAL_PERMISSIVES.items():
        if _known(plc, name):
            plc.force(name, value)


def _step(plc: PLC, *, animate_rotate_sensor: bool = False) -> None:
    if animate_rotate_sensor and _known(plc, "x_RotateSensor"):
        plc.force("x_RotateSensor", (plc.state.scan_id // 50) % 2 == 0)
    plc.step()


def _pulse(plc: PLC, tag: str, settle_scans: int = 4) -> None:
    plc.patch({tag: True})
    _step(plc)
    for _ in range(settle_scans):
        _step(plc)


def _setup_production(plc: PLC) -> None:
    _force_permissives(plc)
    _step(plc)
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    _step(plc)
    for _ in range(3):
        _step(plc)


def _drive_to_starting(plc: PLC) -> None:
    _setup_production(plc)
    _pulse(plc, "C_Clear")
    _pulse(plc, "C_Reset")
    plc.patch({"C_Start": True})
    _step(plc)
    if plc.state.tags.get("S_StateCurrent") != 3:
        raise RuntimeError(f"expected S_StateCurrent=3, got {plc.state.tags.get('S_StateCurrent')!r}")


def _observe_next_change(
    plc: PLC,
    target: str,
    *,
    max_scans: int,
    animate_rotate_sensor: bool,
) -> CausalChain:
    before = plc.state.tags.get(target)
    for _ in range(max_scans):
        _step(plc, animate_rotate_sensor=animate_rotate_sensor)
        after = plc.state.tags.get(target)
        if after != before:
            chain = plc.cause(target, scan=plc.state.scan_id)
            if chain is None:
                raise RuntimeError(f"{target} changed at scan {plc.state.scan_id}, but cause() returned None")
            return chain
    raise RuntimeError(f"{target} did not change within {max_scans} scans")


def _transition_text(step: ChainStep) -> str:
    t = step.transition
    return f"{t.tag_name}: {t.from_value!r}->{t.to_value!r} @scan {t.scan_id}"


def _infer_request_tag(chain: CausalChain, target: str) -> tuple[str, Any] | None:
    """Infer the tag/value feeding the target writer from the recorded chain."""
    target_steps = [step for step in chain.steps if step.transition.tag_name == target]
    if not target_steps:
        return None
    target_step = target_steps[0]
    target_to = target_step.transition.to_value
    for trigger in target_step.triggers:
        if trigger.to_value == target_to:
            return (trigger.tag_name, trigger.to_value)
    for enabler in target_step.enablers:
        if enabler.value == target_to:
            return (enabler.tag_name, enabler.value)
    if target_step.triggers:
        trigger = target_step.triggers[0]
        return (trigger.tag_name, trigger.to_value)
    return None


def _format_written_value(value: Any) -> str:
    if isinstance(value, Literal):
        return repr(value.value)
    if isinstance(value, Affine):
        return f"{value.source}*{value.scale!r}+{value.offset!r}"
    if value is UNKNOWN:
        return "UNKNOWN"
    return repr(value)


def _condition_values_for_rung(rung_obj: Any) -> dict[str, frozenset[Any]]:
    if rung_obj is None:
        return {}
    sp = rung_obj.sp_tree()
    if sp is None:
        return {}
    return _extract_condition_values(_sp_to_expr(sp))


def _print_observed_chain(chain: CausalChain, target: str) -> None:
    print("\n=== Observed Representative Edge ===")
    print(
        f"{chain.effect.tag_name}: {chain.effect.from_value!r}->{chain.effect.to_value!r} "
        f"at scan {chain.effect.scan_id}"
    )
    for index, step in enumerate(chain.steps[:12]):
        triggers = ", ".join(f"{t.tag_name}:{t.from_value!r}->{t.to_value!r}" for t in step.triggers)
        enablers = ", ".join(f"{e.tag_name}={e.value!r}" for e in step.enablers)
        caller = f" caller=r{step.caller_rung_index + 1}" if step.caller_rung_index is not None else ""
        marker = " <= target writer" if step.transition.tag_name == target else ""
        print(
            f"[{index}] {step.subroutine or 'main'} r{step.rung_index + 1}{caller}: "
            f"{_transition_text(step)}{marker}"
        )
        print(f"    triggers={triggers or '-'}")
        print(f"    enablers={enablers or '-'}")


def _tag_value(plc: PLC, tag: str) -> Any:
    if tag in plc.state.tags:
        return plc.state.tags[tag]
    known = getattr(plc, "_known_tags_by_name", {})
    tag_ref = known.get(tag)
    if tag_ref is not None:
        return tag_ref.default
    return "<unknown>"


def _print_call_site(pdg: ProgramGraph, plc: PLC, call_rung_index: int) -> None:
    node_index = pdg.main_node_by_rung()[call_rung_index]
    node = pdg.rung_nodes[node_index]
    rung_obj = resolve_rung(logic, node)
    cond_values = _condition_values_for_rung(rung_obj)
    values = {tag: _tag_value(plc, tag) for tag in sorted(node.condition_reads | node.data_reads)}
    print(
        f"  call_site node={node_index} rung={node.rung_index + 1} "
        f"conds={sorted(node.condition_reads)} condition_values={cond_values} values={values}"
    )


def _print_request_writer_table(pdg: ProgramGraph, plc: PLC, request_tag: str) -> None:
    print(f"\n=== Static Request Writers: {request_tag} ===")
    groups: dict[str, list[tuple[int, Any]]] = {}
    for node_index in sorted(pdg.writers_of.get(request_tag, frozenset())):
        node = pdg.rung_nodes[node_index]
        group = node.subroutine or "main"
        groups.setdefault(group, []).append((node_index, node))

    call_sites = pdg.call_site_rung_indices()
    for group, entries in groups.items():
        print(f"\n-- {group} ({len(entries)} writers) --")
        if group in call_sites:
            print(f"call_sites={[idx + 1 for idx in sorted(call_sites[group])]}")
            for call_rung_index in sorted(call_sites[group]):
                _print_call_site(pdg, plc, call_rung_index)
        for node_index, node in entries:
            rung_obj = resolve_rung(logic, node)
            cond_values = _condition_values_for_rung(rung_obj)
            written = _written_value_for_tag(rung_obj, request_tag)
            data_values = {tag: _tag_value(plc, tag) for tag in sorted(node.data_reads)}
            print(
                f"node={node_index} rung={node.rung_index + 1} "
                f"scope={node.scope} branch={node.branch_path or '-'}"
            )
            print(f"  writes {request_tag} := {_format_written_value(written)}")
            print(f"  condition_reads={sorted(node.condition_reads)}")
            print(f"  condition_values={{{', '.join(f'{k}: {sorted(v)!r}' for k, v in sorted(cond_values.items()))}}}")
            print(f"  data_reads={data_values}")


def _print_target_writer_table(pdg: ProgramGraph, target: str) -> None:
    print(f"\n=== Target Writers: {target} ===")
    for node_index in sorted(pdg.writers_of.get(target, frozenset())):
        node = pdg.rung_nodes[node_index]
        rung_obj = resolve_rung(logic, node)
        cond_values = _condition_values_for_rung(rung_obj)
        written = _written_value_for_tag(rung_obj, target)
        print(
            f"node={node_index} rung={node.rung_index + 1} "
            f"sub={node.subroutine or 'main'} conds={sorted(node.condition_reads)} "
            f"data={sorted(node.data_reads)} write={_format_written_value(written)} "
            f"condition_values={cond_values}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="S_StateCurrent")
    parser.add_argument(
        "--edge",
        choices=("starting-execute", "first-command"),
        default="starting-execute",
    )
    parser.add_argument("--max-scans", type=int, default=900)
    args = parser.parse_args()

    print(f"CLICK_PROJECT={CLICK_PROJECT}")
    print(f"target={args.target} edge={args.edge}")

    pdg = build_program_graph(logic)
    plc = PLC(logic, record_all_tags=True)

    if args.edge == "starting-execute":
        _drive_to_starting(plc)
        print(f"base scan={plc.state.scan_id} {args.target}={plc.state.tags.get(args.target)!r}")
        chain = _observe_next_change(
            plc,
            args.target,
            max_scans=args.max_scans,
            animate_rotate_sensor=True,
        )
    else:
        _setup_production(plc)
        print(f"base scan={plc.state.scan_id} {args.target}={plc.state.tags.get(args.target)!r}")
        plc.patch({"C_Clear": True})
        _step(plc)
        chain = plc.cause(args.target, scan=plc.state.scan_id)
        if chain is None:
            raise RuntimeError("first-command edge did not produce a cause chain")

    _print_observed_chain(chain, args.target)
    request = _infer_request_tag(chain, args.target)
    if request is None:
        print("\nNo request tag inferred from target writer.")
        return 1

    request_tag, request_value = request
    print(f"\nInferred shared request tag: {request_tag}={request_value!r}")
    _print_target_writer_table(pdg, args.target)
    _print_request_writer_table(pdg, plc, request_tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
