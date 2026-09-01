"""Lazy analysis products shared by validators in one validation run."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.return_guards import ReachChain
    from pyrung.core.program import Program


@dataclass
class ValidationContext:
    """Per-check cache for analysis products used by multiple validators."""

    program: Program

    @cached_property
    def graph(self) -> ProgramGraph:
        """Return the program dependency graph."""
        from pyrung.core.analysis.pdg import build_program_graph

        return build_program_graph(self.program)

    @cached_property
    def produced_domains(self) -> dict[str, tuple[Any, ...]]:
        """Return producer-derived finite value domains."""
        from pyrung.core.analysis.value_domains import produced_value_domains

        return produced_value_domains(self.program, self.graph)

    @cached_property
    def closed_domains(self) -> dict[str, tuple[Any, ...]]:
        """Return complete finite value domains."""
        from pyrung.core.analysis.value_domains import closed_value_domains

        return closed_value_domains(
            self.program,
            self.graph,
            produced=self.produced_domains,
        )

    @cached_property
    def scope_reach_chains(self) -> dict[str, tuple[ReachChain, ...]]:
        """Return return-aware paths from Main to subroutine entries."""
        from pyrung.core.analysis.return_guards import scope_reach_chains

        return scope_reach_chains(self.program, self.graph)


__all__ = ["ValidationContext"]
