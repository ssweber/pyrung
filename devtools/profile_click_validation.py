"""Profile Click strict validation and export against a generated project fixture."""

from __future__ import annotations

import argparse
import cProfile
import importlib
import io
import pstats
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pyrung.click import pyrung_to_ladder
from pyrung.click.ladder._exporter import _LadderExporter
from pyrung.click.validation import ClickValidationReport, validate_click_program
from pyrung.click.validation import identity as identity_validation
from pyrung.core.analysis import build_program_graph, collect_program_tags
from pyrung.core.program import Program
from pyrung.core.tag import Tag


def _rung_count(program: Any) -> int:
    def count_rung(rung: Any) -> int:
        return 1 + sum(count_rung(branch) for branch in rung._branches)

    return sum(count_rung(rung) for rung in program.rungs) + sum(
        count_rung(rung) for rungs in program.subroutines.values() for rung in rungs
    )


def _reset_caches(program: Any, tag_map: Any) -> None:
    program._cached_graph = None
    tag_map._mapped_slots_cache = None


def _median_ms(
    operation: Callable[[], object],
    *,
    reset: Callable[[], None],
    repeat: int,
) -> float:
    samples: list[float] = []
    for _ in range(repeat):
        reset()
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1000.0)
    return statistics.median(samples)


def _print_profile(operation: Callable[[], object], *, top: int) -> None:
    profiler = cProfile.Profile()
    profiler.enable()
    operation()
    profiler.disable()

    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative")
    stats.print_stats(top)
    print(stream.getvalue())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--module",
        default="tests.fixtures.tumbler",
        help="module exposing the Program as 'logic'",
    )
    parser.add_argument(
        "--tags-module",
        default=None,
        help="module exposing the TagMap as 'mapping' (defaults to MODULE.tags)",
    )
    parser.add_argument("--repeat", type=int, default=7, help="cold timing samples")
    parser.add_argument("--top", type=int, default=30, help="cProfile rows to print")
    args = parser.parse_args()

    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.top < 1:
        parser.error("--top must be at least 1")

    logic_module = importlib.import_module(args.module)
    tags_module_name = args.tags_module or f"{args.module}.tags"
    tags_module = importlib.import_module(tags_module_name)
    program = logic_module.logic
    tag_map = tags_module.mapping

    reset = lambda: _reset_caches(program, tag_map)

    def full_graph_collector(candidate: Program) -> tuple[Tag, ...]:
        return tuple(build_program_graph(candidate).tags.values())

    def strict_validation() -> ClickValidationReport:
        return validate_click_program(program, tag_map, mode="strict")

    def unchecked_export() -> object:
        return pyrung_to_ladder(program, tag_map, validate=False)

    def skip_precheck(_exporter: _LadderExporter) -> None:
        return None

    def render_and_roundtrip() -> object:
        with patch.object(_LadderExporter, "_run_precheck", skip_precheck):
            return pyrung_to_ladder(program, tag_map, validate=True)

    slot_count = len(tag_map.mapped_slots())
    print(
        f"{args.module}: {_rung_count(program)} rungs, "
        f"{slot_count} mapped slots, median of {args.repeat} cold runs"
    )

    collector_ms = _median_ms(
        lambda: collect_program_tags(program), reset=reset, repeat=args.repeat
    )
    graph_ms = _median_ms(lambda: build_program_graph(program), reset=reset, repeat=args.repeat)
    validation_ms = _median_ms(strict_validation, reset=reset, repeat=args.repeat)
    render_ms = _median_ms(unchecked_export, reset=reset, repeat=args.repeat)
    roundtrip_ms = _median_ms(render_and_roundtrip, reset=reset, repeat=args.repeat)
    print(f"tag collector:          {collector_ms:8.2f} ms")
    print(f"full dependency graph:  {graph_ms:8.2f} ms")
    print(f"strict validation:      {validation_ms:8.2f} ms")
    print(f"render only:            {render_ms:8.2f} ms")
    print(f"render plus round-trip: {roundtrip_ms:8.2f} ms")
    print(f"estimated strict export:{validation_ms + roundtrip_ms:8.2f} ms")

    with patch.object(identity_validation, "collect_program_tags", full_graph_collector):
        graph_validation_ms = _median_ms(strict_validation, reset=reset, repeat=args.repeat)
    print(f"validation via graph:   {graph_validation_ms:8.2f} ms")
    print(f"estimated graph export: {graph_validation_ms + roundtrip_ms:8.2f} ms")

    reset()
    report = strict_validation()
    finding_count = len(report.errors) + len(report.warnings) + len(report.hints)
    print(f"strict findings:        {finding_count:8d}")

    print("\nCurrent strict-validation profile:")
    reset()
    _print_profile(strict_validation, top=args.top)


if __name__ == "__main__":
    main()
