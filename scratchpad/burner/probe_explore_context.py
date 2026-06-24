"""Inspect prover ExploreContext classifications for the burner program."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
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
from pyrung.circuitpy.codegen import compile_kernel  # noqa: E402
from pyrung.core.analysis.prove import _build_explore_context  # noqa: E402
from pyrung.core.analysis.prove.passes import _OptConfig  # noqa: E402
from pyrung.core.analysis.prove.results import Intractable  # noqa: E402

WATCH_PREFIXES = (
    "S_State",
    "S_",
    "C_Cmd",
    "C_CtrlCmd",
    "sm__jump",
    "sm__STATE",
    "isState",
    "isCmd",
)


def _interesting(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in WATCH_PREFIXES)


def _print_mapping(title: str, mapping: dict[str, Any], *, limit: int | None = None) -> None:
    items = [(k, v) for k, v in sorted(mapping.items()) if _interesting(str(k))]
    print(f"\n{title}: {len(items)} interesting / {len(mapping)} total")
    if limit is not None:
        items = items[:limit]
    for key, value in items:
        print(f"  {key}: {value!r}")


def _print_names(title: str, names: Any, *, limit: int | None = None) -> None:
    values = sorted(str(name) for name in names if _interesting(str(name)))
    print(f"\n{title}: {len(values)} interesting / {len(names)} total")
    if limit is not None:
        values = values[:limit]
    for name in values:
        print(f"  {name}")


def main() -> int:
    plc = PLC(logic)
    compiled = compile_kernel(logic, blockless=True, proof_metadata=True)
    opt = replace(_OptConfig(), domains_only=True)
    ctx = _build_explore_context(
        logic,
        _opt_config=opt,
        compiled=compiled,
        initial_state=dict(plc.state.tags),
        allow_partial=True,
    )
    if isinstance(ctx, Intractable):
        print("ExploreContext was intractable")
        for hint in ctx.hints:
            print(f"  {hint}")
        return 1

    print(f"CLICK_PROJECT={CLICK_PROJECT}")
    print("\nCounts:")
    for name in (
        "stateful_dims",
        "nondeterministic_dims",
        "combinational_tags",
        "elided_tags",
        "functional_dep_projections",
        "init_constant_projections",
        "stepping_tags",
        "free_input_names",
        "edge_tag_names",
        "memory_key_names",
        "stateful_names",
    ):
        value = getattr(ctx, name)
        print(f"  {name}: {len(value)}")

    _print_mapping("stateful_dims", ctx.stateful_dims)
    _print_mapping("nondeterministic_dims", ctx.nondeterministic_dims)
    _print_names("combinational_tags", ctx.combinational_tags)
    _print_mapping("elided_tags", ctx.elided_tags)
    _print_mapping("functional_dep_projections", ctx.functional_dep_projections)
    _print_mapping("init_constant_projections", ctx.init_constant_projections)
    _print_names("stepping_tags", ctx.stepping_tags)
    _print_names("free_input_names", ctx.free_input_names)
    _print_names("edge_tag_names", ctx.edge_tag_names)
    _print_names("memory_key_names", ctx.memory_key_names)
    _print_names("stateful_names", ctx.stateful_names)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
