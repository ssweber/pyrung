"""At Starting (no permissives), what candidates does the pilot actually build?

Resolves the crux question: can the pilot derive that it must HOLD
x_BlowerFB=True / x_RotateFB=True to advance the stalled Blower/Rotate SFCs,
or does it only surface the wrong toggle (=False)?
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

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
from pyrung.core.analysis.pilot.candidates import _build_candidates  # noqa: E402
from pyrung.core.analysis.pilot.compass import (  # noqa: E402
    Compass,
    detect_opaque_loop,
    detect_opaque_pipelines,
)
from pyrung.core.analysis.pilot.physical import install_harness  # noqa: E402
from pyrung.core.analysis.pilot.pilot import (  # noqa: E402
    _build_pilot_context,
    _make_pilot_context,
    _PilotState,
    _prepare_iteration,
)
from pyrung.core.analysis.pilot.steers import upstream_candidates  # noqa: E402
from pyrung.core.analysis.pilot.trace import (  # noqa: E402
    compute_edge_tags,
    compute_reference_constants,
    compute_resting_values,
    compute_steerable,
)

FEEDBACKS = ["x_BlowerFB", "x_RotateFB", "x_DoorClosed", "x_LintDoorClosed",
             "x_SailRelay", "x_RotateSensor", "i_BlowerFB", "i_RotateFB"]


def pulse(plc: PLC, name: str, settle: int = 4) -> None:
    plc.patch({name: True})
    plc.step()
    for _ in range(settle):
        plc.step()


def main() -> int:
    plc = PLC(logic)
    plc.step()
    # Drive to Starting WITHOUT permissives.
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step(); plc.step()
    pulse(plc, "C_Clear")
    pulse(plc, "C_Reset")
    pulse(plc, "C_Start")
    snap0 = plc.state.tags
    print(f"S_StateCurrent={snap0.get('S_StateCurrent')}  (3=Starting)")
    print("feedback values now:")
    for t in FEEDBACKS:
        print(f"  {t}={snap0.get(t)!r}")

    # Build the real pilot context.
    program = logic
    fork = plc.fork()
    pdg = build_program_graph(program)
    harness_fb = install_harness(fork)
    ref_consts = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, fork._known_tags_by_name, pdg, program)
    nd_domains, key_config, evidence = _build_pilot_context(program, dict(fork.state.tags))
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    inf = Compass(opaque_slices)
    opaque_loop = detect_opaque_loop(pdg, program)

    print(f"\nx_BlowerFB steerable? {'x_BlowerFB' in steerable}")
    print(f"x_RotateFB steerable? {'x_RotateFB' in steerable}")
    print(f"i_BlowerFB steerable? {'i_BlowerFB' in steerable}")

    ctx = _make_pilot_context(
        fork, "y_BurnerLoop", True, pdg, program, steerable, edge_tags, resting,
        nd_domains=nd_domains, evidence=evidence, influence=inf,
        opaque_loop=opaque_loop, choice=None, blocked_choice_actions=frozenset(),
        max_scans=3000, live=False, debug=False,
    )
    state = _PilotState(
        work=fork, key_config=key_config, seen_keys=set(), nogoods={},
        checkpoints=[], forced_holds={}, steps=[], watch_tags=[],
    )

    def _dbg(_m: str) -> None:
        return None

    frame = _prepare_iteration(state, ctx, _dbg)
    print(f"\ndistance={frame.distance_before}")

    # Stuck tags (dead-end leaves) the candidate builder probes.
    stuck = sorted(
        n.tag for n in frame.tree.leaves()
        if not n.satisfied and not n.is_steerable
        and not getattr(n, "pipeline_internal", False)
    )
    print(f"\nstuck dead-end leaves ({len(stuck)}):")
    for t in stuck:
        print(f"  {t}  (have {frame.snap.get(t)!r})")

    # Is x_BlowerFB in the PDG upstream slice of Blower__init?
    for target in ("Blower__init", "Rotate__init"):
        up = pdg.upstream_slice(target)
        feeders = sorted(f for f in FEEDBACKS if f in up)
        print(f"\nupstream_slice({target}) feedbacks: {feeders}")

    # What does upstream_candidates produce for the SFC tags?
    sfc_stuck = {t for t in stuck if t.startswith(("Blower", "Rotate", "Heat"))}
    up_c = upstream_candidates(
        sfc_stuck or set(stuck), steerable, set(), frame.snap, pdg,
        nd_domains=nd_domains, needed_values={},
    )
    fb_c = [c for c in up_c if c[0] in FEEDBACKS]
    print(f"\nupstream_candidates feedback proposals (value matters!): {fb_c}")

    cl = _build_candidates(frame, state, ctx, _dbg)
    print(f"\nwait_prescribed={cl.wait_prescribed}  reason={cl.wait_reason}")
    fb_cands = [(c.tag, c.value) for c in cl.candidates if c.tag in FEEDBACKS]
    print(f"feedback candidates in final list: {fb_cands}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
