"""Two-phase state-key elision: abstract pre-filter then concrete kernel proofs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import ProgramGraph
from pyrung.core.analysis.simplified import Expr
from pyrung.core.kernel import CompiledKernel

from .abstract import _pass_abstract
from .concrete import (  # noqa: F401
    _collect_forced_true_coverage,
    _ConcreteStateElider,
    _pass_concrete_batch,
)

if TYPE_CHECKING:
    from pyrung.core.program import Program


@dataclass
class _ElisionContext:
    program: Program
    graph: ProgramGraph
    stateful_dims: dict[str, tuple[Any, ...]]
    nondeterministic_dims: dict[str, tuple[Any, ...]]
    compiled: CompiledKernel | None
    elided: dict[str, str]
    proof_details: dict[str, tuple[tuple[str, str], ...]]
    progress: Callable[[str], None] | None
    progress_prefix: Callable[[], str] | None
    _original_stateful_dims: dict[str, tuple[Any, ...]]
    observer_exprs: tuple[Expr, ...]

    def emit(self, msg: str) -> None:
        if self.progress is not None:
            self.progress(msg)


def _elide_scan_local_stateful_dims(
    program: Program,
    graph: ProgramGraph,
    stateful_dims: Mapping[str, tuple[Any, ...]],
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    *,
    compiled: CompiledKernel | None = None,
    observer_exprs: tuple[Expr, ...] = (),
    progress: Callable[[str], None] | None = None,
    progress_prefix: Callable[[], str] | None = None,
) -> tuple[dict[str, tuple[Any, ...]], dict[str, str], dict[str, tuple[tuple[str, str], ...]]]:
    """Return (reduced stateful dims, elided tag → method, tag → proof detail) after conservative elision."""
    if not stateful_dims:
        return {}, {}, {}

    ctx = _ElisionContext(
        program=program,
        graph=graph,
        stateful_dims=dict(stateful_dims),
        nondeterministic_dims=dict(nondeterministic_dims),
        compiled=compiled,
        elided={},
        proof_details={},
        progress=progress,
        progress_prefix=progress_prefix,
        _original_stateful_dims=dict(stateful_dims),
        observer_exprs=observer_exprs,
    )
    ctx.emit(
        "elision | starting scan-local state elision"
        f" | stateful={len(stateful_dims):,}"
        f" | inputs={len(nondeterministic_dims):,}"
    )

    _pass_abstract(ctx)
    if ctx.stateful_dims:
        _pass_concrete_batch(ctx)

    ctx.emit(
        "elision complete"
        f" | removed={len(stateful_dims) - len(ctx.stateful_dims):,}"
        f" | retained={len(ctx.stateful_dims):,}"
    )

    return ctx.stateful_dims, dict(ctx.elided), dict(ctx.proof_details)
