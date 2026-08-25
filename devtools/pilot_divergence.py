"""Stop a PILOT golden drive at its first decision-skeleton difference.

This is a refactoring aid, not part of PILOT.  It runs the same cold-boot
event stream used by the Tumbler golden tests and compares each newly emitted
decision event with an existing golden file.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Executing ``python devtools/pilot_divergence.py`` puts ``devtools`` rather
# than the repository root on sys.path.  The Tumbler serializer intentionally
# lives with its golden tests, so make that existing test support importable.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pilot import pilot_events  # noqa: E402
from tests.tumbler.skeleton import extract_skeleton, first_divergence  # noqa: E402


@dataclass(frozen=True)
class TargetSpec:
    """One tag target, optionally with an explicit equality value."""

    tag_name: str
    value: Any = True
    explicit_value: bool = False


@dataclass(frozen=True)
class StreamComparison:
    """The observed prefix and its first difference, if any."""

    actual: list[dict[str, Any]]
    divergence: tuple[int, Any, Any] | None
    elapsed_s: float
    last_scan: int | None


def parse_target(text: str) -> TargetSpec:
    """Parse ``Tag`` as Boolean true or ``Tag=JSON`` as an equality."""
    tag_name, separator, raw_value = text.partition("=")
    tag_name = tag_name.strip()
    if not tag_name:
        raise ValueError("target tag name is empty")
    if not separator:
        return TargetSpec(tag_name)
    if not raw_value.strip():
        raise ValueError("target value is empty")
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"target value must be JSON (for example {tag_name}=6 or {tag_name}=true)"
        ) from exc
    return TargetSpec(tag_name, value, explicit_value=True)


def compare_event_stream(
    events,
    golden: list[dict[str, Any]],
    *,
    wall_budget_s: float | None = None,
) -> StreamComparison:
    """Consume events only until their skeleton first differs from *golden*."""
    started = time.monotonic()
    raw_events = []
    actual: list[dict[str, Any]] = []
    last_scan: int | None = None

    for event in events:
        raw_events.append(event)
        last_scan = getattr(event, "scan", None)
        actual = extract_skeleton(raw_events)

        # Compare equal-length prefixes while the stream is still running.
        # If the actual stream has already outgrown the golden, first_divergence
        # supplies the missing expected entry.
        expected_prefix = golden[: len(actual)]
        divergence = first_divergence(expected_prefix, actual)
        if divergence is not None:
            return StreamComparison(actual, divergence, time.monotonic() - started, last_scan)

        if event.kind == "finished":
            return StreamComparison(
                actual,
                first_divergence(golden, actual),
                time.monotonic() - started,
                last_scan,
            )

        if wall_budget_s is not None and time.monotonic() - started > wall_budget_s:
            raise TimeoutError(
                f"drive exceeded the {wall_budget_s:g}s wall budget "
                f"at event {len(actual) - 1}, scan {last_scan}"
            )

    return StreamComparison(
        actual,
        first_divergence(golden, actual),
        time.monotonic() - started,
        last_scan,
    )


def _compact_json(value: Any, limit: int = 800) -> str:
    rendered = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def format_comparison(
    comparison: StreamComparison,
    *,
    context: int,
) -> str:
    """Render the first difference with a short matching lead-in."""
    if comparison.divergence is None:
        return (
            f"decision skeleton matches ({len(comparison.actual)} events, "
            f"{comparison.elapsed_s:.2f}s)"
        )

    index, expected, actual = comparison.divergence
    lines = [
        f"decision skeleton diverged at event {index} "
        f"after {comparison.elapsed_s:.2f}s"
        + (f" (raw scan {comparison.last_scan})" if comparison.last_scan is not None else "")
    ]
    start = max(0, index - context)
    if start < index:
        lines.append("previous matching events:")
        for prior_index in range(start, index):
            prior = comparison.actual[prior_index]
            lines.append(f"  [{prior_index}] {_compact_json(prior)}")
    lines.extend(
        (
            f"--- expected[{index}] ---",
            json.dumps(expected, indent=1, sort_keys=True, default=str, ensure_ascii=False),
            f"--- actual[{index}] ---",
            json.dumps(actual, indent=1, sort_keys=True, default=str, ensure_ascii=False),
        )
    )
    return "\n".join(lines)


def _load_golden(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"golden file does not exist: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"golden file is not valid JSON: {path}: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(entry, dict) for entry in value):
        raise ValueError(f"golden file must contain a JSON list of objects: {path}")
    return value


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run PILOT only until its decision skeleton differs from a golden."
    )
    parser.add_argument(
        "--fixture",
        default="tests.fixtures.tumbler",
        help="module containing the program as `logic` (default: tests.fixtures.tumbler)",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Boolean tag name, or equality written as Tag=JSON",
    )
    parser.add_argument("--golden", required=True, type=Path)
    parser.add_argument("--max-scans", type=int, default=400_000)
    parser.add_argument("--wall-budget", type=float, default=240.0)
    parser.add_argument("--dt", type=float, default=0.010)
    parser.add_argument(
        "--context",
        type=int,
        default=2,
        help="number of preceding matching events to print (default: 2)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        target_spec = parse_target(args.target)
        golden = _load_golden(args.golden)
        fixture = importlib.import_module(args.fixture)
        logic = fixture.logic

        plc = PLC(logic, dt=args.dt)
        plc.step()
        try:
            target_tag = plc._known_tags_by_name[target_spec.tag_name]
        except KeyError:
            raise ValueError(
                f"fixture {args.fixture!r} has no tag named {target_spec.tag_name!r}"
            ) from None
        target = target_tag == target_spec.value if target_spec.explicit_value else target_tag

        print(
            f"comparing how({args.target}) with {args.golden} (fixture {args.fixture})",
            flush=True,
        )
        comparison = compare_event_stream(
            pilot_events(plc, target, max_scans=args.max_scans),
            golden,
            wall_budget_s=args.wall_budget,
        )
    except (AttributeError, ImportError, TimeoutError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(format_comparison(comparison, context=max(0, args.context)))
    return 1 if comparison.divergence is not None else 0


if __name__ == "__main__":
    sys.exit(main())
