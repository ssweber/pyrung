"""Two-phase state-key elision: abstract pre-filter then concrete kernel proofs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import ProgramGraph
from pyrung.core.kernel import CompiledKernel

from .abstract import _pass_abstract
from .concrete import (  # noqa: F401
    _collect_forced_true_coverage,
    _ConcreteStateElider,
    _pass_concrete_batch,
)

if TYPE_CHECKING:
    from pyrung.core.program import Program


# ---------------------------------------------------------------------------
# Pipeline types
# ---------------------------------------------------------------------------


@dataclass
class _ElisionContext:
    program: Program
    graph: ProgramGraph
    stateful_dims: dict[str, tuple[Any, ...]]
    nondeterministic_dims: dict[str, tuple[Any, ...]]
    compiled: CompiledKernel | None
    elided: dict[str, str]
    progress: Callable[[str], None] | None
    progress_prefix: Callable[[], str] | None
    _original_stateful_dims: dict[str, tuple[Any, ...]]

    def emit(self, msg: str) -> None:
        if self.progress is not None:
            self.progress(msg)


@dataclass(frozen=True)
class _ElisionPass:
    name: str
    description: str
    fn: Callable[[_ElisionContext], None]
    enabled: bool = True


# ---------------------------------------------------------------------------
# Default pipeline
# ---------------------------------------------------------------------------

_DEFAULT_ELISION_PASSES: tuple[_ElisionPass, ...] = (
    _ElisionPass(
        "abstract",
        "Run abstract analysis once, apply all registered rules",
        _pass_abstract,
    ),
    _ElisionPass(
        "concrete_batch",
        "Exhaustive kernel proofs — shared baseline, per-candidate perturbation",
        _pass_concrete_batch,
    ),
)


def _run_elision_pipeline(
    ctx: _ElisionContext,
    passes: tuple[_ElisionPass, ...] = _DEFAULT_ELISION_PASSES,
) -> None:
    for p in passes:
        if p.enabled:
            p.fn(ctx)
        if not ctx.stateful_dims:
            break


# ---------------------------------------------------------------------------
# Public entry point (signature unchanged)
# ---------------------------------------------------------------------------


def _elide_scan_local_stateful_dims(
    program: Program,
    graph: ProgramGraph,
    stateful_dims: Mapping[str, tuple[Any, ...]],
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    *,
    compiled: CompiledKernel | None = None,
    progress: Callable[[str], None] | None = None,
    progress_prefix: Callable[[], str] | None = None,
) -> tuple[dict[str, tuple[Any, ...]], dict[str, str]]:
    """Return (reduced stateful dims, elided tag → method map) after conservative elision."""
    if not stateful_dims:
        return {}, {}

    ctx = _ElisionContext(
        program=program,
        graph=graph,
        stateful_dims=dict(stateful_dims),
        nondeterministic_dims=dict(nondeterministic_dims),
        compiled=compiled,
        elided={},
        progress=progress,
        progress_prefix=progress_prefix,
        _original_stateful_dims=dict(stateful_dims),
    )
    ctx.emit(
        "elision | starting scan-local state elision"
        f" | stateful={len(stateful_dims):,}"
        f" | inputs={len(nondeterministic_dims):,}"
    )
    _run_elision_pipeline(ctx)
    ctx.emit(
        "elision complete"
        f" | removed={len(stateful_dims) - len(ctx.stateful_dims):,}"
        f" | retained={len(ctx.stateful_dims):,}"
    )

    return ctx.stateful_dims, dict(ctx.elided)
