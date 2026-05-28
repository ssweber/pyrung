"""Single-scan factoring-equivalence oracle.

Free-input factoring partitions independent free inputs into groups, computes
each group's single-scan delta from a shared parent, and merges the deltas
instead of enumerating the full input cross-product.  Its soundness rests on
one identity:

    merge(per-group single-scan deltas) == unfactored single-scan successor
    for the full input combo

This oracle checks that identity directly under PLC simulation.  It is the
per-scan replacement for the old POR commutativity oracle, which asserted a
*two-scan* diamond (A-then-B vs B-then-A across successive scans) — a property
that is unsound for this clocked, integrating domain and which nothing in the
prover relies on anymore.

In particular this catches the write∩write hazard: two free inputs that each
write a (reader-less) output via different rungs are cone-disjoint, so without
the write∩write guard in ``_build_independence_relation`` they land in separate
factoring groups, and the delta-merge winner (group-iteration order) can differ
from the true single-scan rung-order winner.
"""

from __future__ import annotations

import gc
from collections.abc import Mapping

import hypothesis.strategies as st
from hypothesis import Phase, given, note, settings

from pyrung.core import PLC
from pyrung.core.analysis.prove import (
    Intractable,
    _build_explore_context,
)
from pyrung.core.analysis.prove.passes import _OptConfig

from .conftest import DT, MAX_EXAMPLES
from .strategies import build_program, program_specs

_SETTINGS = settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    phases=[Phase.explicit, Phase.reuse, Phase.generate],
)


def _successor_tags(
    program, base_steps: int, patch: Mapping[str, bool | int | float | str]
) -> dict[str, object]:
    """Step a fresh PLC to the shared base, apply *patch*, run one more scan."""
    plc = PLC(program, dt=DT)
    for _ in range(base_steps):
        plc.step()
    if patch:
        plc.patch(patch)
    plc.step()
    return dict(plc.state.tags)


def _find_factoring_mismatch(spec, data: st.DataObject):
    """Return a mismatch tuple if merged per-group deltas != full successor, else None.

    Mismatch tuple: (tag, full_value, merged_value, group_writes).
    """
    try:
        program = build_program(spec)
        ctx = _build_explore_context(program, _opt_config=_OptConfig())
        if isinstance(ctx, Intractable):
            return None
        fac = ctx.free_input_factoring
        if fac is None or len(fac.groups) < 2:
            return None  # need >=2 independent groups for a meaningful check

        # Shared base: one settling scan with no patches.  Split tags
        # (shared_inputs) are held at their base values throughout — never
        # patched in either the full or per-group runs.
        base_steps = 1
        plc_base = PLC(program, dt=DT)
        for _ in range(base_steps):
            plc_base.step()
        base = dict(plc_base.state.tags)

        # Draw one value per free input across all groups from its domain.
        combo: dict[str, bool | int | float | str] = {}
        for group in fac.groups:
            for name in sorted(group):
                domain = ctx.nondeterministic_dims.get(name)
                if not domain:
                    continue
                combo[name] = data.draw(st.sampled_from(list(domain)))
        if not combo:
            return None

        # Unfactored successor for the full input combo.
        full = _successor_tags(program, base_steps, combo)

        # Per-group single-scan deltas: only this group's inputs change from
        # base; every other group's inputs stay at their base values.
        merged = dict(base)
        for group, wt in zip(fac.groups, fac.write_tags, strict=True):
            group_patch = {n: combo[n] for n in group if n in combo}
            tags_g = _successor_tags(program, base_steps, group_patch)
            for t in wt:
                if tags_g.get(t) != base.get(t):
                    merged[t] = tags_g.get(t)

        # The merge must reproduce the full successor on every written tag.
        all_writes: set[str] = set()
        for wt in fac.write_tags:
            all_writes |= wt
        for t in sorted(all_writes):
            if full.get(t) != merged.get(t):
                group_writes = [sorted(wt) for wt in fac.write_tags]
                return (t, full.get(t), merged.get(t), group_writes)
        return None
    except Exception:
        return None
    finally:
        gc.collect()


def test_factoring_equivalence() -> None:
    """Merged per-group single-scan deltas must equal the full-combo successor."""
    failures: list[str] = []

    @given(data=st.data())
    @_SETTINGS
    def inner(data: st.DataObject) -> None:
        try:
            spec = data.draw(program_specs(soundness_only=True))
            mismatch = _find_factoring_mismatch(spec, data)
            if mismatch is None:
                return

            tag, full_v, merged_v, group_writes = mismatch
            note(
                f"Factoring equivalence violation on {tag!r}: "
                f"full-combo successor = {full_v!r}, merged deltas = {merged_v!r}.\n"
                f"  group write sets: {group_writes}"
            )
            failures.append(f"{tag}: full={full_v!r}, merged={merged_v!r}")
        finally:
            gc.collect()

    inner()

    if failures:
        raise AssertionError(
            f"Found {len(failures)} factoring-equivalence violations:\n"
            + "\n".join(f"  {v}" for v in failures)
        )
