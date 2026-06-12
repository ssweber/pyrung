"""Unit-probe the prereq extraction chain on the fill project.

Does _unsatisfied_conditions now emit the chased inequality prereqs
(sv_levelSetPoint via tare, systemLevel_opt2011 via the affine hop)?
"""

import sys
from dataclasses import replace as _replace

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (0175103C)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402
from tags import fill_stepNumber, t_fillDelay  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.circuitpy.codegen import compile_kernel  # noqa: E402
from pyrung.core.analysis.pdg import build_program_graph  # noqa: E402
from pyrung.core.analysis.prove import (  # noqa: E402
    _build_explore_context,
    _compile_property,
)
from pyrung.core.analysis.prove.passes import _OptConfig  # noqa: E402
from pyrung.core.analysis.walk.priors import (  # noqa: E402
    _functional_deps,
    _unsatisfied_condition_groups,
)

plc = PLC(logic)
plc.step()

_, auto_scope, expr = _compile_property(fill_stepNumber == 4)
compiled = compile_kernel(plc._program, blockless=True, proof_metadata=True)
ctx = _build_explore_context(
    plc._program,
    scope=auto_scope,
    extra_exprs=[expr] if expr is not None else [],
    _opt_config=_replace(_OptConfig(), walk_only=True),
    compiled=compiled,
    initial_state=dict(plc._state.tags),
    allow_partial=True,
)
print(f"ctx={type(ctx).__name__}")
func_deps = _functional_deps(ctx)
print(f"func_deps={func_deps}")
nd = dict(getattr(ctx, "nondeterministic_dims", {}) or {})

pdg = build_program_graph(plc._program)
snapshot = dict(plc._state.tags)
done_name = t_fillDelay.Done.name
print(f"done tag name: {done_name!r}")

for tag, value in (
    ("fill_subStatus", 1),
    (done_name, True),
    ("c_subStatusOneShot", True),
):
    res, groups = _unsatisfied_condition_groups(
        tag,
        value,
        snapshot,
        pdg,
        plc._program,
        nd_domains=nd,
        known=plc._known_tags_by_name,
        func_deps=func_deps,
    )
    print(f"\n({tag}, {value}):")
    print(f"  union: {res}")
    for g in groups:
        print(f"  group: {g}")
