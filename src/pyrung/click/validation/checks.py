"""The pyrung checks that validation runs, and the order it runs them in."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pyrung.click.profile import HardwareProfile
    from pyrung.click.tag_map import TagMap
    from pyrung.core.program import Program
    from pyrung.core.validation.walker import ProgramFacts, ProgramLocation

    from .findings import ClickFinding, ValidationMode
    from .resolve import _ResolutionCache


@dataclass(frozen=True)
class Context:
    """What a check may read about the program that validation checks."""

    program: Program
    facts: ProgramFacts
    sites: list[tuple[Any, ProgramLocation]]
    tag_map: TagMap
    mode: ValidationMode
    resolver: _ResolutionCache
    profile: HardwareProfile | None = None


@dataclass(frozen=True)
class Check:
    """One check, and where it runs among the others.

    A ``program`` check runs once, as ``run(context)``. An ``instruction`` check runs on
    each instruction, as ``run(instruction, location, context)``. A ``profile`` check runs
    the same way, after the others, and only when the hardware profile loads. Within a
    phase the checks run by ``order``. That order keeps the findings in a fixed order,
    which tests and the sweep against main compare.
    """

    phase: Literal["program", "instruction", "profile"]
    order: int
    run: Callable[..., list[ClickFinding]]
