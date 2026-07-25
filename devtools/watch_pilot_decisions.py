"""Stream PILOT's live decisions with the same prose as DAP ``how``.

This is intentionally a tap, not another planner. It consumes ``pilot_events``
and delegates user-facing prose to DAP's existing progress formatter, adding
one compact decision line from the structured iteration/candidate receipts.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.runner import _compile_avoid
from pyrung.dap.console import _PilotProgressFormatter
from tests.fixtures.tumbler import logic


def _value(text: str) -> Any:
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text)
    except ValueError:
        return text


def _pair(text: str) -> tuple[str, Any]:
    tag, separator, value = text.partition("=")
    if not separator or not tag:
        raise argparse.ArgumentTypeError("expected TAG=VALUE")
    return tag, _value(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=_pair, default=("Sts_State_Completed", True))
    parser.add_argument("--avoid", default="Cmd_State_Complete")
    parser.add_argument("--max-scans", type=int, default=1_000_000)
    parser.add_argument(
        "--stop-action",
        type=_pair,
        default=("Cmd_Reset2FactoryDefault", True),
        help="stop as soon as this action appears in candidate construction",
    )
    args = parser.parse_args()

    plc = PLC(logic)
    plc.step()
    tags = plc._known_tags_by_name
    target_tag, target_value = args.target
    formatter = _PilotProgressFormatter()
    last_snapshot: dict[str, Any] = {}

    condition = tags[target_tag] == target_value
    avoid_pred = _compile_avoid(tags[args.avoid]) if args.avoid else None
    for event in pilot_events(
        plc,
        condition,
        max_scans=args.max_scans,
        avoid_pred=avoid_pred,
    ):
        rendered = formatter.format(event)
        if rendered:
            print(rendered, end="", flush=True)

        if event.kind == "iteration":
            last_snapshot = dict(event.data["snapshot"])
            continue
        if event.kind != "candidates_built":
            continue

        candidates = tuple(
            (candidate["tag"], candidate["value"]) for candidate in event.data["candidates"]
        )
        trace = tuple(event.data["trace_actions"])
        route = tuple(event.data["route_candidates"])
        prerequisites = tuple(
            (rung.dest, rung.value) for rung in event.data.get("prerequisite_rungs", ())
        )
        print(
            "[decision] "
            f"scan={event.scan} "
            f"state={last_snapshot.get('Sts_StateCurrent')!r} "
            f"step={last_snapshot.get('Internal__Step')!r} "
            f"candidates={candidates!r} trace={trace!r} route={route!r} "
            f"holds={prerequisites!r}",
            flush=True,
        )
        if (
            args.stop_action in candidates
            or args.stop_action in trace
            or args.stop_action in prerequisites
        ):
            for detail in event.data.get("trace_action_details", ()):
                if detail.pair != args.stop_action:
                    continue
                print(
                    "[receipt] "
                    f"provenance={detail.provenance!r} "
                    f"writer_path={detail.writer_path!r} "
                    f"operation_boundary={detail.operation_boundary!r} "
                    f"until={detail.until!r}",
                    flush=True,
                )
            print(f"[stop] candidate construction surfaced {args.stop_action!r}", flush=True)
            break


if __name__ == "__main__":
    main()
