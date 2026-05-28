"""Targeted POR (partial-order reduction) fuzz tests.

Two oracles that exercise POR invariants directly, rather than relying on
the subset-differential tests to randomly include it:

1. **Verdict agreement** — ``prove()`` with POR on must agree with POR off
   (all other optimizations held constant at production defaults).  Every
   example tests POR, not ~50% of them.

2. **Independence commutativity** — For each pair of inputs declared
   independent by the static analysis, verify via PLC simulation that
   flipping A-then-B and B-then-A from the same state yield the same
   successor.  Catches bugs in ``_influenced_rungs`` or the read/write
   overlap check that would silently misclassify dependent actions.
"""

from __future__ import annotations

import gc
from dataclasses import replace

import hypothesis.strategies as st
from hypothesis import Phase, given, note, settings

from pyrung.core import PLC
from pyrung.core.analysis.prove import (
    Intractable,
    _build_explore_context,
    prove,
)
from pyrung.core.analysis.prove.passes import _OptConfig

from .conftest import DEPTH_BUDGET, DT, MAX_EXAMPLES, MAX_STATES
from .minimize import minimize
from .reproducer import format_subset_reproducer, write_reproducer
from .strategies import (
    build_program,
    build_property,
    program_specs,
    property_specs,
)

_POR_ON = replace(_OptConfig(), partial_order_reduction=True)
_POR_OFF = replace(_OptConfig(), partial_order_reduction=False)

_SETTINGS = settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    phases=[Phase.explicit, Phase.reuse, Phase.generate],
)


# --------------------------------------------------------------------------
# Oracle 1: prove() verdict agreement — POR on vs POR off
# --------------------------------------------------------------------------


def _por_disagrees(spec, prop_spec) -> bool:
    try:
        program = build_program(spec)
        prop = build_property(prop_spec)
        off = prove(
            program,
            prop,
            max_states=MAX_STATES,
            depth_budget=DEPTH_BUDGET,
            _opt_config=_POR_OFF,
        )
        if isinstance(off, Intractable):
            return False
        on = prove(
            program,
            prop,
            max_states=MAX_STATES,
            depth_budget=DEPTH_BUDGET,
            _opt_config=_POR_ON,
        )
        if isinstance(on, Intractable):
            return False
        return type(on) is not type(off)
    except Exception:
        return False
    finally:
        gc.collect()


def test_por_verdict_agreement() -> None:
    """POR must not change the prove() verdict."""
    failures: list[str] = []

    @given(data=st.data())
    @_SETTINGS
    def inner(data: st.DataObject) -> None:
        try:
            spec = data.draw(program_specs(soundness_only=True))
            prop_spec = data.draw(property_specs(spec.pool))
            if not _por_disagrees(spec, prop_spec):
                return

            note("POR verdict disagreement found — minimizing")
            spec = minimize(spec, lambda s: _por_disagrees(s, prop_spec))
            program = build_program(spec)
            prop = build_property(prop_spec)
            off = prove(
                program,
                prop,
                max_states=MAX_STATES,
                depth_budget=DEPTH_BUDGET,
                _opt_config=_POR_OFF,
            )
            on = prove(
                program,
                prop,
                max_states=MAX_STATES,
                depth_budget=DEPTH_BUDGET,
                _opt_config=_POR_ON,
            )
            code = format_subset_reproducer(
                spec,
                prop_spec,
                frozenset({"partial_order_reduction"}),
                type(on).__name__,
                type(off).__name__,
            )
            note(f"\n--- Reproducer ---\n{code}")
            path = write_reproducer(code, "por_verdict")
            if path is None:
                return
            note(f"Written to {path}")
            failures.append(str(path))
        finally:
            gc.collect()

    inner()

    if failures:
        raise AssertionError(
            f"Found {len(failures)} POR verdict bugs — see reproducers:\n"
            + "\n".join(f"  {p}" for p in failures)
        )


# --------------------------------------------------------------------------
# Oracle 2: independence commutativity — simulation cross-check
# --------------------------------------------------------------------------


def _find_commutativity_violation(spec):
    """Check that independent actions actually commute under simulation.

    For each pair (A, B) declared independent, from the initial state:
    - Run a few scans to build up interesting state
    - Flip A then step, flip B then step → state S_ab
    - Restart, flip B then step, flip A then step → state S_ba
    - S_ab must equal S_ba on all tags in the write cones of A and B

    Returns (action_a, action_b, s_ab, s_ba) on violation, else None.
    """
    try:
        program = build_program(spec)
        ctx = _build_explore_context(program, _opt_config=_POR_ON)
        if isinstance(ctx, Intractable):
            return None
        rel = ctx.independence_relation
        if rel is None or len(rel.action_names) < 2:
            return None

        for i in range(len(rel.action_names)):
            for j in rel.independent[i]:
                if j <= i:
                    continue
                name_a = rel.action_names[i]
                name_b = rel.action_names[j]
                write_tags = rel.write_tags[i] | rel.write_tags[j]

                plc_ab = PLC(program, dt=DT)
                plc_ba = PLC(program, dt=DT)

                plc_ab.step()
                plc_ba.step()

                plc_ab.patch({name_a: True})
                plc_ab.step()
                plc_ab.patch({name_a: True, name_b: True})
                plc_ab.step()

                plc_ba.patch({name_b: True})
                plc_ba.step()
                plc_ba.patch({name_a: True, name_b: True})
                plc_ba.step()

                tags_ab = plc_ab.current_state.tags
                tags_ba = plc_ba.current_state.tags

                for tag in write_tags:
                    if tag in tags_ab and tag in tags_ba:
                        if tags_ab[tag] != tags_ba[tag]:
                            return (
                                name_a,
                                name_b,
                                {t: tags_ab[t] for t in write_tags if t in tags_ab},
                                {t: tags_ba[t] for t in write_tags if t in tags_ba},
                            )
    except Exception:
        return None
    finally:
        gc.collect()
    return None


def test_independence_commutativity() -> None:
    """Actions declared independent must commute under PLC simulation."""
    failures: list[str] = []

    @given(data=st.data())
    @_SETTINGS
    def inner(data: st.DataObject) -> None:
        try:
            spec = data.draw(program_specs(soundness_only=True))
            violation = _find_commutativity_violation(spec)
            if violation is None:
                return

            name_a, name_b, s_ab, s_ba = violation
            note(
                f"Independence violation: {name_a} and {name_b} declared "
                f"independent but don't commute.\n"
                f"  A-then-B: {s_ab}\n"
                f"  B-then-A: {s_ba}"
            )
            failures.append(f"{name_a} vs {name_b}: AB={s_ab}, BA={s_ba}")
        finally:
            gc.collect()

    inner()

    if failures:
        raise AssertionError(
            f"Found {len(failures)} commutativity violations:\n"
            + "\n".join(f"  {v}" for v in failures)
        )
