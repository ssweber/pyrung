"""SPIKE #1 (read-only, LIVE burner): projected-oracle on the real program.

Loads the generated Click project, drives to Starting, and runs the spike's
projected-oracle writer selection against the REAL tags:

  * S_StateCompleteBool == 1   (one-hot Fix 1 boundary-gate tag)
  * Blower_CurStep     == 2    (the even-step affine wall, real blower.py)

Compares against today's `_rank_writers`.  No source changes.
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
sys.path.insert(0, str(Path(__file__).parent))

from main import logic  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pdg import build_program_graph, resolve_rung  # noqa: E402
from pyrung.core.analysis.pilot.compass import detect_opaque_loop  # noqa: E402
from pyrung.core.analysis.pilot.physical import install_harness  # noqa: E402
from pyrung.core.analysis.pilot.trace import (  # noqa: E402
    _rank_writers,
    compute_reference_constants,
    compute_steerable,
)
from pyrung.core.analysis.sp_values import _written_value_for_tag  # noqa: E402
from pyrung.core.crossing import Affine, Literal  # noqa: E402

from spike_projected_oracle import classify_writer, select_writer  # noqa: E402

PHYSICAL_PERMISSIVES = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_RotateSensor": False,
    "x_SailRelay": True,
}


def _known(plc: PLC, tag: str) -> bool:
    return tag in plc._known_tags_by_name or tag in plc.state.tags


def _pulse(plc: PLC, tag: str, settle: int = 8) -> None:
    plc.patch({tag: True})
    plc.step()
    for _ in range(settle):
        plc.step()


def _wv_label(pdg: Any, program: Any, ri: int, tag: str) -> str:
    ro = resolve_rung(program, pdg.rung_nodes[ri])
    wv = _written_value_for_tag(ro, tag) if ro is not None else None
    sub = pdg.rung_nodes[ri].subroutine or "Main"
    if isinstance(wv, Literal):
        k = f"literal({wv.value})"
    elif isinstance(wv, Affine):
        k = f"affine({wv.source}*{wv.scale}+{wv.offset})"
    else:
        k = "unknown"
    return f"{sub}:R{pdg.rung_nodes[ri].rung_index}[{k}]"


def _report(tag: str, value: Any, snap, pinned_overlay, pinned, pdg, program) -> None:
    print(f"\n--- target {tag} == {value!r}  (have={snap.get(tag)!r}) ---")
    writers = pdg.writers_of.get(tag, frozenset())
    print(f"  writers: {sorted(writers)}")

    ranked = _rank_writers(writers, pdg, program, tag, value, snap, frozenset(pinned))
    if ranked:
        print(f"  today  _rank_writers picks: {_wv_label(pdg, program, ranked[0], tag)}")
    else:
        print("  today  _rank_writers picks: <none>")

    print("  spike  per-writer classification:")
    for ri in sorted(writers):
        c = classify_writer(ri, tag, value, snap, pinned_overlay, pinned, pdg, program)
        if c is None:
            continue
        print(f"           {_wv_label(pdg, program, ri, tag)}: "
              f"counterfactual={c['counterfactual']} frontier={c['frontier']}")
    sel = select_writer(tag, value, snap, pinned_overlay, pinned, pdg, program)
    if sel is not None:
        print(f"  spike  projected-oracle picks: {_wv_label(pdg, program, sel['ri'], tag)}"
              f"  frontier={sel['frontier']}")
    else:
        print("  spike  projected-oracle picks: <none>")


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
    print(f"opaque_loop (one-hot state family) = {sorted(opaque_loop)}")

    # Proper mode-change handshake (request bit alongside the mode), then the
    # Clear/Reset/Start command pulses.
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step()
    plc.step()
    _pulse(plc, "C_Clear")
    _pulse(plc, "C_Reset")
    # Start, then step one at a time and CAPTURE the moment S_Starting goes
    # True -- the Starting->Execute boundary gate, before it coasts to Execute.
    plc.patch({"C_Start": True})
    snap = None
    for _ in range(200):
        plc.step()
        if plc.state.tags.get("S_Starting") is True:
            snap = dict(plc.state.tags)
            break
    if snap is None:
        snap = dict(plc.state.tags)
        print("WARNING: never observed S_Starting=True; using final snapshot")
    print(f"\nAt Starting: S_StateCurrent={snap.get('S_StateCurrent')} "
          f"S_Starting={snap.get('S_Starting')} Blower_CurStep={snap.get('Blower_CurStep')} "
          f"Blower__init={snap.get('Blower__init')} Blower_Trans={snap.get('Blower_Trans')}")

    pinned_overlay = {t: snap.get(t) for t in opaque_loop}
    pinned = set(opaque_loop)

    print("\n" + "=" * 72)
    print("ONE-HOT (Fix 1): S_StateCompleteBool")
    print("=" * 72)
    _report("S_StateCompleteBool", 1, snap, pinned_overlay, pinned, pdg, logic)

    print("\n" + "=" * 72)
    print("EVEN-STEP (affine wall): Blower_CurStep == 2  (at Starting snapshot)")
    print("=" * 72)
    _report("Blower_CurStep", 2, snap, pinned_overlay, pinned, pdg, logic)

    print("\n" + "=" * 72)
    print("EVEN-STEP adversarial: force Blower_CurStep=2 (even), retrace ==2")
    print("(this is where a current-snapshot check is fooled into picking R16)")
    print("=" * 72)
    adv = dict(snap)
    adv["Blower_CurStep"] = 2
    adv["Blower__valstepisodd"] = 0
    _report("Blower_CurStep", 2, adv, {t: adv.get(t) for t in opaque_loop}, pinned, pdg, logic)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
