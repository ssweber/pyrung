"""SPIKE (read-only): projected-oracle attribution kernel.

Validates the one abstraction the trace_back refactor turns on, BEFORE
deleting `_rank_writers` or touching the live trace.

Context
-------
There are already three backward walks in-tree, all built on the SAME
substrate -- `attribute()` / `evaluate_sp()` (sp_tree.py), pure structural
walks parameterized by an injected condition-oracle `Callable[[Condition], bool]`:

  * why_cause        -- snapshot-explanatory (explains the present value)
  * projected_cause  -- `cause(to=)`: projected planner toward a to_value,
                        with `assume` overlay, `structural` history-free mode,
                        Or-aware best-branch classification, simulation-based
                        producer check, and blockers as output.  This is
                        trace_back's real sibling.
  * trace_back       -- pilot's projected planner (affine inversion, copy-src,
                        indirect-table inversion, coast leaves, choice locks,
                        opaque-loop guard, recursion-to-steerable).

The wall: trace_back hand-rolls writer-selection consistency (`_rank_writers`,
`_guard_requires_other_state`, `_is_self_gated`).  Fix 1 (one-hot literal)
works; the affine/even-step case fails.

Reasoning result (validated below): NEITHER why_cause NOR projected_cause
solves Fix 1 or the even-step case in `structural` mode -- both treat a false
guard leaf as a freely-reachable proximate move.  The single missing piece,
identical in both, is a PROJECTED ORACLE that *pins* derived/held tags per
sub-goal:

  * Fix 1 (one-hot)  -> pin the held state register and its mutually-exclusive
                        peers; a writer whose guard needs a pinned peer flipped
                        is COUNTERFACTUAL, not a frontier.
  * even-step (affine)-> pin the affine-source prerequisite (CurStep==1 to
                        produce CurStep==2) AND its one-hop-derived values
                        (valstepisodd = CurStep % 2 = 1); a writer whose guard
                        reads a pinned-derived tag at a contradicting value is
                        COUNTERFACTUAL.

Everything else trace_back needs (producer check, Or classification, blockers)
already exists in projected_cause and should be REUSED, not reinvented.

This spike builds the projected oracle on the real shared substrate
(`attribute`/`evaluate_sp`, `_written_value_for_tag`, `_invert_affine`) and
proves it reproduces Fix 1 (parity with `_rank_writers`) AND fixes the
even-step case (where `_rank_writers` picks wrong).
"""

from __future__ import annotations

from typing import Any

from pyrung import Bool, Int, Program, calc, copy, out, rung
from pyrung.core.analysis.causal.support import _condition_tag_name
from pyrung.core.analysis.pdg import build_program_graph, resolve_rung
from pyrung.core.analysis.sp_tree import attribute, evaluate_sp
from pyrung.core.analysis.sp_values import (
    _calc_writer_for_tag,
    _values_match,
    _written_value_for_tag,
)
from pyrung.core.analysis.pilot.trace import _invert_affine, _rank_writers
from pyrung.core.crossing import Affine, Literal


# ---------------------------------------------------------------------------
# The projected oracle -- snapshot + per-sub-goal overlay, some tags PINNED.
# ---------------------------------------------------------------------------


class _ProjectedView:
    """ScanContext stand-in: reads go overlay -> snapshot. (`cond.evaluate`)"""

    __slots__ = ("_snap", "_overlay")

    def __init__(self, snap: dict[str, Any], overlay: dict[str, Any]) -> None:
        self._snap = snap
        self._overlay = overlay

    def get_tag(self, name: str, default: Any = None) -> Any:
        if name in self._overlay:
            v = self._overlay[name]
        else:
            v = self._snap.get(name, default)
        return v if v is not None else default

    def get_memory(self, key: str, default: Any = None) -> Any:
        return default


def _oracle(snap: dict[str, Any], overlay: dict[str, Any]):
    view = _ProjectedView(snap, overlay)
    return lambda cond: bool(cond.evaluate(view))


def _derive_one_hop(
    overlay: dict[str, Any],
    snap: dict[str, Any],
    pdg: Any,
    program: Any,
) -> dict[str, Any]:
    """Partial-eval ONE hop: for each tag with a single calc writer whose
    source tags are all resolved in overlay, compute and pin its value.

    This is the principled version of the affine patch that botched the
    even-step case: instead of reading valstepisodd from the *current*
    snapshot, we recompute it from the *projected* CurStep.
    """
    out_overlay = dict(overlay)
    view = _ProjectedView(snap, out_overlay)
    for rn in pdg.rung_nodes:
        ro = resolve_rung(program, rn)
        if ro is None:
            continue
        for tag in list(rn.ote_writes) + [
            getattr(getattr(i, "dest", None), "name", None) for i in ro._instructions
        ]:
            if tag is None or tag in out_overlay:
                continue
            ci = _calc_writer_for_tag(ro, tag)
            if ci is None:
                continue
            try:
                out_overlay[tag] = ci.expression.evaluate(view)
            except Exception:
                continue
    return out_overlay


# ---------------------------------------------------------------------------
# The shared classifier -- one writer, under one oracle, with a pinned set.
# ---------------------------------------------------------------------------


def classify_writer(
    ri: int,
    tag: str,
    value: Any,
    snap: dict[str, Any],
    pinned_overlay: dict[str, Any],
    pinned: set[str],
    pdg: Any,
    program: Any,
) -> dict[str, Any] | None:
    """Classify a candidate writer of (tag, value) under the projected oracle.

    Returns None if the writer cannot produce `value` at all.  Otherwise a
    dict with:
      counterfactual: a FALSE guard leaf reads a PINNED tag -> dead branch
      frontier:       the non-pinned FALSE guard leaves (the real prereqs)
    """
    ro = resolve_rung(program, pdg.rung_nodes[ri])
    if ro is None:
        return None
    wv = _written_value_for_tag(ro, tag)

    # producer check + build the affine-source prerequisite overlay
    overlay = dict(pinned_overlay)
    local_pinned = set(pinned)
    if isinstance(wv, Literal):
        if not _values_match(wv.value, value):
            return None
    elif isinstance(wv, Affine):
        if wv.source == tag:
            src_val = _invert_affine(wv, value)
            if src_val is None:
                return None
            # establishing (tag==value) requires source (==tag) to hold src_val
            overlay[tag] = src_val
            local_pinned.add(tag)
            overlay = _derive_one_hop(overlay, snap, pdg, program)
            # pin the derived tags too (they are fixed by the prerequisite)
            local_pinned |= {k for k in overlay if k not in pinned_overlay}
        else:
            src_val = _invert_affine(wv, value)
            if src_val is None:
                return None
    else:
        return None  # UNKNOWN -- out of scope for the spike

    sp = ro.sp_tree()
    if sp is None:
        return {"ri": ri, "counterfactual": False, "frontier": []}

    oracle = _oracle(snap, overlay)
    false_leaves = [
        _condition_tag_name(a.condition) for a in attribute(sp, oracle) if not a.value
    ]
    counterfactual = any(t in local_pinned for t in false_leaves)
    frontier = [t for t in false_leaves if t not in local_pinned]
    return {"ri": ri, "counterfactual": counterfactual, "frontier": frontier}


def select_writer(
    tag: str,
    value: Any,
    snap: dict[str, Any],
    pinned_overlay: dict[str, Any],
    pinned: set[str],
    pdg: Any,
    program: Any,
) -> dict[str, Any] | None:
    """Projected-oracle writer selection -- the replacement for _rank_writers."""
    cands = []
    for ri in sorted(pdg.writers_of.get(tag, frozenset())):
        c = classify_writer(ri, tag, value, snap, pinned_overlay, pinned, pdg, program)
        if c is not None:
            cands.append(c)
    # live (non-counterfactual) first, then fewest open frontier leaves
    cands.sort(key=lambda c: (c["counterfactual"], len(c["frontier"])))
    return cands[0] if cands else None


# ---------------------------------------------------------------------------
# Fixture A -- one-hot pipeline tag (Fix 1 / S_StateCompleteBool).
# ---------------------------------------------------------------------------


def fixture_one_hot():
    S_Starting = Bool("S_Starting")
    S_Clearing = Bool("S_Clearing")
    Blower__init = Int("Blower__init")
    SCB = Bool("SCB")  # S_StateCompleteBool stand-in

    with Program(strict=False) as logic:
        with rung(S_Clearing):  # W_cf: counterfactual writer
            copy(1, SCB)
        with rung(S_Starting, Blower__init == 1):  # W_live: held-state writer
            copy(1, SCB)

    pdg = build_program_graph(logic)
    snap = {"S_Starting": True, "S_Clearing": False, "Blower__init": 0, "SCB": False}
    # held one-hot state + its mutually-exclusive peer, pinned
    pinned_overlay = {"S_Starting": True, "S_Clearing": False}
    pinned = {"S_Starting", "S_Clearing"}
    return logic, pdg, snap, pinned_overlay, pinned


# ---------------------------------------------------------------------------
# Fixture B -- the even-step counter, faithful to blower.py R15/R16/R17/R19.
# ---------------------------------------------------------------------------


def fixture_even_step():
    CurStep = Int("CurStep")
    valstepisodd = Int("valstepisodd")
    Trans = Int("Trans")
    xPause = Int("xPause")
    x_TimerDone = Bool("x_TimerDone")
    x_FB = Bool("x_FB")
    init = Int("Blower__init")

    with Program(strict=False) as logic:
        # R8': transition trigger -- Trans=1 when at step 1, timed out, feedback
        with rung(CurStep == 1, x_TimerDone, x_FB):
            copy(1, Trans)
        # R15: parity (the derived tag)
        with rung():
            calc(CurStep % 2, valstepisodd)
        # R16: EVEN-STEP HANDLING -- fires only when CurStep is EVEN
        with rung(valstepisodd != 1, xPause == 0):
            calc(CurStep + 1, CurStep)
        # R17: step advance on transition -- fires regardless of parity
        with rung(Trans == 1):
            calc(CurStep + 1, CurStep)
        # R19: init complete at step 2
        with rung(CurStep == 2):
            copy(1, init)

    pdg = build_program_graph(logic)
    # Adversarial snapshot: currently sitting at an EVEN step, so a
    # current-snapshot check sees R16's guard satisfied and picks it -- wrong.
    snap = {
        "CurStep": 2,
        "valstepisodd": 0,
        "Trans": 0,
        "xPause": 0,
        "x_TimerDone": False,
        "x_FB": False,
        "Blower__init": 0,
    }
    return logic, pdg, snap


def _label(pdg, ri):
    return f"R{ri}"


def main() -> int:
    print("=" * 72)
    print("SPIKE: projected-oracle attribution kernel")
    print("=" * 72)

    # ---- Claim 1: Fix 1 reproduction (parity with _rank_writers) ----------
    print("\n[Claim 1] one-hot pipeline tag SCB==1  (Fix 1)")
    logic, pdg, snap, pinned_overlay, pinned = fixture_one_hot()

    ranked = _rank_writers(
        pdg.writers_of.get("SCB", frozenset()), pdg, logic, "SCB", True, snap,
        frozenset({"S_Starting", "S_Clearing"}),
    )
    print(f"  today  _rank_writers order : {[_label(pdg, r) for r in ranked]}")
    print(f"         -> picks {_label(pdg, ranked[0])}")

    sel = select_writer("SCB", True, snap, pinned_overlay, pinned, pdg, logic)
    print(f"  spike  projected-oracle    : picks {_label(pdg, sel['ri'])}"
          f"  counterfactual={sel['counterfactual']}  frontier={sel['frontier']}")
    same = ranked[0] == sel["ri"]
    print(f"  RESULT: {'PASS -- parity' if same else 'FAIL -- diverged'} "
          f"(both should surface Blower__init as the frontier)")

    # ---- Claim 2: even-step case (where _rank_writers picks wrong) ---------
    print("\n[Claim 2] even-step counter CurStep==2  (the affine wall)")
    logic, pdg, snap = fixture_even_step()
    curstep_writers = pdg.writers_of.get("CurStep", frozenset())

    ranked = _rank_writers(curstep_writers, pdg, logic, "CurStep", 2, snap)
    affine_ranked = [r for r in ranked]
    print(f"  today  _rank_writers order : {[_label(pdg, r) for r in affine_ranked]}")
    print(f"         -> trace_back uses first viable affine writer: "
          f"{_label(pdg, affine_ranked[0]) if affine_ranked else None}")

    # spike WITHOUT one-hop derive (overlay only CurStep=1) -> still wrong
    cands_noderive = []
    for ri in sorted(curstep_writers):
        ro = resolve_rung(logic, pdg.rung_nodes[ri])
        wv = _written_value_for_tag(ro, "CurStep")
        if not isinstance(wv, Affine):
            continue
        overlay = {"CurStep": 1}  # NO derive: valstepisodd stays snapshot=0
        oracle = _oracle(snap, overlay)
        sp = ro.sp_tree()
        false_leaves = [
            _condition_tag_name(a.condition) for a in attribute(sp, oracle) if not a.value
        ]
        cf = "CurStep" in false_leaves or "valstepisodd" in false_leaves
        cands_noderive.append((ri, cf, false_leaves))
    print(f"  spike  NO one-hop derive   : "
          + ", ".join(f"{_label(pdg, ri)}(blockers={fl})" for ri, _cf, fl in cands_noderive))

    # spike WITH one-hop derive -> correct
    sel = select_writer("CurStep", 2, snap, {}, set(), pdg, logic)
    print(f"  spike  WITH one-hop derive : picks {_label(pdg, sel['ri'])}"
          f"  counterfactual={sel['counterfactual']}  frontier={sel['frontier']}")
    # report every candidate's classification for transparency
    for ri in sorted(curstep_writers):
        c = classify_writer(ri, "CurStep", 2, snap, {}, set(), pdg, logic)
        if c is not None:
            print(f"           {_label(pdg, ri)}: counterfactual={c['counterfactual']} "
                  f"frontier={c['frontier']}")

    correct = sel is not None and sel["frontier"] == ["Trans"]
    today_wrong = bool(affine_ranked) and affine_ranked[0] != sel["ri"]
    print(f"  RESULT: {'PASS' if correct else 'FAIL'} -- projected oracle selects the "
          f"Trans rung (R17); R16 correctly rejected as counterfactual")
    print(f"          (today's _rank_writers picks {_label(pdg, affine_ranked[0])} "
          f"-> {'the even-step rung, WRONG' if today_wrong else 'same'})")

    print("\n" + "=" * 72)
    print("Conclusion: the one new primitive is the projected oracle with PINNING")
    print("(held one-hot peers + affine-source prerequisite + one-hop derive).")
    print("It rides on the SAME attribute()/evaluate_sp() substrate that")
    print("why_cause and projected_cause already use -> migrate trace_back's")
    print("bool/attribution hop onto projected_cause(structural=True) + this")
    print("overlay; keep affine/indirect/coast/choice as trace_back deltas.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
