"""Diagnose prove() and reachable_states() optimization soundness reproducers.

This is an internal fuzz-triage helper.  It imports a reproducer module,
captures the program/property passed to prove() or reachable_states(), then
reruns optimized, unoptimized, and forced-keep elision variants to identify
the smallest elided-tag set that restores agreement with the unoptimized result.

Modes:
  prove        — optimized vs unoptimized prove() disagreement
  reachable    — BFS vs simulation reachable_states() disagreement
  auto         — detect from captured calls (default)
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

from pyrung.core import PLC
from pyrung.core.analysis.prove import Counterexample, Intractable
from pyrung.core.analysis.prove import prove as _real_prove
from pyrung.core.analysis.prove import reachable_states as _real_reachable_states
from pyrung.core.analysis.prove.results import Journal


@dataclass(frozen=True)
class ProveCall:
    program: Any
    conditions: tuple[Any, ...]
    kwargs: dict[str, Any]


@dataclass(frozen=True)
class ReachableCall:
    program: Any
    kwargs: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnose optimized vs unoptimized prove()/reachable_states() disagreements."
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
        "--no-full-journal",
        action="store_false",
        dest="full_journal",
        help="Print compact journal summary instead of full per-tag decisions.",
    )
    parser.set_defaults(full_journal=True)
    parser.add_argument(
        "--mode",
        choices=["auto", "prove", "reachable"],
        default="auto",
        help="Which call type to diagnose. Defaults to auto-detect.",
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


def capture_reproducer_call(
    module: ModuleType,
    test_name: str,
) -> tuple[ProveCall | None, ReachableCall | None, str | None]:
    test_fn = getattr(module, test_name, None)
    if test_fn is None:
        raise RuntimeError(f"{module.__name__} has no {test_name}()")

    prove_calls: list[ProveCall] = []
    reachable_calls: list[ReachableCall] = []

    def capturing_prove(program: Any, *conditions: Any, **kwargs: Any) -> Any:
        prove_calls.append(ProveCall(program, tuple(conditions), dict(kwargs)))
        return _real_prove(program, *conditions, **kwargs)

    def capturing_reachable(program: Any, **kwargs: Any) -> Any:
        reachable_calls.append(ReachableCall(program, dict(kwargs)))
        return _real_reachable_states(program, **kwargs)

    original_prove = getattr(module, "prove", None)
    original_reachable = getattr(module, "reachable_states", None)
    vars(module)["prove"] = capturing_prove
    vars(module)["reachable_states"] = capturing_reachable

    failure: str | None = None
    try:
        test_fn()
    except AssertionError as exc:
        failure = str(exc)
    finally:
        if original_prove is not None:
            vars(module)["prove"] = original_prove
        if original_reachable is not None:
            vars(module)["reachable_states"] = original_reachable

    prove_call: ProveCall | None = None
    reachable_call: ReachableCall | None = None
    optimized_prove = [c for c in prove_calls if not c.kwargs.get("_skip_optimizations")]
    if optimized_prove:
        prove_call = optimized_prove[0]
    optimized_reachable = [c for c in reachable_calls if not c.kwargs.get("_skip_optimizations")]
    if optimized_reachable:
        reachable_call = optimized_reachable[0]
    return prove_call, reachable_call, failure


# ---------------------------------------------------------------------------
# prove() helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# reachable_states() helpers
# ---------------------------------------------------------------------------


def reachable_with(
    call: ReachableCall,
    *,
    skip_optimizations: bool,
    journal: bool = True,
) -> Any:
    kwargs = dict(call.kwargs)
    kwargs["_skip_optimizations"] = skip_optimizations
    kwargs["_journal"] = journal
    return _real_reachable_states(call.program, **kwargs)


# ---------------------------------------------------------------------------
# Force-keep elision monkeypatch
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Shared reporting
# ---------------------------------------------------------------------------


def result_name(result: Any) -> str:
    if isinstance(result, frozenset):
        return f"frozenset({len(result)} states)"
    return type(result).__name__


def result_matches(left: Any, right: Any) -> bool:
    if isinstance(left, Intractable) or isinstance(right, Intractable):
        return False
    if isinstance(left, frozenset) and isinstance(right, frozenset):
        return left == right
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


def get_journal(result: Any) -> Journal | None:
    return getattr(result, "journal", None)


# ---------------------------------------------------------------------------
# prove() diagnosis
# ---------------------------------------------------------------------------


def _property_tag_names(conditions: tuple[Any, ...]) -> list[str]:
    """Extract tag names referenced by the prove() condition."""
    from pyrung.core.analysis.prove.expr import _referenced_tags
    from pyrung.core.analysis.simplified import _condition_to_expr
    from pyrung.core.condition import _as_condition, _normalize_and_condition

    normalized = _normalize_and_condition(
        *conditions,
        coerce=_as_condition,
        empty_error="no conditions",
        group_empty_error="empty group",
    )
    expr = _condition_to_expr(normalized)
    return sorted(_referenced_tags(expr))


def _replay_counterexample(
    program: Any,
    trace: list[Any],
) -> PLC | None:
    """Replay a counterexample trace through a PLC runner."""
    try:
        runner = PLC(program)
    except Exception:
        return None
    for step in trace:
        if step.inputs:
            runner.patch(step.inputs)
        for _ in range(step.scans):
            runner.step()
    return runner


def _format_chain(chain: Any) -> list[str]:
    """Format a CausalChain into display lines."""
    lines: list[str] = []
    for step in chain.steps:
        t = step.transition
        causes = ", ".join(
            f"{c.tag_name}: {c.from_value} -> {c.to_value}" for c in step.proximate_causes
        )
        enables = ", ".join(f"{e.tag_name}={e.value}" for e in step.enabling_conditions)
        parts = []
        if causes:
            parts.append(f"caused by [{causes}]")
        if enables:
            parts.append(f"enabled by [{enables}]")
        qualifier = f" ({', '.join(parts)})" if parts else ""
        lines.append(
            f"    {t.tag_name}: {t.from_value} -> {t.to_value}"
            f" @ scan {t.scan_id}, rung {step.rung_index}{qualifier}"
        )
    return lines


def diagnose_causal_chain(
    call: ProveCall,
    unoptimized: Any,
    restoring_tags: list[str] | None = None,
) -> None:
    """Replay the counterexample and print cause() chains for property tags."""
    if not isinstance(unoptimized, Counterexample):
        return
    if not unoptimized.trace:
        return

    property_tags = _property_tag_names(call.conditions)
    if not property_tags:
        return

    runner = _replay_counterexample(call.program, unoptimized.trace)
    if runner is None:
        return

    print()
    print("causal chain analysis (from counterexample replay):")
    for tag_name in property_tags:
        chain = runner.cause(tag_name)
        if chain is None:
            print(f"  cause({tag_name}): no transition found")
            continue
        print(f"  cause({tag_name}):")
        for line in _format_chain(chain):
            print(line)

    extra = [t for t in (restoring_tags or []) if t not in property_tags]
    for tag_name in extra:
        chain = runner.cause(tag_name)
        if chain is None:
            continue
        print(f"  cause({tag_name}):  [restoring set tag]")
        for line in _format_chain(chain):
            print(line)


def diagnose_prove_forced_keep(
    call: ProveCall, unoptimized: Any, candidates: list[str], limit: int
) -> list[str]:
    if not candidates:
        print()
        print("No elided tags found to force-keep.")
        return []

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
        return []
    for subset in found:
        print(f"  {{{', '.join(subset)}}}")

    restoring: list[str] = []
    seen: set[str] = set()
    for subset in found:
        for tag in subset:
            if tag not in seen:
                restoring.append(tag)
                seen.add(tag)
    return restoring


def diagnose_prove(call: ProveCall, failure: str | None, args: argparse.Namespace) -> int:
    optimized = prove_with(call, skip_optimizations=False, journal=True)
    unoptimized = prove_with(call, skip_optimizations=True, journal=True)

    print("mode: prove")
    if failure:
        print(f"reproducer assertion: {failure}")
    print(f"optimized:   {result_name(optimized)}")
    print(f"unoptimized: {result_name(unoptimized)}")
    print()
    print("unoptimized trace:")
    print(trace_text(unoptimized))

    print_journal("optimized", get_journal(optimized), full=args.full_journal)
    print_journal("unoptimized", get_journal(unoptimized), full=args.full_journal)

    candidates = [name for name, _method in elided_tags(get_journal(optimized))]
    restoring = diagnose_prove_forced_keep(call, unoptimized, candidates, args.max_subset_size)

    diagnose_causal_chain(call, unoptimized, restoring)

    if isinstance(optimized, Counterexample) and not isinstance(unoptimized, Counterexample):
        return 2
    return 0


# ---------------------------------------------------------------------------
# reachable_states() diagnosis
# ---------------------------------------------------------------------------


def diagnose_reachable_forced_keep(
    call: ReachableCall,
    unoptimized: frozenset[frozenset[tuple[str, Any]]],
    candidates: list[str],
    limit: int,
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
                result = reachable_with(call, skip_optimizations=False, journal=True)
            if isinstance(result, Intractable):
                names = ", ".join(subset)
                print(f"  {{{names}}} -> Intractable")
                continue
            marker = "MATCH" if result == unoptimized else ""
            missing = unoptimized - result
            extra = result - unoptimized
            names = ", ".join(subset)
            suffix = ""
            if not marker:
                parts = []
                if missing:
                    parts.append(f"missing={len(missing)}")
                if extra:
                    parts.append(f"extra={len(extra)}")
                suffix = f" ({', '.join(parts)})" if parts else ""
            print(f"  {{{names}}} -> {len(result)} states {marker}{suffix}".rstrip())
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


def _get_reachable_journal(call: ReachableCall, *, skip_optimizations: bool) -> Journal | None:
    """Run reachable_states with journal=True and extract the context journal.

    reachable_states returns a frozenset (no journal attribute), so we use
    the internal _build_reachable_context to get the journal from the context.
    """
    from pyrung.core.analysis.prove import _build_reachable_context

    program = call.program
    kwargs = dict(call.kwargs)
    project_list = list(kwargs.get("project") or [])
    project_names = tuple(project_list) if project_list else None
    scope_list = kwargs.get("scope")
    effective_scope = sorted(set(scope_list or project_list) | set(project_names or ()))
    context = _build_reachable_context(
        program,
        scope=effective_scope,
        project=project_names or (),
        joint_inputs=kwargs.get("joint_inputs", ()),
        exclusive_inputs=kwargs.get("exclusive_inputs", ()),
        _skip_optimizations=skip_optimizations,
        journal=True,
    )
    if isinstance(context, Intractable):
        return None
    return context.journal


def diagnose_reachable(call: ReachableCall, failure: str | None, args: argparse.Namespace) -> int:
    optimized = reachable_with(call, skip_optimizations=False)
    unoptimized = reachable_with(call, skip_optimizations=True)

    print("mode: reachable")
    if failure:
        print(f"reproducer assertion: {failure}")
    print(f"optimized:   {result_name(optimized)}")
    print(f"unoptimized: {result_name(unoptimized)}")

    if isinstance(optimized, frozenset) and isinstance(unoptimized, frozenset):
        missing = unoptimized - optimized
        extra = optimized - unoptimized
        if missing:
            print()
            print("states in unoptimized but NOT in optimized (missed by BFS):")
            for state in sorted(missing, key=str):
                print(f"  {dict(state)}")  # ty: ignore[no-matching-overload]
        if extra:
            print()
            print("states in optimized but NOT in unoptimized (over-approximation):")
            for state in sorted(extra, key=str):
                print(f"  {dict(state)}")  # ty: ignore[no-matching-overload]
        if not missing and not extra:
            print()
            print("optimized and unoptimized agree.")

    opt_journal = _get_reachable_journal(call, skip_optimizations=False)
    unopt_journal = _get_reachable_journal(call, skip_optimizations=True)
    print_journal("optimized", opt_journal, full=args.full_journal)
    print_journal("unoptimized", unopt_journal, full=args.full_journal)

    candidates = [name for name, _method in elided_tags(opt_journal)]
    if isinstance(unoptimized, frozenset):
        diagnose_reachable_forced_keep(call, unoptimized, candidates, args.max_subset_size)

    if isinstance(optimized, frozenset) and isinstance(unoptimized, frozenset):
        return 2 if unoptimized - optimized else 0
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    args = parse_args()
    module = load_module(args.reproducer)
    prove_call, reachable_call, failure = capture_reproducer_call(module, args.test_name)

    print(f"reproducer: {args.reproducer}")

    mode = args.mode
    if mode == "auto":
        if reachable_call is not None:
            mode = "reachable"
        elif prove_call is not None:
            mode = "prove"
        else:
            print("ERROR: No prove() or reachable_states() call captured.")
            return 1

    if mode == "prove":
        if prove_call is None:
            print("ERROR: No prove() call captured from reproducer.")
            return 1
        return diagnose_prove(prove_call, failure, args)

    if mode == "reachable":
        if reachable_call is None:
            print("ERROR: No reachable_states() call captured from reproducer.")
            return 1
        return diagnose_reachable(reachable_call, failure, args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
