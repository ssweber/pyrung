"""Fold differential check: every folded run against a stepped twin.

Enabled by ``--fold-check`` (``make test-fold``).  The three fold entry points
(``fold_run_until``, ``fold_run_for``, PILOT's ``cycle_fold_until``) are wrapped.
Before a folded run starts, a twin is built from the same tip state, forces,
pending patches and synthesis overlay; after the run, the twin steps the same
number of scans one by one, and every state the folded run itself produced
(executed scans and landings, as cached) must equal the twin's state at that
scan: tags and memory exactly, the timestamp to within a microsecond.  A mismatch raises :class:`FoldMismatch` inside the
test that ran the fold, naming the scan and the differing keys.

A run is skipped, and counted, when the twin cannot reproduce it: an active
``when()`` registration or a monitor other than a Harness feedback notifier
(their callbacks act on the folded runner only), replay mode, a time mode other
than fixed-step, or a run longer
than ``FOLD_CHECK_MAX_SCANS`` scans (default 200000).
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable
from typing import Any

MAX_SCANS = int(os.environ.get("FOLD_CHECK_MAX_SCANS", "200000"))
# Per-scan diagnostics a landing legitimately inflates (see fold._do_fold).
_IGNORED_TAGS = frozenset({"sys.scan_time_current_ms"})

STATS: Counter[str] = Counter()
_depth = 0


class FoldMismatch(AssertionError):
    pass


def _skip_reason(plc: Any) -> str | None:
    if getattr(plc, "_replay_mode", False):
        return "replay mode"
    if getattr(getattr(plc, "_time_mode", None), "name", "") != "FIXED_STEP":
        return "not fixed-step"
    for reg in getattr(plc, "_breakpoints_by_id", {}).values():
        if reg.enabled and not reg.removed:
            return "when() registration"
    for reg in getattr(plc, "_monitors_by_id", {}).values():
        if reg.enabled and not reg.removed:
            name = getattr(getattr(reg, "callback", None), "__qualname__", "")
            if "_make_fb_callback" not in name:
                return "monitor"
    return None


def _twin(plc: Any) -> Any:
    state = plc._state
    twin = plc._fork_from_reconstructed_state(
        state,
        rtc_at_state=plc._rtc_at_sim_time(state.timestamp),
        forces=dict(plc._forces),
        replay_mode=False,
    )
    twin._set_time_mode(plc._time_mode, dt=plc._dt)
    twin._input_overrides._pending_patches.update(plc._pending_patches)
    return twin


def _image(state: Any) -> dict[str, Any]:
    out = {k: v for k, v in state.tags.items() if k not in _IGNORED_TAGS}
    out.update({f"mem:{k}": v for k, v in state.memory.items()})
    return out


# Stepping adds dt once per scan and drifts in floating point (4508.20000002
# after 450820 scans of 0.01); a fold multiplies.  Clock phases are snapped to
# the scan grid, so drift this small reads alike.  Tags and memory stay exact.
_TIMESTAMP_TOLERANCE = 1e-6


def _compare(
    kind: str, scan: int, folded: Any, stepped: Any, last_equal: int | None = None
) -> None:
    a, b = _image(folded), _image(stepped)
    if abs(folded.timestamp - stepped.timestamp) > _TIMESTAMP_TOLERANCE:
        a[":timestamp"], b[":timestamp"] = folded.timestamp, stepped.timestamp
    if a == b:
        return
    # A gap since the last equal state means the fold computed this one in a
    # jump or a landing; no gap means one executed scan diverged.
    where = (
        ""
        if last_equal is None
        else f" (last equal state: scan {last_equal}, "
        + ("an executed scan diverged)" if last_equal == scan - 1 else "computed across a fold)")
    )
    keys = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
    diff = ", ".join(f"{k}: folded={a.get(k)!r} stepped={b.get(k)!r}" for k in keys[:8])
    more = f" (+{len(keys) - 8} more)" if len(keys) > 8 else ""
    STATS["mismatch"] += 1
    raise FoldMismatch(
        f"fold-check ({kind}): scan {scan} differs from stepping{where}: {diff}{more}"
    )


def _checked(kind: str, plc: Any, call: Callable[[], Any]) -> Any:
    global _depth
    if _depth:
        return call()
    reason = _skip_reason(plc)
    if reason is not None:
        STATS[f"skipped: {reason}"] += 1
        return call()
    start = plc._state.scan_id
    twin = _twin(plc)
    _depth += 1
    try:
        result = call()
    finally:
        _depth -= 1
    end = plc._state.scan_id
    n = end - start
    if n <= 0:
        STATS["no scans"] += 1
        return result
    if n > MAX_SCANS:
        STATS["skipped: longer than FOLD_CHECK_MAX_SCANS"] += 1
        return result
    produced = {
        sid: entry[0] for sid, entry in plc._recent_state_cache.items() if start < sid <= end
    }
    last_equal = start
    for _ in range(n):
        twin._run_single_scan(consume_pause_request=False)
        sid = twin._state.scan_id
        if sid in produced:
            _compare(kind, sid, produced[sid], twin._state, last_equal)
            last_equal = sid
    if twin._state.scan_id != end:
        STATS["mismatch"] += 1
        raise FoldMismatch(
            f"fold-check ({kind}): folded run ended at scan {end}, stepping at "
            f"{twin._state.scan_id}"
        )
    _compare(kind, end, plc._state, twin._state)
    STATS[f"checked: {kind}"] += 1
    STATS["checked scans"] += n
    return result


def install() -> None:
    """Wrap the fold entry points (idempotent)."""
    import pyrung.core.analysis.pilot.cyclefold as cyclefold
    import pyrung.core.fold as fold

    if getattr(fold, "_fold_check_installed", False):
        return
    setattr(fold, "_fold_check_installed", True)  # noqa: B010

    run_until, run_for, cycle = fold.fold_run_until, fold.fold_run_for, cyclefold.cycle_fold_until

    def fold_run_until(runner: Any, predicate: Any, *args: Any, **kwargs: Any) -> Any:
        return _checked("run_until", runner, lambda: run_until(runner, predicate, *args, **kwargs))

    def fold_run_for(runner: Any, seconds: float, *args: Any, **kwargs: Any) -> Any:
        return _checked("run_for", runner, lambda: run_for(runner, seconds, *args, **kwargs))

    def cycle_fold_until(plc: Any, predicate: Any, *args: Any, **kwargs: Any) -> Any:
        return _checked("cycle", plc, lambda: cycle(plc, predicate, *args, **kwargs))

    setattr(fold, "fold_run_until", fold_run_until)  # noqa: B010
    setattr(fold, "fold_run_for", fold_run_for)  # noqa: B010
    setattr(cyclefold, "cycle_fold_until", cycle_fold_until)  # noqa: B010


def wall_budget(seconds: float) -> float:
    """A wall-clock test budget, widened while the check steps a twin of every fold."""
    import pyrung.core.fold as fold

    return seconds * 10 if getattr(fold, "_fold_check_installed", False) else seconds


def summary() -> list[str]:
    return [f"  {key}: {count}" for key, count in sorted(STATS.items())]
