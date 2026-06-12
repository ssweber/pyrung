"""Why does _governing pick HMI_resetError for (fill_stepNumber, 5)?"""
import sys
from dataclasses import replace as _replace

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (0175103C)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402
from tags import fill_stepNumber  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.circuitpy.codegen import compile_kernel  # noqa: E402
from pyrung.core.analysis.pdg import build_program_graph  # noqa: E402
from pyrung.core.analysis.prove import _build_explore_context, _compile_property  # noqa: E402
from pyrung.core.analysis.prove.passes import _OptConfig  # noqa: E402
from pyrung.core.analysis.walk.priors import _governing, _probe_steps, _value_richness  # noqa: E402

plc = PLC(logic)
plc.step()

_, auto_scope, expr = _compile_property(fill_stepNumber == 5)
compiled = compile_kernel(plc._program, blockless=True, proof_metadata=True)
ctx = _build_explore_context(
    plc._program, scope=auto_scope, extra_exprs=[expr] if expr is not None else [],
    _opt_config=_replace(_OptConfig(), walk_only=True), compiled=compiled,
    initial_state=dict(plc._state.tags), allow_partial=True,
)
pdg = build_program_graph(plc._program)

stepping = getattr(ctx, "stepping_tags", None)
print(f"stepping_tags: {sorted(stepping) if stepping else stepping}")
print(f"fill_stepNumber in stepping: {stepping is not None and 'fill_stepNumber' in stepping}")
print(f"_value_richness(fill_stepNumber) = {_value_richness('fill_stepNumber', pdg, plc._program)}")
print(f"tag_roles[HMI_resetError] = {pdg.tag_roles.get('HMI_resetError')}")
print(f"tag_roles[fill_stepNumber] = {pdg.tag_roles.get('fill_stepNumber')}")
probed = _probe_steps(plc, "fill_stepNumber", pdg, plc._known_tags_by_name, plc._program)
print(f"_probe_steps(fill_stepNumber) = {probed}")
gov = _governing("fill_stepNumber", 5, pdg, plc._program, explore_context=ctx, plc=plc)
print(f"_governing -> {gov}")
