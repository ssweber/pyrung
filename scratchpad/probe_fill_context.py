"""Inspect pipeline context for the fill project: domains + func-deps.

What would an inequality func-dep chase have to work with?
"""

import sys
from dataclasses import replace as _replace

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (0175103C)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402
from tags import _fill  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.circuitpy.codegen import compile_kernel  # noqa: E402
from pyrung.core.analysis.prove import (  # noqa: E402
    _build_explore_context,
    _compile_property,
)
from pyrung.core.analysis.prove.passes import _OptConfig  # noqa: E402

plc = PLC(logic)
plc.step()

_, auto_scope, expr = _compile_property(_fill)
compiled = compile_kernel(plc._program, blockless=True, proof_metadata=True)
ctx = _build_explore_context(
    plc._program,
    scope=auto_scope,
    extra_exprs=[expr],
    _opt_config=_replace(_OptConfig(), walk_only=True),
    compiled=compiled,
    initial_state=dict(plc._state.tags),
    allow_partial=True,
)
print(type(ctx).__name__)

nd = getattr(ctx, "nondeterministic_dims", {}) or {}
for name in (
    "systemLevel_opt2011",
    "sv_levelBand",
    "sv_levelIncrement",
    "sv_levelHtMax",
    "sv_levelHtMin",
    "pv_LevelHt",
    "sv_levelSetPoint",
    "tsv_fillDelay_ss",
):
    print(f"nd[{name}] = {nd.get(name)}")

fd = getattr(ctx, "functional_dep_projections", None)
print(f"\nfunctional_dep_projections ({len(fd) if fd else 0}):")
if fd:
    for k, v in fd.items():
        print(f"  {k}: {v}")

print("\n--- classification ---")
for name in (
    "pv_LevelHt",
    "calc_levelSvLowerWBand",
    "calc_levelSvUpperWBand",
    "sv_levelSetPoint",
    "systemLevel_opt2011",
):
    in_stateful = name in (getattr(ctx, "stateful_dims", {}) or {})
    in_nd = name in (getattr(ctx, "nondeterministic_dims", {}) or {})
    comb = getattr(ctx, "combinational_tags", None) or ()
    elided = getattr(ctx, "elided_tags", None) or {}
    print(
        f"{name}: stateful={in_stateful} nd={in_nd} comb={name in comb}"
        f" elided={elided.get(name)}"
    )
