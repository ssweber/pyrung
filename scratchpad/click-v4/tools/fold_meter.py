"""Fold effectiveness meter: monkeypatch instrumentation usable from scripts and pytest.

Counts kernel scans (interpreted + compiled replay), ordinary fold plans taken /
declined (plateau vs bound), skipped logical scans, cycle folds, the tags whose
change broke a plateau, and the frozen-write sets of every built fold context.

pytest:  -p fold_meter  (FOLD_METER_OUT=<json path>)
script:  import fold_meter; fold_meter.install(); ...; fold_meter.snapshot()
"""

from __future__ import annotations

import collections
import json
import os
import time

M: dict = {}


def reset() -> None:
    M.clear()
    M.update(
        interp_scans=0,
        replay_steps=0,
        plans=0,
        taken=0,
        declined_plateau=0,
        declined_bound=0,
        skipped=0,  # logical scans covered beyond the landing scan, ordinary fold
        cycle_folds=0,
        cycle_skipped=0,
        culprits=collections.Counter(),
        cycle_blockers=collections.Counter(),
        frozen=set(),
        contexts=0,
        t0=time.perf_counter(),
    )


def install() -> None:
    reset()
    from pyrung.core import fold as F
    from pyrung.core.compiled_plc import CompiledPLC
    from pyrung.core.runner import PLC

    orig_scan = PLC._run_single_scan

    def run_single_scan(self, *a, **k):
        M["interp_scans"] += 1
        return orig_scan(self, *a, **k)

    PLC._run_single_scan = run_single_scan

    orig_replay = CompiledPLC.step_replay

    def step_replay(self, *a, **k):
        M["replay_steps"] += 1
        n = k.get("logical_scans", 1)
        return orig_replay(self, *a, **k)

    CompiledPLC.step_replay = step_replay

    orig_plan = F._OrdinaryFoldStrategy.plan

    def plan(self, state, probe, **k):
        M["plans"] += 1
        out = orig_plan(self, state, probe, **k)
        if out is not None:
            M["taken"] += 1
            M["skipped"] += out.skip - 1
            return out
        if F._visible_items_match(state, probe.visible, self.exclude):
            M["declined_bound"] += 1
        else:
            M["declined_plateau"] += 1
            now = F._visible_items(state, self.exclude)
            keys = set(now) | set(probe.visible)
            for key in keys:
                if now.get(key, "<missing>") != probe.visible.get(key, "<missing>"):
                    M["culprits"][key] += 1
        return out

    F._OrdinaryFoldStrategy.plan = plan

    orig_build = F._build_fold_context

    def build(*a, **k):
        ctx = orig_build(*a, **k)
        M["contexts"] += 1
        M["frozen"] |= set(ctx.frozen_writes)
        return ctx

    F._build_fold_context = build

    try:
        from pyrung.core.analysis.pilot import cyclefold as CF
    except Exception:  # pragma: no cover
        return
    orig_cf = CF.cycle_fold_until

    def cycle_fold_until(*a, **k):
        stats = k.get("stats")
        if stats is None:
            stats = {}
            k["stats"] = stats
        out = orig_cf(*a, **k)
        M["cycle_folds"] += stats.get("cycle_folds", 0)
        M["cycle_skipped"] += stats.get("cycle_folded_scans", 0)
        return out

    CF.cycle_fold_until = cycle_fold_until

    orig_detect = CF.detect_cycle

    def detect_cycle(ring, **k):
        out = orig_detect(ring, **k)
        if out is not None or os.environ.get("FOLD_METER_CYCLE") is None:
            return out
        sig = k.get("significant_keys") or frozenset()
        mono = k.get("monotone_allowed") or frozenset()
        base = k.get("period_multiple_of", 1) or 1
        maxp = k.get("max_period", 0)
        best = None
        p = base
        while p <= maxp and p < len(ring):
            a, b = ring[-1], ring[-1 - p]
            diff = [key for key in sig if key not in mono and a.get(key) != b.get(key)]
            if best is None or len(diff) < len(best[1]):
                best = (p, diff)
            p += base
        if best is not None and len(best[1]) <= 6:
            for key in best[1]:
                M["cycle_blockers"][key] += 1
        return out

    CF.detect_cycle = detect_cycle


def snapshot() -> dict:
    kernel = M["interp_scans"] + M["replay_steps"]
    # landing scans are counted in kernel; skipped are the extra logical scans
    logical = kernel + M["skipped"] + M["cycle_skipped"]
    return {
        "kernel_scans": kernel,
        "interp_scans": M["interp_scans"],
        "replay_steps": M["replay_steps"],
        "logical_scans": logical,
        "plans": M["plans"],
        "folds_taken": M["taken"],
        "declined_plateau": M["declined_plateau"],
        "declined_bound": M["declined_bound"],
        "ordinary_skipped": M["skipped"],
        "cycle_folds": M["cycle_folds"],
        "cycle_skipped": M["cycle_skipped"],
        "wall_s": round(time.perf_counter() - M["t0"], 2),
        "contexts": M["contexts"],
        "frozen": sorted(M["frozen"]),
        "top_culprits": M["culprits"].most_common(40),
        "cycle_blockers": M["cycle_blockers"].most_common(30),
    }


# -- pytest plugin hooks ---------------------------------------------------


def pytest_configure(config):  # noqa: ARG001
    install()


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    out = os.environ.get("FOLD_METER_OUT")
    snap = snapshot()
    snap["exitstatus"] = int(exitstatus)
    if out:
        with open(out, "w") as f:
            json.dump(snap, f, indent=1, default=str)
