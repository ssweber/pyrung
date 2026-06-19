"""Make-or-break probe for the constructive copy-source regression rung.

Context (handoff.md "Confirmed 2026-06-18"): the `(S_StateCurrent, 6)` waypoint
regresses because the rung-7 copy propagates a reverted `S_StateRequested=9`.
The proposed *rung (2)* repair raises the data-flow-half sub-goal
`(S_StateRequested, 6)` (the copy source at the committed value) and hands it to
the walker's normal **establish** resolver.

This whole ladder rests on ONE empirical claim, the make-or-break:

    `(S_StateRequested, 6)` is actually establishable via the external command
    chain — establish terminates at C_* commands (rung 2 is real), NOT at
    retentive/readonly init (in which case rung 4 *reject* is the honest
    terminal: the waypoint is genuinely unprotectable, and we improve only by
    diagnosing it and killing the 1934-fork thrash).

Experiments (cheap -> expensive):

  EXP-A  STRUCTURAL (instant, no walk): writers_of[S_StateRequested] and its
         PDG upstream cone.  Does the cone contain external command inputs
         (the lever) at all?  If not, rung (2)/(3) cannot work — go straight
         to rung (4).
  EXP-B  SANITY (quick walk): how(S_StateRequested == 9).  9 is the retentive
         resting value; this should be trivially reachable.  Confirms the
         walker is not simply broken on this tag before we read into EXP-C.
  EXP-C  THE MAKE-OR-BREAK (main walk): how(S_StateRequested == 6) from cold —
         the real establish machinery on the exact sub-goal rung (2) raises.
         reachable + the steers (are they external commands?).  Distinguishes
         honest-NotFound (-> rung 4) from budget-exhausted (inconclusive) from
         reachable-via-commands (-> rung 2 confirmed).

Note on faithfulness: EXP-C tests establishability in a *clean* cold context,
which is the faithful "can establish reach commands for this goal" question.
In-corridor protection (keep 6 across the copy until the next waypoint lands)
is owned by the agenda's must-stay scoping + the replay backstop, not something
a probe pre-confirms.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path as _Path
from typing import Any


CLICK_PROJECT = _Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\Sam\AppData\Local\Temp\CLICK (00010A66)\pyrung_project",
    )
)
sys.path.insert(0, str(CLICK_PROJECT))

from pyrung import PLC  # noqa: E402
from main import logic  # noqa: E402
from tags import S_StateRequested  # noqa: E402

from pyrung.core.analysis.pdg import build_program_graph, resolve_rung  # noqa: E402
from pyrung.core.analysis.walk.priors import (  # noqa: E402
    _external_bool_inputs,
    _edge_tags,
)

TARGET = "S_StateRequested"


def _ext_inputs() -> set[str]:
    pdg = build_program_graph(logic)
    plc = PLC(logic)
    known = plc._known_tags_by_name
    from pyrung.core.analysis.walk.passes import run_walk_passes

    advice, _journal = run_walk_passes(logic, pdg)
    ext = set(_external_bool_inputs(pdg, known, logic, advice=advice))
    ext |= _edge_tags(pdg, logic) & ext
    return ext


def exp_a() -> None:
    print("\n================ EXP-A: structural cone of S_StateRequested ================", flush=True)
    pdg = build_program_graph(logic)
    writers = sorted(pdg.writers_of.get(TARGET, ()))
    print(f"writers_of[{TARGET}] = {len(writers)} writer node(s):", flush=True)
    for node_idx in writers:
        node = pdg.rung_nodes[node_idx]
        rung = resolve_rung(logic, node)
        sp = None if rung is None else rung.sp_tree()
        gate = "<no sp / unconditional>" if sp is None else "<gated>"
        print(f"  node {node_idx}: {node.subroutine}[r{node.rung_index}] {gate}", flush=True)

    try:
        cone = pdg.upstream_slice(TARGET)
    except Exception as exc:  # noqa: BLE001 - diagnostic
        print(f"upstream_slice raised {type(exc).__name__}: {exc}", flush=True)
        cone = frozenset()
    ext = _ext_inputs()
    cone_cmds = sorted(n for n in cone if n.startswith("C_"))
    cone_ext = sorted(set(cone) & ext)
    print(f"\nupstream cone size = {len(cone)}", flush=True)
    print(f"cone & external inputs ({len(cone_ext)}): {cone_ext}", flush=True)
    print(f"cone C_*-prefixed ({len(cone_cmds)}): {cone_cmds}", flush=True)
    verdict = "HAS a command lever in the cone" if (cone_ext or cone_cmds) else "NO command lever in cone -> rung (4)"
    print(f"VERDICT-A: {verdict}", flush=True)


def _dump_path(label: str, path: Any) -> None:
    reachable = getattr(path, "reachable", None)
    reason = getattr(path, "reason", None)
    print(f"\n{label}: reachable={reachable!r}", flush=True)
    print(f"{label}: reason={reason!r}", flush=True)
    steps = getattr(path, "steps", None)
    if steps:
        print(f"{label}: {len(steps)} step(s):", flush=True)
        for i, step in enumerate(steps):
            print(f"  step {i}: {step!r}", flush=True)
    # Human-readable render (shows the input changes / plan).
    try:
        rendered = str(path)
        print(f"{label}: --- render ---\n{rendered}", flush=True)
    except Exception as exc:  # noqa: BLE001 - diagnostic
        print(f"{label}: str(path) raised {type(exc).__name__}: {exc}", flush=True)


def exp_b() -> None:
    print("\n================ EXP-B: sanity how(S_StateRequested == 9) ================", flush=True)
    t0 = time.monotonic()
    path = PLC(logic).how(S_StateRequested == 9, walk_seconds=20)
    print(f"elapsed={time.monotonic() - t0:.2f}s", flush=True)
    _dump_path("EXP-B", path)


def exp_c() -> None:
    print("\n================ EXP-C: MAKE-OR-BREAK how(S_StateRequested == 6) ================", flush=True)
    t0 = time.monotonic()
    path = PLC(logic).how(S_StateRequested == 6, walk_seconds=60, debug=True)
    print(f"elapsed={time.monotonic() - t0:.2f}s", flush=True)
    _dump_path("EXP-C", path)

    reachable = getattr(path, "reachable", None)
    reason = getattr(path, "reason", None)
    if reachable:
        print("\nVERDICT-C: REACHABLE -> rung (2) confirmed: establish can drive "
              "S_StateRequested to the committed value via the command chain. "
              "Check the steers above are C_* commands.", flush=True)
    elif reason and "budget" in str(reason).lower():
        print("\nVERDICT-C: INCONCLUSIVE (budget exhausted, not honest-NotFound). "
              "Establish did not finish; needs a larger cap or a shallower seam.", flush=True)
    else:
        print("\nVERDICT-C: HONEST NOT-FOUND -> rung (4) reject is the correct "
              "terminal: (S_StateRequested, 6) is not command-establishable, so "
              "the waypoint is genuinely unprotectable. Improve only via diagnosis "
              "+ anti-thrash, not a constructive hold.", flush=True)

    # Tail of the structured debug trace: goal lifecycle + oracle chains naming
    # the actual sub-goals establish raised for S_StateRequested.
    trace = getattr(path, "debug_trace", None)
    if trace is not None:
        text = str(trace)
        tail = "\n".join(text.splitlines()[-60:])
        print(f"\nEXP-C debug_trace (tail) ---\n{tail}", flush=True)


def main() -> int:
    print(f"CLICK_PROJECT={CLICK_PROJECT}", flush=True)
    exp_a()
    exp_b()
    exp_c()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
