"""Mode 1b: subset-differential soundness.

``test_soundness.py`` compares the all-on config against the baseline. That
misses interaction bugs: optimization A can mask optimization B's bug, or a bug
can need A and B together. This module instead compares a random *subset* of
the reduction optimizations against the sound baseline on every example, so a
two-element subset isolates an interacting pair.

Two oracles:
  * ``prove()``            — the verdict (Proven/Counterexample) must agree.
  * ``reachable_states()`` — the projected reachable set must not lose states;
    catches dropped states that never flip a safety verdict.

The reach-extending optimizations (``hidden_event_jumping``,
``pending_settlement``) are pinned on in every config — disabling them under a
finite ``depth_budget`` under-approximates reachability, so they cannot appear
in the subset axis. See ``_REDUCTION_OPTIMIZATIONS`` in ``prove/passes.py``.
"""

from __future__ import annotations

import gc
from dataclasses import replace

import hypothesis.strategies as st
from hypothesis import Phase, given, note, settings

from pyrung.core.analysis.prove import Intractable, prove, reachable_states
from pyrung.core.analysis.prove.passes import _REDUCTION_OPTIMIZATIONS, _OptConfig

from .conftest import DEPTH_BUDGET, MAX_EXAMPLES, MAX_STATES
from .minimize import co_minimize
from .reproducer import (
    format_subset_reachable_reproducer,
    format_subset_reproducer,
    write_reproducer,
)
from .strategies import (
    ProgramSpec,
    PropertySpec,
    build_program,
    build_property,
    program_specs,
    property_specs,
)

_REDUCTIONS = tuple(sorted(_REDUCTION_OPTIMIZATIONS))


@st.composite
def _opt_subsets(draw: st.DrawFn) -> frozenset[str]:
    """A random non-empty subset of the reduction optimizations.

    The empty subset equals the baseline (trivial agreement) and the full set
    equals production (already covered by ``test_optimization_soundness``), so
    the interesting interaction cases are the pairs and triples in between.
    """
    k = draw(st.integers(1, len(_REDUCTIONS)))
    order = draw(st.permutations(_REDUCTIONS))
    return frozenset(order[:k])


def _config_for(names: frozenset[str]) -> _OptConfig:
    """sound_baseline with the named reduction optimizations re-enabled."""
    return replace(_OptConfig.sound_baseline(), **dict.fromkeys(names, True))


_SETTINGS = settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    phases=[Phase.explicit, Phase.reuse, Phase.generate],
)


# --------------------------------------------------------------------------
# Oracle 1: prove() verdict agreement
# --------------------------------------------------------------------------


def _prove_disagrees(spec: ProgramSpec, prop_spec: PropertySpec, names: frozenset[str]) -> bool:
    """True when the subset's prove() verdict differs from the baseline's.

    Intractable on either side means "can't decide" — not a disagreement.
    """
    try:
        program = build_program(spec)
        prop = build_property(prop_spec)
        baseline = prove(
            program,
            prop,
            max_states=MAX_STATES,
            depth_budget=DEPTH_BUDGET,
            _opt_config=_OptConfig.sound_baseline(),
        )
        if isinstance(baseline, Intractable):
            return False
        candidate = prove(
            program,
            prop,
            max_states=MAX_STATES,
            depth_budget=DEPTH_BUDGET,
            _opt_config=_config_for(names),
        )
        if isinstance(candidate, Intractable):
            return False
        return type(candidate) is not type(baseline)
    except Exception:
        return False
    finally:
        gc.collect()


def test_subset_differential_soundness() -> None:
    failures: list[str] = []

    @given(data=st.data())
    @_SETTINGS
    def inner(data: st.DataObject) -> None:
        try:
            spec = data.draw(program_specs(soundness_only=True))
            prop_spec = data.draw(property_specs(spec.pool))
            names = data.draw(_opt_subsets())
            if not _prove_disagrees(spec, prop_spec, names):
                return

            note(f"disagreeing optimization subset: {sorted(names)}")
            min_spec, minimal = co_minimize(
                spec, names, lambda s, n: _prove_disagrees(s, prop_spec, n)
            )
            program = build_program(min_spec)
            prop = build_property(prop_spec)
            baseline = prove(
                program,
                prop,
                max_states=MAX_STATES,
                depth_budget=DEPTH_BUDGET,
                _opt_config=_OptConfig.sound_baseline(),
            )
            candidate = prove(
                program,
                prop,
                max_states=MAX_STATES,
                depth_budget=DEPTH_BUDGET,
                _opt_config=_config_for(minimal),
            )
            code = format_subset_reproducer(
                min_spec,
                prop_spec,
                minimal,
                type(candidate).__name__,
                type(baseline).__name__,
            )
            note(f"\n--- Reproducer ---\n{code}")
            path = write_reproducer(code, "soundness_subset")
            if path is None:
                return
            note(f"Written to {path}")
            failures.append(str(path))
        finally:
            gc.collect()

    inner()

    if failures:
        raise AssertionError(
            f"Found {len(failures)} subset-soundness bugs — see reproducers:\n"
            + "\n".join(f"  {p}" for p in failures)
        )


# --------------------------------------------------------------------------
# Oracle 2: reachable_states() set containment
# --------------------------------------------------------------------------


def _dropped_states(
    spec: ProgramSpec, names: frozenset[str]
) -> frozenset[frozenset[tuple[str, object]]] | None:
    """States in the baseline reachable set but missing from the subset's.

    Returns None when either side is Intractable (can't compare). A non-empty
    result is a soundness bug — a sound reduction may only over-approximate.
    """
    try:
        program = build_program(spec)
        baseline = reachable_states(
            program,
            max_states=MAX_STATES,
            depth_budget=DEPTH_BUDGET,
            _opt_config=_OptConfig.sound_baseline(),
        )
        if isinstance(baseline, Intractable):
            return None
        candidate = reachable_states(
            program,
            max_states=MAX_STATES,
            depth_budget=DEPTH_BUDGET,
            _opt_config=_config_for(names),
        )
        if isinstance(candidate, Intractable):
            return None
        return baseline - candidate
    except Exception:
        return None
    finally:
        gc.collect()


def test_subset_differential_reachability() -> None:
    failures: list[str] = []

    @given(data=st.data())
    @_SETTINGS
    def inner(data: st.DataObject) -> None:
        try:
            spec = data.draw(program_specs(soundness_only=True))
            names = data.draw(_opt_subsets())
            dropped = _dropped_states(spec, names)
            if not dropped:
                return

            note(f"subset dropped {len(dropped)} reachable state(s): {sorted(names)}")
            min_spec, minimal = co_minimize(spec, names, lambda s, n: bool(_dropped_states(s, n)))
            code = format_subset_reachable_reproducer(min_spec, minimal)
            note(f"\n--- Reproducer ---\n{code}")
            path = write_reproducer(code, "soundness_subset_reach")
            if path is None:
                return
            note(f"Written to {path}")
            failures.append(str(path))
        finally:
            gc.collect()

    inner()

    if failures:
        raise AssertionError(
            f"Found {len(failures)} subset-reachability bugs — see reproducers:\n"
            + "\n".join(f"  {p}" for p in failures)
        )
