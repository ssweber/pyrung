"""Profile where time goes in pilot_events() on the burner project."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

CLICK_PROJECT = Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\Sam\AppData\Local\Temp\CLICK (00010A00)\pyrung_project",
    )
)
sys.path.insert(0, str(CLICK_PROJECT))

from main import logic  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pilot.compass import (  # noqa: E402
    Compass,
    detect_opaque_loop,
    detect_opaque_pipelines,
)
from pyrung.core.analysis.pilot.physical import install_harness  # noqa: E402
from pyrung.core.analysis.pilot.pilot import (  # noqa: E402
    _build_pilot_context,
    _parse_target,
    _pilot_loop_events,
    _prepare_route,
)
from pyrung.core.analysis.pilot.trace import (  # noqa: E402
    compute_edge_tags,
    compute_reference_constants,
    compute_resting_values,
    compute_steerable,
)


class Timer:
    def __init__(self, label: str):
        self.label = label
        self.elapsed = 0.0
        self._start = None

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_):
        self.elapsed += time.perf_counter() - self._start


def main() -> int:
    plc = PLC(logic)
    plc.step()

    target = plc._known_tags_by_name["y_BurnerLoop"]
    max_scans = int(os.environ.get("PILOT_MAX_SCANS", "100000"))

    timers: dict[str, Timer] = {}

    def t(label: str) -> Timer:
        if label not in timers:
            timers[label] = Timer(label)
        return timers[label]

    # ── Setup phase (mirroring pilot_events lines 1593-1619) ──
    from pyrung.core.analysis.pdg import build_program_graph

    # Pass the Tag object for a Bool target (not the evaluated comparison)
    target_tag_parsed, target_value_parsed, target_predicate = _parse_target(target)
    program = plc._program

    with t("fork"):
        fork = plc.fork(history_budget=math.inf)

    with t("build_pdg"):
        pdg = build_program_graph(program)

    with t("install_harness"):
        harness_fb = install_harness(fork)

    with t("compute_reference_constants"):
        ref_consts = compute_reference_constants(pdg, program)

    with t("compute_steerable"):
        steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts

    with t("compute_edge_tags"):
        edge_tags = compute_edge_tags(pdg, program)

    with t("compute_resting_values"):
        resting = compute_resting_values(steerable, fork._known_tags_by_name, pdg, program)

    with t("_build_pilot_context"):
        nd_domains, key_config, evidence, _semantic = _build_pilot_context(
            program, dict(fork.state.tags)
        )

    with t("detect_opaque_pipelines"):
        opaque_slices = detect_opaque_pipelines(pdg, program, steerable)

    with t("Compass_init"):
        inf = Compass(opaque_slices)

    with t("detect_opaque_loop"):
        opaque_loop = detect_opaque_loop(pdg, program)

    with t("_prepare_route"):
        route_lock, blocked_route_actions, _route_taken = _prepare_route(
            fork,
            target_tag_parsed,
            target_value_parsed,
            pdg,
            program,
            steerable,
            opaque_loop,
        )

    setup_total = sum(ti.elapsed for ti in timers.values())
    print(f"\n{'=' * 60}")
    print(f"SETUP PHASE  total={setup_total:.3f}s")
    print(f"{'=' * 60}")
    for label, ti in timers.items():
        pct = 100 * ti.elapsed / setup_total if setup_total else 0
        print(f"  {label:35s} {ti.elapsed:8.3f}s  ({pct:5.1f}%)")

    # ── Loop phase ──
    print(f"\n{'=' * 60}")
    print("LOOP PHASE")
    print(f"{'=' * 60}")

    loop_start = time.perf_counter()
    event_counts: dict[str, int] = {}
    event_times: dict[str, float] = {}
    iteration_count = 0
    last_event_end = loop_start
    zoom_scans = 0
    for event in _pilot_loop_events(
        fork,
        target_tag_parsed,
        target_value_parsed,
        pdg,
        program,
        steerable,
        edge_tags,
        resting,
        nd_domains=nd_domains,
        evidence=evidence,
        key_config=key_config,
        influence=inf,
        opaque_loop=opaque_loop,
        route=route_lock,
        blocked_route_actions=blocked_route_actions,
        max_scans=max_scans,
        live=False,
        debug=False,
        target_predicate=target_predicate,
    ):
        now = time.perf_counter()
        dt = now - last_event_end
        kind = event.kind
        event_counts[kind] = event_counts.get(kind, 0) + 1
        event_times[kind] = event_times.get(kind, 0.0) + dt
        last_event_end = now

        if kind == "iteration":
            iteration_count += 1

        if kind == "zoom_accepted":
            d = event.data
            scan_before = d.get("scan_before", 0)
            scan_after = d.get("scan_after", 0)
            if scan_before and scan_after:
                zoom_scans += scan_after - scan_before

        if kind == "finished":
            d = event.data
            print(f"  reached: {d['reached']}")
            print(f"  reason: {d['reason']}")
            print(f"  steps: {len(d['steps'])}")
            print(f"  final scan: {event.scan}")
            break

    loop_total = time.perf_counter() - loop_start

    print(f"\n  iterations: {iteration_count}")
    print(f"  zoom_scans (coast): {zoom_scans}")
    print(f"  loop wall-clock: {loop_total:.3f}s")

    print("\n  Time by event kind (wall-clock between yields):")
    for kind, elapsed in sorted(event_times.items(), key=lambda x: -x[1]):
        count = event_counts[kind]
        pct = 100 * elapsed / loop_total if loop_total else 0
        print(f"    {kind:30s} {elapsed:8.3f}s  ({pct:5.1f}%)  x{count}")

    # ── Grand total ──
    grand = setup_total + loop_total
    print(f"\n{'=' * 60}")
    print(f"GRAND TOTAL: {grand:.3f}s  (setup={setup_total:.3f}s  loop={loop_total:.3f}s)")
    print(f"{'=' * 60}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
