"""Long ``run_for()`` / ``run_until()`` runs through the compiled kernel.

Nothing is skipped: every scan executes, so a run cannot land anywhere stepping
would not.  A run starts interpreted and moves to the kernel only once it has
run long enough for that to pay.  It stays interpreted when something has to
watch each scan (a ``when()``, a monitor, a callable stop predicate), and when
the program has no kernel.

Compiled scans record no causal timelines.  Their range goes in the runner's
fold windows, as the fold's macro steps do.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from pyrung.core.state import SystemState
from pyrung.core.time_mode import TimeMode

if TYPE_CHECKING:
    from pyrung.core.kernel import CompiledKernel
    from pyrung.core.runner import PLC

# Scans a run takes interpreted before it moves to the kernel.  Short runs never
# compile and keep their causal records; tests/fold_check.py lowers this to send
# every run through the kernel.
INTERPRETED_PREFIX = 1000


def _someone_watches_each_scan(plc: PLC) -> bool:
    for registration in plc._breakpoints_by_id.values():
        if registration.enabled and not registration.removed:
            return True
    for registration in plc._monitors_by_id.values():
        if registration.enabled and not registration.removed:
            return True
    return False


def _step(
    plc: PLC,
    scans: int,
    predicate: Callable[[SystemState], bool] | None,
) -> tuple[int, bool]:
    """Step up to *scans* interpreted scans; return (scans run, stop asked)."""
    for used in range(1, scans + 1):
        plc._consume_pause_request()
        plc._run_single_scan(consume_pause_request=False)
        pause_requested = plc._consume_pause_request()
        hit = predicate is not None and bool(predicate(plc._state))
        if hit or pause_requested:
            return used, True
    return scans, False


def run_scans(
    plc: PLC,
    scans: int,
    predicate: Callable[[SystemState], bool] | None = None,
    *,
    compiled_predicate: bool = True,
) -> SystemState:
    """Run *scans* scans, or until *predicate* holds on a committed scan.

    *compiled_predicate* says *predicate* depends only on the state it is
    handed.  A callable cannot promise that, so it keeps the run interpreted.
    """
    prefix = max(1, INTERPRETED_PREFIX)
    used, stopped = _step(plc, min(scans, prefix), predicate)
    if stopped or used >= scans:
        return plc._state
    if (
        (predicate is not None and not compiled_predicate)
        or plc._replay_mode
        or plc._program is None
        or plc._time_mode != TimeMode.FIXED_STEP
        or _someone_watches_each_scan(plc)
    ):
        _step(plc, scans - used, predicate)
        return plc._state
    kernel = plc._compiled_replay_supported_kernel()
    if kernel is None:
        _step(plc, scans - used, predicate)
        return plc._state
    remaining = _run_compiled(plc, kernel, scans - used, predicate)
    if remaining:
        _step(plc, remaining, predicate)
    return plc._state


def _run_compiled(
    plc: PLC,
    kernel: CompiledKernel,
    budget: int,
    predicate: Callable[[SystemState], bool] | None,
) -> int:
    """Run up to *budget* kernel scans; return the scans left to step interpreted.

    A scan that raises, or that may have written a value outside a tag's bounds,
    is undone: the tip stays at the last state materialized and the scans from
    there on step interpreted, which raises or warns exactly as stepping does.
    """
    from pyrung.core.compiled_plc import CompiledPLC
    from pyrung.core.fold import _replace_tip_state

    start = plc._state
    first_scan_id = start.scan_id + 1
    replay = CompiledPLC(
        plc._soft_exec_program(),
        initial_state=start,
        dt=plc._dt,
        compiled=kernel,
    )
    replay._set_rtc_internal(plc._system_runtime._rtc_now(start), start.timestamp)
    replay._input_overrides._forces.update(plc._input_overrides.forces)

    interval = plc._checkpoint_interval
    constrained = plc._constrained_tags
    kernel_tags = replay._kernel.tags
    settled = start  # the last state known exact, and where a failed run resumes
    used = 0
    undone = False
    plc._epoch_caches.on_tip_advanced()
    while used < budget:
        try:
            replay.step_replay(defer_flush=not constrained)
        except Exception:
            undone = True
            break
        if constrained and _may_violate_bounds(kernel_tags, constrained):
            undone = True
            break
        used += 1
        scan_id = replay._kernel.scan_id
        checkpoint = scan_id % interval == 0
        if predicate is None and not checkpoint:
            continue
        state = replay._materialize_replay_state()
        settled = state
        if checkpoint:
            _install(plc, state, first_scan_id, _replace_tip_state)
            first_scan_id = scan_id + 1
        if predicate is not None and predicate(state):
            break
    if undone:
        used = settled.scan_id - start.scan_id
    elif settled.scan_id != replay._kernel.scan_id:
        settled = replay._materialize_replay_state()
    if settled.scan_id >= first_scan_id:
        _install(plc, settled, first_scan_id, _replace_tip_state)
    return budget - used if undone else 0


def _may_violate_bounds(tags: dict, constrained: dict) -> bool:
    from pyrung.core.bounds import check_bounds

    return bool(check_bounds({n: tags[n] for n in constrained if n in tags}, constrained))


def _install(plc: PLC, state: SystemState, first_scan_id: int, replace_tip: Callable) -> None:
    """Make *state* the tip, standing for the scans from *first_scan_id* on."""
    previous_tip = plc._state.scan_id
    replace_tip(plc, state)
    plc._record_fold_landing(first_scan_id)
    if plc._playhead == previous_tip:
        plc._playhead = state.scan_id
