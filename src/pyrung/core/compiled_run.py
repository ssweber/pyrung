"""Long ``run_for()`` / ``run_until()`` runs through the compiled kernel.

Nothing is skipped: every scan executes, so a run cannot land anywhere stepping
would not.  A run starts interpreted and moves to the kernel only once it has
run long enough for that to pay.  It stays interpreted when something has to
watch each scan (a ``when()``, a monitor, a callable stop predicate), and when
the program has no kernel.

Compiled scans record no causal timelines.  Their range goes in the runner's
fold windows, as the fold's macro steps do.

Between real scans, the guarded loop of :mod:`pyrung.core.scan_trace` runs the
scans that repeat the one before.  Its scans end in the same kernel state a real
scan would leave, so everything here treats them alike: a compiled stop predicate
and the bounds watch are guards inside it, which fail before the scan that would
matter commits.
"""

from __future__ import annotations

from collections.abc import Callable
from operator import itemgetter
from typing import TYPE_CHECKING, Any

from pyrung.core.bounds import TagConstraint, check_bounds
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.scan_trace import ScanLoop
from pyrung.core.state import SystemState
from pyrung.core.time_mode import TimeMode

if TYPE_CHECKING:
    from pyrung.core.kernel import CompiledKernel
    from pyrung.core.runner import PLC

# Scans a run takes interpreted before it moves to the kernel.  Short runs never
# compile and keep their causal records; tests/fold_check.py lowers this to send
# every run through the kernel.
INTERPRETED_PREFIX = 1000

# Test mode: each is called with the runner and the materialized state after each
# firing of the guarded loop, so the scans between exits can be compared with stepping.
LOOP_OBSERVERS: list[Callable[[PLC, SystemState], None]] = []


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
    from pyrung.core.fold import _replace_tip_state

    start = plc._state
    first_scan_id = start.scan_id + 1
    replay = CompiledPLC(
        plc._soft_exec_program(),
        initial_state=start,
        dt=plc._dt,
        compiled=kernel,
        exact_anchor=True,
    )
    replay._set_rtc_internal(plc._system_runtime._rtc_now(start), start.timestamp)
    replay._input_overrides._forces.update(plc._input_overrides.forces)

    interval = plc._checkpoint_interval
    bounds = _BoundsWatch(replay, plc._constrained_tags) if plc._constrained_tags else None
    if bounds is not None and bounds.violated():
        # A value already out of range: a scan rewriting it warns, unseen here.
        return budget
    settled = start  # the last state known exact, and where a failed run resumes
    used = 0
    undone = False
    loop = ScanLoop(replay, kernel, budget, predicate, plc._constrained_tags)
    plc._epoch_caches.on_tip_advanced()
    while used < budget:
        # Stop the loop at each checkpoint: it has to be installed on its scan.
        to_checkpoint = interval - replay._kernel.scan_id % interval
        ran = loop.advance(min(budget - used, to_checkpoint))
        if ran:
            used += ran
            checkpoint = replay._kernel.scan_id % interval == 0
            if checkpoint or LOOP_OBSERVERS:
                settled = replay._materialize_replay_state()
                if checkpoint:
                    _install(plc, settled, _replace_tip_state)
                for observer in LOOP_OBSERVERS:
                    observer(plc, settled)
            continue
        try:
            replay.step_replay(defer_flush=True)
        except Exception:
            loop.real_scan_failed("raised")
            undone = True
            break
        if bounds is not None and bounds.moved() and bounds.violated():
            loop.real_scan_failed("left bounds")
            undone = True
            break
        loop.real_scan_ran()
        used += 1
        scan_id = replay._kernel.scan_id
        checkpoint = scan_id % interval == 0
        if predicate is None and not checkpoint:
            continue
        state = replay._materialize_replay_state()
        settled = state
        if checkpoint:
            _install(plc, state, _replace_tip_state)
        if predicate is not None and predicate(state):
            loop.stop_held()
            break
    loop.finish()
    if undone:
        used = settled.scan_id - start.scan_id
    elif settled.scan_id != replay._kernel.scan_id:
        settled = replay._materialize_replay_state()
    if settled.scan_id != plc._state.scan_id:
        _install(plc, settled, _replace_tip_state)
    if settled.scan_id >= first_scan_id:
        plc._record_fold_landing(first_scan_id)
        plc._bounds_violations = {}
    return budget - used if undone else 0


class _BoundsWatch:
    """Notice a constrained tag changing, at the cost of a few C calls a scan.

    The kernel does not report writes, so an in-range rewrite goes unseen.  It
    cannot warn, and the watch starts from values known to be in range.
    """

    def __init__(self, replay: CompiledPLC, constrained: dict[str, TagConstraint]) -> None:
        kernel = replay._kernel
        self._constrained = constrained
        scalars = [n for n in constrained if n in kernel.tags and n not in replay._block_pos]
        cells: dict[str, list[tuple[str, int]]] = {}
        for name in constrained:
            position = replay._block_pos.get(name)
            if position is not None:
                cells.setdefault(position[0], []).append((name, position[1]))
        # (names, itemgetter, source): the getter always yields a tuple.
        self._groups: list[tuple[tuple[str, ...], Callable[[Any], Any], Any]] = []
        if scalars:
            self._groups.append((tuple(scalars), itemgetter(*scalars, scalars[0]), kernel.tags))
        for symbol, entries in cells.items():
            indices = [index for _name, index in entries]
            self._groups.append(
                (
                    tuple(name for name, _index in entries),
                    itemgetter(*indices, indices[0]),
                    kernel.blocks[symbol],
                )
            )
        self._seen = [get(source) for _names, get, source in self._groups]

    def moved(self) -> bool:
        now = [get(source) for _names, get, source in self._groups]
        if now == self._seen:
            return False
        self._seen = now
        return True

    def violated(self) -> bool:
        values = {
            name: value
            for (names, _get, _source), seen in zip(self._groups, self._seen, strict=True)
            for name, value in zip(names, seen, strict=False)
        }
        return bool(check_bounds(values, self._constrained))


def _install(plc: PLC, state: SystemState, replace_tip: Callable) -> None:
    """Make *state* the tip: cached, checkpointed on its interval, and history trimmed."""
    previous_tip = plc._state.scan_id
    replace_tip(plc, state)
    plc._record_fold_landing()
    plc._apply_history_retention(state.scan_id)
    if plc._playhead == previous_tip:
        plc._playhead = state.scan_id
