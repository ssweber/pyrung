"""Two-phase state-key elision: abstract pre-filter then concrete kernel proofs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import ProgramGraph
from pyrung.core.kernel import CompiledKernel

from .abstract import _ScanLocalStateElider
from .concrete import _collect_forced_true_coverage, _ConcreteStateElider  # noqa: F401

if TYPE_CHECKING:
    from pyrung.core.program import Program


def _elide_scan_local_stateful_dims(
    program: Program,
    graph: ProgramGraph,
    stateful_dims: Mapping[str, tuple[Any, ...]],
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    *,
    compiled: CompiledKernel | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, tuple[Any, ...]]:
    """Return a reduced stateful-dimension map after conservative elision.

    Two-phase hybrid: abstract provenance analysis first (fast, handles most
    cases), then concrete kernel proofs on whatever abstract retained.
    """
    if not stateful_dims:
        return {}

    def _emit(msg: str) -> None:
        if progress is not None:
            progress(msg)

    _emit(
        "elision | starting scan-local state elision"
        f" | stateful={len(stateful_dims):,}"
        f" | inputs={len(nondeterministic_dims):,}"
    )

    # Phase 1: abstract provenance analysis
    abstract_elider = _ScanLocalStateElider(program, graph, stateful_dims, nondeterministic_dims)
    abstract_reduced, _accepted = abstract_elider.elide()
    abstract_removed = len(stateful_dims) - len(abstract_reduced)
    _emit(
        f"elision | abstract phase complete"
        f" | removed={abstract_removed:,}"
        f" | retained={len(abstract_reduced):,}"
    )

    if not abstract_reduced:
        _emit(f"elision complete | removed={len(stateful_dims):,} | retained=0")
        return {}

    # Phase 2: concrete kernel proofs on what abstract couldn't resolve.
    # Abstract-removed tags can still act as same-scan observers, so keep the
    # full observer set.  Only the abstract-retained tags remain as concrete
    # state dimensions that the proof may need to vary.
    concrete_elider = _ConcreteStateElider(
        program,
        graph,
        stateful_dims,
        nondeterministic_dims,
        state_basis=frozenset(abstract_reduced),
        compiled=compiled,
        progress=progress,
    )
    abstract_retained_names = frozenset(abstract_reduced)
    concrete_retained = set(abstract_retained_names)
    changed = True
    while changed:
        changed = False
        snapshot = set(concrete_retained)
        for tag_name in sorted(snapshot):
            if not concrete_elider._is_concrete_candidate(tag_name):
                continue
            if tag_name not in abstract_retained_names:
                continue
            compare_retained = frozenset(snapshot - {tag_name})
            if concrete_elider._can_elide(tag_name, compare_retained):
                concrete_retained.discard(tag_name)
                changed = True
    concrete_elider._emit(
        "elision complete"
        f" | removed={len(stateful_dims) - len(concrete_retained):,}"
        f" | retained={len(concrete_retained):,}"
    )
    return {name: domain for name, domain in stateful_dims.items() if name in concrete_retained}
