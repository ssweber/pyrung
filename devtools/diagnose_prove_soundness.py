"""Diagnose prove() optimization soundness reproducers.

This is an internal fuzz-triage helper.  It imports a reproducer module,
captures the program/property passed to prove(), then reruns optimized,
unoptimized, and forced-keep elision variants to identify the smallest
elided-tag set that restores agreement with the unoptimized result.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from types import ModuleType
from typing import Any

from pyrung.core.analysis.prove import Counterexample, Intractable
from pyrung.core.analysis.prove import prove as _real_prove
from pyrung.core.analysis.prove.results import Journal


@dataclass(frozen=True)
class ProveCall:
    program: Any
    conditions: tuple[Any, ...]
    kwargs: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnose optimized vs unoptimized prove() disagreements."
    )
    parser.add_argument("reproducer", type=Path, help="Path to a fuzz reproducer .py file.")
    parser.add_argument(
        "--test-name",
        default="test_reproducer",
        help="Function to run in the reproducer module. Defaults to test_reproducer.",
    )
    parser.add_argument(
        "--max-subset-size",
        type=int,
        default=2,
        help="Largest forced-keep elided-tag subset to try. Defaults to 2.",
    )
    parser.add_argument(
        "--full-journal",
        action="store_true",
        help="Print full per-tag journal decisions instead of a compact summary.",
    )
    return parser.parse_args()


def load_module(path: Path) -> ModuleType:
    resolved = path.resolve()
    spec = importlib.util.spec_from_file_location(resolved.stem, resolved)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {resolved}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[resolved.stem] = module
    spec.loader.exec_module(module)
    return module


def capture_reproducer_call(module: ModuleType, test_name: str) -> tuple[ProveCall, str | None]:
    test_fn = getattr(module, test_name, None)
    if test_fn is None:
        raise RuntimeError(f"{module.__name__} has no {test_name}()")

    calls: list[ProveCall] = []
    original_prove = getattr(module, "prove", None)

    def capturing_prove(program: Any, *conditions: Any, **kwargs: Any) -> Any:
        calls.append(ProveCall(program, tuple(conditions), dict(kwargs)))
        return _real_prove(program, *conditions, **kwargs)

    vars(module)["prove"] = capturing_prove
    failure: str | None = None
    try:
        test_fn()
    except AssertionError as exc:
        failure = str(exc)
    finally:
        if original_prove is not None:
            vars(module)["prove"] = original_prove

    optimized_calls = [call for call in calls if not call.kwargs.get("_skip_optimizations")]
    if not optimized_calls:
        raise RuntimeError("No optimized prove() call was captured from the reproducer.")
    return optimized_calls[0], failure


def prove_with(
    call: ProveCall,
    *,
    skip_optimizations: bool,
    journal: bool = True,
) -> Any:
    kwargs = dict(call.kwargs)
    kwargs["_skip_optimizations"] = skip_optimizations
    kwargs["journal"] = journal
    return _real_prove(call.program, *call.conditions, **kwargs)


@contextmanager
def force_keep_elided(tag_names: frozenset[str]) -> Iterator[None]:
    from pyrung.core.analysis.prove.elision.abstract import _ScanLocalStateElider
    from pyrung.core.analysis.prove.elision.concrete import _ConcreteStateElider

    original_prove_tag = _ScanLocalStateElider._prove_tag
    original_can_elide = _ConcreteStateElider._can_elide

    def prove_tag(self: Any, tag_name: str, retained: frozenset[str], accepted: Any) -> Any:
        if tag_name in tag_names:
            return None
        return original_prove_tag(self, tag_name, retained, accepted)

    def can_elide(self: Any, candidate: str, retained: frozenset[str]) -> bool:
        if candidate in tag_names:
            return False
        return original_can_elide(self, candidate, retained)

    type.__setattr__(_ScanLocalStateElider, "_prove_tag", prove_tag)
    type.__setattr__(_ConcreteStateElider, "_can_elide", can_elide)
    try:
        yield
    finally:
        type.__setattr__(_ScanLocalStateElider, "_prove_tag", original_prove_tag)
        type.__setattr__(_ConcreteStateElider, "_can_elide", original_can_elide)


def result_name(result: Any) -> str:
    return type(result).__name__


def result_matches(left: Any, right: Any) -> bool:
    if isinstance(left, Intractable) or isinstance(right, Intractable):
        return False
    return type(left) is type(right)


def trace_text(result: Any) -> str:
    trace = getattr(result, "trace", None)
    if trace is None:
        return "(none)"
    return "\n".join(f"  inputs={step.inputs} scans={step.scans}" for step in trace)


def elided_tags(journal: Journal | None) -> list[tuple[str, str]]:
    if journal is None:
        return []
    tags: list[tuple[str, str]] = []
    for name, entry in journal.tags.items():
        if entry.outcome.startswith("elided:"):
            tags.append((name, entry.outcome.removeprefix("elided:")))
    return sorted(tags)


def absorbed_tags(journal: Journal | None) -> list[tuple[str, str]]:
    if journal is None:
        return []
    tags: list[tuple[str, str]] = []
    for name, entry in journal.tags.items():
        for decision in entry.decisions:
            if decision.kind == "absorption" and decision.outcome == "absorbed":
                tags.append((name, decision.reason))
    return sorted(tags)


def print_journal(label: str, journal: Journal | None, *, full: bool) -> None:
    print()
    print(f"{label} journal:")
    if journal is None:
        print("  (none)")
        return
    if journal.notes:
        print("  notes:")
        for note in journal.notes:
            print(f"    - {note}")
    for name, method in elided_tags(journal):
        print(f"  elided {name}: {method}")
    for name, reason in absorbed_tags(journal):
        print(f"  absorbed {name}: {reason}")
    if not full:
        return
    for name, entry in journal.tags.items():
        print(f"  [{name}] outcome={entry.outcome} domain={entry.domain}")
        for decision in entry.decisions:
            print(
                "    "
                f"{decision.pass_name} {decision.kind} "
                f"{decision.outcome}: {decision.reason} {decision.detail}"
            )


def diagnose_forced_keep(
    call: ProveCall, unoptimized: Any, candidates: list[str], limit: int
) -> None:
    if not candidates:
        print()
        print("No elided tags found to force-keep.")
        return

    print()
    print("force-keep elision candidates:")
    found: list[tuple[str, ...]] = []
    max_size = min(limit, len(candidates))
    for size in range(1, max_size + 1):
        for subset in combinations(candidates, size):
            with force_keep_elided(frozenset(subset)):
                result = prove_with(call, skip_optimizations=False, journal=True)
            marker = "MATCH" if result_matches(result, unoptimized) else ""
            names = ", ".join(subset)
            print(f"  {{{names}}} -> {result_name(result)} {marker}".rstrip())
            if marker:
                found.append(subset)
        if found:
            break

    print()
    print("minimal restoring sets:")
    if not found:
        print(f"  (none up to size {max_size})")
        return
    for subset in found:
        print(f"  {{{', '.join(subset)}}}")


def main() -> int:
    args = parse_args()
    module = load_module(args.reproducer)
    call, failure = capture_reproducer_call(module, args.test_name)

    optimized = prove_with(call, skip_optimizations=False, journal=True)
    unoptimized = prove_with(call, skip_optimizations=True, journal=True)

    print(f"reproducer: {args.reproducer}")
    if failure:
        print(f"reproducer assertion: {failure}")
    print(f"optimized:   {result_name(optimized)}")
    print(f"unoptimized: {result_name(unoptimized)}")
    print()
    print("unoptimized trace:")
    print(trace_text(unoptimized))

    print_journal("optimized", getattr(optimized, "journal", None), full=args.full_journal)
    print_journal("unoptimized", getattr(unoptimized, "journal", None), full=args.full_journal)

    candidates = [name for name, _method in elided_tags(getattr(optimized, "journal", None))]
    diagnose_forced_keep(call, unoptimized, candidates, args.max_subset_size)

    if isinstance(optimized, Counterexample) and not isinstance(unoptimized, Counterexample):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
