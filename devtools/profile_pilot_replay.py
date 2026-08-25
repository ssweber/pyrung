"""Measure PILOT's residual replay work without changing its executor.

The default drive is the exact Tumbler completion route that excludes
``Cmd_State_Complete``.  Instrumentation wraps existing replay/fold boundaries
and retains only aggregate counters and exact call intervals -- never a
per-scan log.

Run the checked benchmark and write its machine-readable artifact::

    uv run python -u devtools/profile_pilot_replay.py --write
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pyrung.core.runner as runner_module
from pyrung import PLC
from pyrung.core.analysis.pilot import causal as causal_module
from pyrung.core.analysis.pilot import coast as coast_module
from pyrung.core.analysis.pilot import cyclefold as cyclefold_module
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.runner import _compile_avoid

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from tests.tumbler.skeleton import dump_skeleton, extract_skeleton  # noqa: E402

DEFAULT_JSON = _ROOT / "scratchpad" / "pilot-docs" / "E1_RESIDUAL_REPLAY_DATA.json"
_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")


@dataclass
class TimeCount:
    calls: int = 0
    seconds: float = 0.0

    def add(self, started: float) -> None:
        self.calls += 1
        self.seconds += time.perf_counter() - started


@dataclass
class Partition:
    logical: int = 0
    ordinary_folded: int = 0
    cycle_folded: int = 0
    residual: int = 0

    def add(self, stats: Mapping[str, int]) -> None:
        logical = int(stats.get("logical_scans", 0))
        ordinary = int(stats.get("ordinary_folded_scans", 0))
        cycle = int(stats.get("cycle_folded_scans", 0))
        residual = int(stats.get("residual_scans", stats.get("kernel_scans", logical)))
        if ordinary + cycle + residual != logical:
            raise AssertionError(
                f"fold partition is not exhaustive: {ordinary} + {cycle} + {residual} != {logical}"
            )
        self.logical += logical
        self.ordinary_folded += ordinary
        self.cycle_folded += cycle
        self.residual += residual


@dataclass(frozen=True)
class CandidateCall:
    overlay_fingerprint: str
    start_scan: int
    end_scan: int
    candidates: tuple[str, ...]
    seconds: float

    @property
    def interval_scans(self) -> int:
        return max(0, self.end_scan - self.start_scan)

    @property
    def exact_key(self) -> tuple[Any, ...]:
        return (
            self.overlay_fingerprint,
            self.start_scan,
            self.end_scan,
            self.candidates,
        )


@dataclass(frozen=True)
class CoastCall:
    kind: str
    overlay_fingerprint: str
    start_scan: int
    end_scan: int
    logical: int
    ordinary_folded: int
    cycle_folded: int
    residual: int


@dataclass
class Profile:
    coast_partitions: dict[str, Partition] = field(default_factory=lambda: defaultdict(Partition))
    compiled_actual_scans: int = 0
    witness_scans: int = 0
    capture_requests: int = 0
    capture_hits: int = 0
    capture_misses: int = 0
    slab_refills: int = 0
    slab_materialized_states: int = 0
    candidate_calls: list[CandidateCall] = field(default_factory=list)
    candidate_owners: list[PLC] = field(default_factory=list, repr=False)
    coast_calls: list[CoastCall] = field(default_factory=list)
    coast_owners: list[PLC] = field(default_factory=list, repr=False)
    replay_time: TimeCount = field(default_factory=TimeCount)
    witness_time: TimeCount = field(default_factory=TimeCount)
    candidate_time: TimeCount = field(default_factory=TimeCount)
    compile_time: TimeCount = field(default_factory=TimeCount)
    warm_backend_time: TimeCount = field(default_factory=TimeCount)
    handoff_time: TimeCount = field(default_factory=TimeCount)
    feasibility_shadow: dict[str, Any] | None = None
    first_shadow_mismatch: dict[str, Any] | None = None
    shadow_seconds: float = 0.0


def _stable_repr(value: Any) -> str:
    return _ADDRESS.sub("0xADDR", repr(value))


def executable_overlay_fingerprint(plc: PLC) -> str:
    """Stable fingerprint of the recorded executable overlay.

    This identifies program, synthesis rungs, and runner options that were
    visible to the profiler. It is not proof that a historical interval belongs
    to one exact PILOT World; endpoint parity remains the qualification gate.
    """
    synthesis = plc._synthesis
    payload = {
        "program": _stable_repr(getattr(plc._program, "rungs", plc._logic)),
        "subroutines": _stable_repr(getattr(plc._program, "subroutines", {})),
        "plant": _stable_repr(getattr(synthesis, "plant", ())),
        "holds": _stable_repr(getattr(synthesis, "holds", ())),
        "dt": plc._dt,
        "time_mode": str(plc._time_mode),
        "record_all_tags": plc._record_all_tags,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def skeleton_digest(events: list[Any]) -> tuple[str, int]:
    skeleton = extract_skeleton(events)
    encoded = dump_skeleton(skeleton).encode()
    return hashlib.sha256(encoded).hexdigest(), len(skeleton)


@contextmanager
def observe(profile: Profile) -> Iterator[None]:
    """Install aggregate-only probes around current replay boundaries."""
    coast_stack: list[str] = []
    shadow_active = [False]
    original_seek = coast_module.CoastSession.seek
    original_cycle = cyclefold_module.cycle_fold_until
    original_compiled_step = CompiledPLC.step
    original_compiled_step_replay = CompiledPLC.step_replay
    original_capture = PLC._replay_capture_at
    original_slab = PLC._replay_slab_fill
    original_candidate = causal_module._program_written_changes
    original_kernel = PLC._compiled_replay_supported_kernel
    original_handoff = PLC._fork_from_reconstructed_state
    original_execute = runner_module.execute_program

    def seek(self: coast_module.CoastSession, *args: Any, **kwargs: Any) -> Any:
        coast_stack.append(self.kind)
        try:
            return original_seek(self, *args, **kwargs)
        finally:
            coast_stack.pop()

    def cycle(
        plc: PLC,
        predicate: Callable[[Any], bool],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        supplied = kwargs.get("stats")
        local_stats: dict[str, int] = supplied if isinstance(supplied, dict) else {}
        kwargs["stats"] = local_stats
        start_scan = plc.state.scan_id
        try:
            return original_cycle(plc, predicate, *args, **kwargs)
        finally:
            kind = coast_stack[-1] if coast_stack else "unscoped"
            profile.coast_partitions[kind].add(local_stats)
            call = CoastCall(
                kind=kind,
                overlay_fingerprint=executable_overlay_fingerprint(plc),
                start_scan=start_scan,
                end_scan=plc.state.scan_id,
                logical=int(local_stats.get("logical_scans", 0)),
                ordinary_folded=int(local_stats.get("ordinary_folded_scans", 0)),
                cycle_folded=int(local_stats.get("cycle_folded_scans", 0)),
                residual=int(
                    local_stats.get(
                        "residual_scans",
                        local_stats.get("kernel_scans", 0),
                    )
                ),
            )
            profile.coast_calls.append(call)
            profile.coast_owners.append(plc)
            if kind == "replay" and call.residual >= 20 and profile.feasibility_shadow is None:
                shadow_started = time.perf_counter()
                compiled_cache = plc._compiled_replay_kernel
                shadow_active[0] = True
                try:
                    shadow = _benchmark_exact_coast(plc, call, original_kernel)
                finally:
                    plc._compiled_replay_kernel = compiled_cache
                    shadow_active[0] = False
                    profile.shadow_seconds += time.perf_counter() - shadow_started
                if shadow is not None:
                    if shadow["endpoint_parity"]:
                        profile.feasibility_shadow = shadow
                    elif profile.first_shadow_mismatch is None:
                        profile.first_shadow_mismatch = shadow

    def compiled_step(self: CompiledPLC) -> Any:
        if shadow_active[0]:
            return original_compiled_step(self)
        started = time.perf_counter()
        try:
            return original_compiled_step(self)
        finally:
            profile.compiled_actual_scans += 1
            profile.warm_backend_time.add(started)

    def compiled_step_replay(self: CompiledPLC, *args: Any, **kwargs: Any) -> Any:
        if shadow_active[0]:
            return original_compiled_step_replay(self, *args, **kwargs)
        started = time.perf_counter()
        try:
            return original_compiled_step_replay(self, *args, **kwargs)
        finally:
            profile.compiled_actual_scans += 1
            profile.warm_backend_time.add(started)

    def capture(self: PLC, target_scan_id: int) -> Any:
        profile.capture_requests += 1
        hit = target_scan_id in self._cached_replay_captures
        profile.capture_hits += int(hit)
        profile.capture_misses += int(not hit)
        started = time.perf_counter()
        try:
            return original_capture(self, target_scan_id)
        finally:
            profile.replay_time.add(started)

    def slab(self: PLC, scan_id: int) -> Any:
        try:
            return original_slab(self, scan_id)
        finally:
            profile.slab_refills += 1
            profile.slab_materialized_states += int(
                self._last_replay_slab_stats.get("materialized_states", 0)
            )

    def candidate(
        self: PLC,
        start_scan: int,
        end_scan: int,
        relevant: frozenset[str],
    ) -> Any:
        started = time.perf_counter()
        try:
            return original_candidate(self, start_scan, end_scan, relevant)
        finally:
            elapsed = time.perf_counter() - started
            profile.candidate_time.calls += 1
            profile.candidate_time.seconds += elapsed
            profile.candidate_calls.append(
                CandidateCall(
                    overlay_fingerprint=executable_overlay_fingerprint(self),
                    start_scan=start_scan,
                    end_scan=end_scan,
                    candidates=tuple(sorted(relevant)),
                    seconds=elapsed,
                )
            )
            profile.candidate_owners.append(self)

    def kernel(self: PLC) -> Any:
        if shadow_active[0]:
            return original_kernel(self)
        cold = self._compiled_replay_kernel is None
        started = time.perf_counter()
        try:
            return original_kernel(self)
        finally:
            if cold:
                profile.compile_time.add(started)

    def handoff(self: PLC, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original_handoff(self, *args, **kwargs)
        finally:
            profile.handoff_time.add(started)

    def execute(*args: Any, **kwargs: Any) -> Any:
        observer = kwargs.get("observer")
        is_witness = type(observer).__name__ == "ConditionViewCapture"
        started = time.perf_counter()
        try:
            return original_execute(*args, **kwargs)
        finally:
            if is_witness:
                profile.witness_scans += 1
                profile.witness_time.add(started)

    coast_module.CoastSession.seek = seek
    cyclefold_module.cycle_fold_until = cycle  # ty: ignore[invalid-assignment]
    CompiledPLC.step = compiled_step
    CompiledPLC.step_replay = compiled_step_replay
    PLC._replay_capture_at = capture
    PLC._replay_slab_fill = slab
    causal_module._program_written_changes = candidate  # ty: ignore[invalid-assignment]
    PLC._compiled_replay_supported_kernel = kernel
    PLC._fork_from_reconstructed_state = handoff
    runner_module.execute_program = execute  # ty: ignore[invalid-assignment]
    try:
        yield
    finally:
        coast_module.CoastSession.seek = original_seek
        cyclefold_module.cycle_fold_until = original_cycle
        CompiledPLC.step = original_compiled_step
        CompiledPLC.step_replay = original_compiled_step_replay
        PLC._replay_capture_at = original_capture
        PLC._replay_slab_fill = original_slab
        causal_module._program_written_changes = original_candidate
        PLC._compiled_replay_supported_kernel = original_kernel
        PLC._fork_from_reconstructed_state = original_handoff
        runner_module.execute_program = original_execute


def _state_equal(left: Any, right: Any) -> bool:
    return (
        left.scan_id == right.scan_id
        and math.isclose(left.timestamp, right.timestamp, rel_tol=0.0, abs_tol=1e-9)
        and dict(left.tags) == dict(right.tags)
        and dict(left.memory) == dict(right.memory)
    )


def _state_difference(left: Any, right: Any) -> dict[str, Any]:
    left_tags = dict(left.tags)
    right_tags = dict(right.tags)
    left_memory = dict(left.memory)
    right_memory = dict(right.memory)
    tag_keys = left_tags.keys() | right_tags.keys()
    memory_keys = left_memory.keys() | right_memory.keys()
    return {
        "scan_ids": [left.scan_id, right.scan_id],
        "timestamp_delta": right.timestamp - left.timestamp,
        "tag_difference_count": sum(left_tags.get(key) != right_tags.get(key) for key in tag_keys),
        "tag_difference_sample": [
            [key, left_tags.get(key), right_tags.get(key)]
            for key in sorted(tag_keys)
            if left_tags.get(key) != right_tags.get(key)
        ][:8],
        "memory_difference_count": sum(
            left_memory.get(key) != right_memory.get(key) for key in memory_keys
        ),
        "memory_difference_sample": [
            [key, left_memory.get(key), right_memory.get(key)]
            for key in sorted(memory_keys)
            if left_memory.get(key) != right_memory.get(key)
        ][:8],
    }


def _benchmark_exact_coast(
    plc: PLC,
    call: CoastCall,
    kernel_reader: Callable[[PLC], Any],
) -> dict[str, Any] | None:
    """Benchmark one coast before its recorded executable overlay can change."""
    kernel = kernel_reader(plc)
    if kernel is None:
        return None
    try:
        interpreted_started = time.perf_counter()
        interpreted = plc._replay_range_interpreted(call.start_scan + 1, call.end_scan)
        interpreted_s = time.perf_counter() - interpreted_started
        compiled: list[Any] = []
        warm_times: list[float] = []
        for _ in range(3):
            started = time.perf_counter()
            compiled = plc._replay_range_compiled(call.start_scan + 1, call.end_scan, kernel)
            warm_times.append(time.perf_counter() - started)
    except Exception:
        return None
    if not interpreted or not compiled:
        return None
    warm_s = min(warm_times)
    parity = _state_equal(interpreted[-1], compiled[-1])
    result: dict[str, Any] = {
        "advancement_kind": "replay_coast",
        "overlay_fingerprint": call.overlay_fingerprint,
        "start_scan": call.start_scan,
        "end_scan": call.end_scan,
        "logical_scans": call.logical,
        "residual_scans": call.residual,
        "interpreted_seconds": interpreted_s,
        "warm_compiled_seconds": warm_s,
        "speedup": interpreted_s / warm_s if warm_s else None,
        "endpoint_parity": parity,
    }
    if not parity:
        result["difference"] = _state_difference(interpreted[-1], compiled[-1])
    return result


def benchmark_representative_interval(profile: Profile) -> dict[str, Any] | None:
    """Shadow one replay coast with the same recorded executable overlay."""
    if profile.feasibility_shadow is not None:
        return profile.feasibility_shadow
    if profile.first_shadow_mismatch is not None:
        return profile.first_shadow_mismatch
    ranked = sorted(
        (
            (call, owner)
            for call, owner in zip(profile.coast_calls, profile.coast_owners, strict=True)
            if call.kind == "replay"
        ),
        key=lambda item: item[0].residual,
        reverse=True,
    )
    first_mismatch: dict[str, Any] | None = profile.first_shadow_mismatch
    for call, plc in ranked:
        if call.end_scan <= call.start_scan:
            continue
        # Candidate runners are live mutable forks. A later correction may
        # replace their synthesis overlay after this call; benchmarking that
        # later overlay against the recorded interval would be false parity.
        if executable_overlay_fingerprint(plc) != call.overlay_fingerprint:
            continue
        kernel = plc._compiled_replay_supported_kernel()
        if kernel is None:
            continue  # unsupported executable overlay: fail closed
        try:
            interpreted_started = time.perf_counter()
            interpreted = plc._replay_range_interpreted(call.start_scan + 1, call.end_scan)
            interpreted_s = time.perf_counter() - interpreted_started

            # Kernel is already compiled: this is deliberately a warm backend
            # measurement. Repeat to damp one-off allocator noise.
            warm_times: list[float] = []
            compiled: list[Any] = []
            for _ in range(3):
                started = time.perf_counter()
                compiled = plc._replay_range_compiled(call.start_scan + 1, call.end_scan, kernel)
                warm_times.append(time.perf_counter() - started)
        except Exception:
            continue
        warm_s = min(warm_times)
        parity = bool(interpreted and compiled and _state_equal(interpreted[-1], compiled[-1]))
        if not parity:
            # Treat an unexplained mismatch as ineligible, never as evidence
            # for the modeled ceiling. Continue looking for the same recorded
            # overlay with endpoint parity.
            if interpreted and compiled and first_mismatch is None:
                first_mismatch = {
                    "advancement_kind": "replay_coast",
                    "overlay_fingerprint": call.overlay_fingerprint,
                    "start_scan": call.start_scan,
                    "end_scan": call.end_scan,
                    "logical_scans": call.logical,
                    "residual_scans": call.residual,
                    "interpreted_seconds": interpreted_s,
                    "warm_compiled_seconds": warm_s,
                    "speedup": interpreted_s / warm_s if warm_s else None,
                    "endpoint_parity": False,
                    "difference": _state_difference(interpreted[-1], compiled[-1]),
                }
            continue
        return {
            "advancement_kind": "replay_coast",
            "overlay_fingerprint": call.overlay_fingerprint,
            "start_scan": call.start_scan,
            "end_scan": call.end_scan,
            "logical_scans": call.logical,
            "residual_scans": call.residual,
            "interpreted_seconds": interpreted_s,
            "warm_compiled_seconds": warm_s,
            "speedup": interpreted_s / warm_s if warm_s else None,
            "endpoint_parity": parity,
        }
    return first_mismatch


def _timing(value: TimeCount) -> dict[str, Any]:
    return {"calls": value.calls, "seconds": value.seconds}


def _replay_advancement_key(
    overlay_fingerprint: str,
    start_scan: int,
    end_scan: int,
) -> tuple[str, str, int, int]:
    """Identity qualified by one replay-coast shadow."""
    return ("replay_coast", overlay_fingerprint, start_scan, end_scan)


def qualified_replay_residual_scans(
    calls: list[CoastCall],
    shadow: Mapping[str, Any] | None,
) -> int:
    """Residual scans covered by this exact passing shadow, and no others."""
    if (
        shadow is None
        or not shadow.get("endpoint_parity")
        or shadow.get("advancement_kind") != "replay_coast"
    ):
        return 0
    shadow_key = _replay_advancement_key(
        str(shadow.get("overlay_fingerprint", "")),
        int(shadow.get("start_scan", -1)),
        int(shadow.get("end_scan", -1)),
    )
    return sum(
        call.residual
        for call in calls
        if call.kind == "replay"
        and _replay_advancement_key(
            call.overlay_fingerprint,
            call.start_scan,
            call.end_scan,
        )
        == shadow_key
    )


def build_report(
    profile: Profile,
    *,
    events: list[Any],
    route_seconds: float,
    reached: bool,
    finish_scan: int | None,
    shadow: dict[str, Any] | None,
) -> dict[str, Any]:
    digest, skeleton_events = skeleton_digest(events)
    shapes = Counter(call.exact_key for call in profile.candidate_calls)
    unique_overlays = sorted({call.overlay_fingerprint for call in profile.candidate_calls})
    replay_partition = profile.coast_partitions.get("replay", Partition())
    backend_supported_interval_scans = 0
    backend_supported_replay_residual = 0
    unsupported_overlays: set[str] = set()
    checked: dict[int, bool] = {}
    for call, owner in zip(profile.candidate_calls, profile.candidate_owners, strict=True):
        owner_id = id(owner)
        if executable_overlay_fingerprint(owner) != call.overlay_fingerprint:
            unsupported_overlays.add(call.overlay_fingerprint)
            continue
        if owner_id not in checked:
            try:
                checked[owner_id] = owner._compiled_replay_supported_kernel() is not None
            except Exception:
                checked[owner_id] = False
        if checked[owner_id]:
            backend_supported_interval_scans += call.interval_scans
        else:
            unsupported_overlays.add(call.overlay_fingerprint)
    coast_checked: dict[int, bool] = {}
    for call, owner in zip(profile.coast_calls, profile.coast_owners, strict=True):
        if (
            call.kind != "replay"
            or executable_overlay_fingerprint(owner) != call.overlay_fingerprint
        ):
            continue
        owner_id = id(owner)
        if owner_id not in coast_checked:
            try:
                coast_checked[owner_id] = owner._compiled_replay_supported_kernel() is not None
            except Exception:
                coast_checked[owner_id] = False
        if coast_checked[owner_id]:
            backend_supported_replay_residual += call.residual
    # Candidate-history calls have no candidate-history shadow in E1, so even a
    # passing replay-coast shadow cannot qualify them. Replay qualification is
    # equally narrow: same recorded overlay fingerprint and exact interval.
    compiled_eligible_interval_scans = 0
    compiled_eligible_replay_residual = qualified_replay_residual_scans(
        profile.coast_calls,
        shadow,
    )

    modeled_savings = None
    if (
        shadow is not None
        and shadow["endpoint_parity"]
        and shadow["speedup"]
        and compiled_eligible_replay_residual > 0
    ):
        ratio = 1.0 / float(shadow["speedup"])
        modeled_savings = {
            "eligible_interpreted_residual_scans": compiled_eligible_replay_residual,
            "warm_kernel_time_fraction": ratio,
            "maximum_replay_residual_time_saved_fraction": max(0.0, 1.0 - ratio),
            "note": (
                "Ceiling only for replay coasts with this exact recorded "
                "executable-overlay fingerprint and advancement interval; "
                "excludes compile, handoff, witness, reasoning, and live-route "
                "costs."
            ),
        }

    return {
        "schema": 2,
        "route": {
            "target": "Sts_State_Completed=True",
            "avoid": "Cmd_State_Complete",
            "reached": reached,
            "finish_scan": finish_scan,
            "seconds": route_seconds,
            "timing_basis": "instrumented wall time excluding feasibility shadow",
            "skeleton_sha256": digest,
            "skeleton_events": skeleton_events,
        },
        "baseline": {
            "executor": "current interpreted PILOT with existing causal compiled replay",
            "coast_partitions": {
                name: asdict(partition)
                for name, partition in sorted(profile.coast_partitions.items())
            },
            "compiled_actual_scans": profile.compiled_actual_scans,
            "interpreted_replay_residual_scans": replay_partition.residual,
            "interpreted_witness_scans": profile.witness_scans,
            "capture": {
                "requests": profile.capture_requests,
                "hits": profile.capture_hits,
                "misses": profile.capture_misses,
            },
            "slabs": {
                "refills": profile.slab_refills,
                "materialized_states": profile.slab_materialized_states,
            },
            "candidate_replay": {
                "calls": len(profile.candidate_calls),
                "unique_exact_intervals": len(shapes),
                "repeated_exact_intervals": sum(count - 1 for count in shapes.values()),
                "recorded_executable_overlay_fingerprints": unique_overlays,
                "backend_supported_interval_scans": backend_supported_interval_scans,
                "backend_supported_replay_residual_scans": (backend_supported_replay_residual),
                "compiled_eligible_interval_scans": compiled_eligible_interval_scans,
                "compiled_eligible_replay_residual_scans": (compiled_eligible_replay_residual),
                "unsupported_overlay_fingerprints": sorted(unsupported_overlays),
                "calls_detail": [
                    {
                        "overlay_fingerprint": call.overlay_fingerprint,
                        "start_scan": call.start_scan,
                        "end_scan": call.end_scan,
                        "interval_scans": call.interval_scans,
                        "candidate_count": len(call.candidates),
                        "candidates_sha256": hashlib.sha256(
                            json.dumps(call.candidates).encode()
                        ).hexdigest(),
                        "seconds": call.seconds,
                    }
                    for call in profile.candidate_calls
                ],
            },
            "timing": {
                "boundaries_are_nested_and_non_additive": True,
                "route_is_instrumented_time_excluding_shadow": True,
                "cold_compile": _timing(profile.compile_time),
                "warm_backend": _timing(profile.warm_backend_time),
                "observation_handoff": _timing(profile.handoff_time),
                "interpreted_witness": _timing(profile.witness_time),
                "replay_envelope": _timing(profile.replay_time),
                "candidate_intervals": _timing(profile.candidate_time),
                "route_seconds": route_seconds,
            },
        },
        "feasibility_shadow": shadow,
        "modeled_ceiling": modeled_savings,
    }


def run_exact_route(*, max_scans: int, wall_seconds: float) -> dict[str, Any]:
    logic = importlib.import_module("tests.fixtures.tumbler").logic
    plc = PLC(logic, dt=0.010)
    plc.step()
    tags = plc._known_tags_by_name
    avoid = _compile_avoid(tags["Cmd_State_Complete"])
    profile = Profile()
    events: list[Any] = []
    reached = False
    finish_scan: int | None = None
    started = time.perf_counter()
    with observe(profile):
        for event in pilot_events(
            plc,
            tags["Sts_State_Completed"],
            max_scans=max_scans,
            avoid_pred=avoid,
        ):
            events.append(event)
            if event.kind == "finished":
                reached = bool(event.data.get("reached"))
                finish_scan = event.scan
                break
            if time.perf_counter() - started > wall_seconds:
                break
    route_seconds = time.perf_counter() - started - profile.shadow_seconds
    shadow = benchmark_representative_interval(profile)
    return build_report(
        profile,
        events=events,
        route_seconds=route_seconds,
        reached=reached,
        finish_scan=finish_scan,
        shadow=shadow,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-scans", type=int, default=40_000)
    parser.add_argument("--wall-seconds", type=float, default=300.0)
    parser.add_argument(
        "--write",
        nargs="?",
        const=str(DEFAULT_JSON),
        metavar="PATH",
        help="write JSON as well as printing it (default: checked report artifact)",
    )
    args = parser.parse_args()
    report = run_exact_route(max_scans=args.max_scans, wall_seconds=args.wall_seconds)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.write:
        path = Path(args.write)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
