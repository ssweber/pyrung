"""Differential check of the long runs: every run in the guarded loop against a stepped twin.

Enabled by ``--fold-check`` (``make test-fold``).  The entry points
(``compiled_run.run_scans``, ``coast_run.coast_until``) are wrapped.  Before a run starts, a
twin is built from the same tip state, forces, pending patches and synthesis overlay; after
the run, the twin steps the same number of scans one by one, and every state the run itself
produced (executed scans and landings, as cached) must equal the twin's state at that scan,
tags, memory and timestamp exactly.  A driver with a stop predicate also hands it to the twin,
on its plain state and not through the read trace, which is what is being checked: if it is
true on the twin at a scan before the one where the run stopped, the run overshot and
:class:`FoldMismatch` says so.  A predicate that reads the real runner instead of its
argument (it would answer about the wrong runner on a twin) or that raises on the twin is not
asked, and is counted.  PILOT's opaque ``avoid=`` is judged on the scans a coast really executes,
by contract, so a true answer from one is let off on the scans between them, and that is counted
too; the other triggers of the same predicate are still judged there (:class:`_OpaqueAvoid`).  A
mismatch raises :class:`FoldMismatch` inside the test that ran it, naming the scan and the
differing keys.

The guarded scan loop runs in a finer mode here: it fires a few scans at a time
(``scan_trace.BURST_SIZES``), and each state it leaves between firings is compared
with the twin too, so an intermediate scan cannot diverge unseen.  A self check
mismatch raises, and every short run traces (``scan_trace.MIN_RUN_SCANS``).  The
summary counts the scans the loop ran and why it declined; a session that checks
compiled runs while the loop never fires fails (:func:`vacuous`), since it would
have proved nothing about the loop.

A run is skipped, and counted, when the twin cannot reproduce it: an active
``when()`` registration or a monitor other than a Harness feedback notifier
(their callbacks act on the folded runner only), replay mode, or a time mode other
than fixed-step.  A compiled run or a coast longer than ``FOLD_CHECK_MAX_SCANS`` scans
(default 200000) is compared at its landing with real compiled scans, and its loop scans
counted apart from those compared scan by scan.

A coast is compared like a compiled run: exactly, and with the runner around the state,
since both run real scans and the loop.  Its stretches fire finely here too.  It keeps its
production constants: its checkpoints and their force records are some of stepping's, equal
at each.  What it leaves in the firing indexes must match what stepping leaves
(:func:`compare_records`), scans it left out being stepped again to compare, where a stretch
is allowed one thing: a value column may say ``Unknown``, and a column that does may say a
tag varied where it did not.  how()'s clock, memory cap and ledger are no scan's doing, so a
coast they stop isn't held to the scans asked or to a true predicate, and all else is
compared at the landing it reached.

A compiled run is also held to what was asked of it: it lands on the scans asked for,
or short of them only where stepping's stop predicate holds.  The runner around the state
is compared too, its read mirror against its own state and a parked playhead included.  A
run that raises is compared the same way: stepping gets to the same scan and raises the
same type, or it is a mismatch.

A mismatch fails the test that met it and the session, whether or not the test or the
code under it caught the exception (an ``except Exception`` on the way out must not hide
one).  A test that provokes one on purpose says so with :func:`deliberate`.  The summary
prints every mismatch of the session with its test and the differing fields.
"""

from __future__ import annotations

import copyreg
import gc
import os
import threading
import types
import warnings
import weakref
from collections import Counter
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import pyrung.core.compiled_run as compiled_run
import pyrung.core.rung_firings as rung_firings
import pyrung.core.scan_trace as scan_trace
from pyrung.core.analysis.pilot.budget import SearchOutOfMemory, SearchOutOfTime
from pyrung.core.coast_run import work_charged
from pyrung.core.rung_firings import SearchQuestionRefused

MAX_SCANS = int(os.environ.get("FOLD_CHECK_MAX_SCANS", "200000"))
# Firing sizes for the loop: short ones to compare often, long ones to reach far.
_LOOP_BURSTS = (1, 1, 2, 1, 3, 7, 1, 1, 97, 1, 2, 1, 317, 1, 1, 997, 1, 1, 5, 1)
# Past FOLD_CHECK_MAX_SCANS a run is compared with real compiled scans; past this, not at all.
MAX_PLAIN_COMPILED_SCANS = 20_000_000
# Scans a landing is carried on, and scans a fork from inside a range is carried on.
CONTINUE_SCANS = 5
FORK_SCANS = 3
HISTORY_SAMPLES = 4
# RTC changes in a range that get a stepped state each side of them, for the seek.
RTC_CHANGES_SAMPLED = 4
# vacuous(): a run this long pays for a trace; below MIN_RUNS_TO_JUDGE of them the
# session is too small to say (a single test), and the shares are well under what the
# long-run suites reach.
LONG_ENOUGH_FOR_THE_LOOP = 50
MIN_RUNS_TO_JUDGE = 20
MIN_SHARE_OF_RUNS_IN_THE_LOOP = 0.25
MIN_SHARE_OF_SCANS_IN_THE_LOOP = 0.25

STATS: Counter[str] = Counter()
SKIPS: dict[str, list[str]] = {}
# Every mismatch of the session, caught by its test or not, as (test id, message).  Any
# of these fails the session (see conftest): nothing should have provoked them.
MISMATCHES: list[tuple[str, str]] = []
SELF_CHECK_MISMATCHES: list[tuple[str, str]] = []
# Those provoked on purpose, inside deliberate().
DECLARED: list[tuple[str, str]] = []
# Per test file: compiled runs long enough for the loop, and those that used it.
LONG_RUNS_BY_FILE: dict[str, list[int]] = {}
_depth = 0
# Mismatch counts of the deliberate() blocks open on this thread, innermost last.
_deliberate_blocks = threading.local()
# The runners inside an own_twin() block.
_own_twin_blocks: list[_OwnTwin] = []


class FoldMismatch(AssertionError):
    pass


def _test_id() -> str:
    return os.environ.get("PYTEST_CURRENT_TEST", "?").split(" ")[0]


def _open_deliberate_blocks() -> list[list[int]]:
    blocks = getattr(_deliberate_blocks, "stack", None)
    if blocks is None:
        blocks = _deliberate_blocks.stack = []
    return blocks


@contextmanager
def deliberate() -> Iterator[None]:
    """One mismatch in this block is the test's point, so it fails nothing by itself.

    Exactly one, on this thread: a block that saw none proves nothing, and a second
    mismatch or another thread's is a real one the first must not hide.
    """
    blocks = _open_deliberate_blocks()
    block = [0]
    blocks.append(block)
    message = "fold-check: deliberate() expects one mismatch, this block had {}"
    try:
        yield
    except BaseException:
        # The test's own failure goes on, pytest's being a BaseException, and the count is
        # held to as well.  A block left open would swallow the next test's mismatch.
        blocks.pop()
        if block[0] != 1:
            _mismatch(message.format(block[0]))
        raise
    blocks.pop()
    if block[0] != 1:
        raise _mismatch(message.format(block[0]))


def _declares_this_mismatch() -> bool:
    blocks = _open_deliberate_blocks()
    if not blocks:
        return False
    blocks[-1][0] += 1
    return blocks[-1][0] == 1


@dataclass
class _OwnTwin:
    """One own_twin() block: where its run landed, and the scan its last compare was of."""

    plc: Any
    start: int
    rtc_at_the_start: Any
    log_at_the_start: Any
    landed: int | None = None
    compared: int | None = None

    def unchecked(self) -> str | None:
        if self.compared is None:
            return "fold-check (own twin): the block never compared its run"
        end = self.plc._state.scan_id
        if self.landed is not None:
            end = max(end, self.landed)
        if self.compared != end:
            return (
                f"fold-check (own twin): the block's last compare is of scan {self.compared}, "
                f"its run got to scan {end}"
            )
        return None


@contextmanager
def own_twin(plc: Any) -> Iterator[Callable[[Any], None]]:
    """A run with no twin here: its function keeps state a twin in this process would share.

    The twin would go on from the run's count, so the test clears that state, steps its
    own runner and hands it to the compare this yields.  The block's last compare must be
    of where its run got to, even if the block raises, and of another runner: state,
    scan and runner are compared as a twin's would be.  Only *plc* is let off, and only
    the twin: it still lands on the scans asked of it, and other runners in the block stay
    checked.  tests/test_fold_check_stop.py pins the tests using this, and the summary
    lists them.
    """
    start = plc._state.scan_id
    block = _OwnTwin(plc, start, _rtc_at_the_start(plc, start), plc._scan_log)

    def compare(stepped: Any) -> None:
        if stepped is plc:
            raise _mismatch("fold-check (own twin): a run compared with itself proves nothing")
        scan = plc._state.scan_id
        if scan != stepped._state.scan_id:
            raise _mismatch(
                f"fold-check (own twin): run at scan {scan}, stepped at {stepped._state.scan_id}"
            )
        _compare("compiled", scan, plc._state, stepped._state)
        compare_runners("compiled", plc, stepped, block.start, own_twin_of=block)
        block.compared = scan

    _note_skip("skipped: the test steps its own twin")
    _own_twin_blocks.append(block)
    try:
        yield compare
    except FoldMismatch:
        raise
    except Exception:
        # The test's own failure goes on, and what went uncompared is on the record too.
        unchecked = block.unchecked()
        if unchecked is not None:
            _mismatch(unchecked)
        raise
    else:
        unchecked = block.unchecked()
        if unchecked is not None:
            raise _mismatch(unchecked)
    finally:
        _own_twin_blocks.pop()


def undeclared() -> list[tuple[str, str]]:
    """The mismatches of the session nothing declared: any of them is a failure."""
    return [*MISMATCHES, *SELF_CHECK_MISMATCHES]


def _mismatch(message: str) -> FoldMismatch:
    """Count and record a mismatch, and return the exception to raise."""
    if _declares_this_mismatch():
        STATS["mismatch, deliberate"] += 1
        DECLARED.append((_test_id(), message))
    else:
        STATS["mismatch"] += 1
        MISMATCHES.append((_test_id(), message))
    return FoldMismatch(message)


def _note_self_check_mismatch(message: str) -> None:
    if _declares_this_mismatch():
        DECLARED.append((_test_id(), message))
    else:
        SELF_CHECK_MISMATCHES.append((_test_id(), message))


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
    # The same base, not the same reading: a timedelta rounds to microseconds, so a
    # base moved to another sim time can read 1 us apart later on.
    twin._set_rtc_internal(plc._rtc_base, plc._rtc_base_sim_time)
    twin._running = plc._running
    # A set_rtc() before the run is logged under the scan to come: it is shared history.
    log = plc._scan_log
    changes = log._rtc_base_changes
    twin._scan_log._rtc_base_changes.update(
        {scan: change for scan, change in changes.items() if scan > state.scan_id}
    )
    twin._scan_log._rtc_origin = log.rtc_base_at(state.scan_id) or (
        plc._rtc_base,
        plc._rtc_base_sim_time,
    )
    # The twin starts forced, unlike a new runner: its log has to say so.
    twin._forces_last_recorded = {}
    return twin


def _exact(value: Any) -> Any:
    """A value spelled so ``==`` means the same type and, for a float, the same bits.

    ``True == 1`` and ``0.0 == -0.0`` hold in Python and are different values in a
    tag; NaN equals nothing, and its spelling here equals itself.
    """
    if isinstance(value, float):
        return ("float", value.hex())
    return (type(value).__name__, value)


def _image(state: Any) -> dict[str, Any]:
    out = {k: _exact(v) for k, v in state.tags.items()}
    out.update({f"mem:{k}": _exact(v) for k, v in state.memory.items()})
    return out


def _compare(
    kind: str, scan: int, folded: Any, stepped: Any, last_equal: int | None = None
) -> None:
    # The loop, real compiled scans and the history seek all add dt as stepping does, so
    # they land on the same bits.
    a, b = _image(folded), _image(stepped)
    a[":timestamp"], b[":timestamp"] = _exact(folded.timestamp), _exact(stepped.timestamp)
    a[":scan_id"], b[":scan_id"] = folded.scan_id, stepped.scan_id
    if a == b:
        return
    # A gap since the last equal state means the loop computed this one inside a firing;
    # no gap means one executed scan diverged.
    where = (
        ""
        if last_equal is None
        else f" (last equal state: scan {last_equal}, "
        + ("an executed scan diverged)" if last_equal == scan - 1 else "computed in the loop)")
    )
    keys = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
    diff = ", ".join(f"{k}: folded={a.get(k)!r} stepped={b.get(k)!r}" for k in keys[:8])
    more = f" (+{len(keys) - 8} more)" if len(keys) > 8 else ""
    raise _mismatch(f"fold-check ({kind}): scan {scan} differs from stepping{where}: {diff}{more}")


def _in_range(by_scan: dict[int, Any], start: int, end: int) -> dict[int, Any]:
    return {scan: by_scan[scan] for scan in sorted(by_scan) if start < scan <= end}


def _rtc_at_the_start(plc: Any, start: int) -> Any:
    """The base *start* ran on.  The log's origin answers for every scan before the first
    change, and a log with no change has none: the runner's own base holds then."""
    return plc._scan_log.rtc_base_at(start) or (plc._rtc_base, plc._rtc_base_sim_time)


def _rtc_parts(moment: datetime) -> tuple[Any, Any, int]:
    return moment.date(), moment.time().replace(microsecond=0), moment.microsecond


def _set_the_same_rtc(plc: Any, twin: Any, scan: int) -> bool:
    """The base each runner took at *scan* is one set: each part alike, or kept by both.

    A rung's apply_time keeps the date and microsecond its runner had, an apply_date the
    time of day, and two runners built apart had different ones.
    """

    def had_then(runner: Any, sim_time: float) -> datetime:
        base, base_sim_time = runner._scan_log.rtc_base_at(scan - 1)
        return base + timedelta(seconds=sim_time - base_sim_time)

    (ours, our_time) = plc._scan_log._rtc_base_changes[scan]
    (theirs, their_time) = twin._scan_log._rtc_base_changes[scan]
    if our_time != their_time:
        return False
    return all(
        our == their or (our == we_had and their == they_had)
        for our, their, we_had, they_had in zip(
            _rtc_parts(ours),
            _rtc_parts(theirs),
            _rtc_parts(had_then(plc, our_time)),
            _rtc_parts(had_then(twin, their_time)),
            strict=True,
        )
    )


def _compare_rtc_as_own_twins(
    plc: Any, twin: Any, a: dict[str, Any], b: dict[str, Any], block: _OwnTwin
) -> None:
    """Rewrite the two images so an RTC base each came by on its own compares equal.

    A reset in the block starts a fresh log on a base of its own, on each runner, so what
    the run started on is what that log answers, as the twin's is.
    """
    at_the_start = "as it started"
    started_on = block.rtc_at_the_start
    if plc._scan_log is not block.log_at_the_start:
        started_on = a["log: rtc base at the start"]
    twin_start = b["log: rtc base at the start"]
    if a["log: rtc base at the start"] == started_on:
        a["log: rtc base at the start"] = at_the_start
    b["log: rtc base at the start"] = at_the_start

    ours, theirs = a["log: rtc"], b["log: rtc"]
    last_set = max(ours, default=None)
    moved_as_set = (
        last_set == max(theirs, default=None)
        and last_set is not None
        and a["rtc base"] == ours[last_set][0]
        and b["rtc base"] == theirs[last_set][0]
    )
    for scan in ours.keys() & theirs.keys():
        if _set_the_same_rtc(plc, twin, scan):
            theirs[scan] = ours[scan]
    if moved_as_set and theirs[last_set] == ours[last_set]:
        b["rtc base"] = a["rtc base"]
        return
    if a["rtc base"] == started_on[0]:
        a["rtc base"] = at_the_start
    if b["rtc base"] == twin_start[0]:
        b["rtc base"] = at_the_start


def _runner_image(plc: Any, start: int, end: int) -> dict[str, Any]:
    """What a commit leaves on the runner besides the state, for scans after *start*."""
    log = plc._scan_log
    interval = plc._checkpoint_interval
    forces_at_checkpoints = {
        scan: forces
        for scan, forces in _in_range(log._force_changes_by_scan, start, end).items()
        if scan % interval == 0
    }
    return {
        "running": plc._running,
        "battery_present": plc._battery_present,
        "rtc base": plc._rtc_base,
        "rtc base sim time": _exact(plc._rtc_base_sim_time),
        "log: rtc base at the start": _rtc_at_the_start(plc, start),
        "forces": dict(plc._forces),
        "pending patches": dict(plc._pending_patches),
        "drained patches": dict(plc._this_scan_drained_patches),
        "bounds violations": sorted(plc._bounds_violations),
        "pause requested": plc._pause_requested_this_scan,
        "log: rtc": _in_range(log._rtc_base_changes, start, end),
        "log: patches": _in_range(log._patches_by_scan, start, end),
        "log: forces at checkpoints": forces_at_checkpoints,
        "log: io submits": _in_range(log._io_submits_by_scan, start, end),
        "log: io drains": _in_range(log._io_drains_by_scan, start, end),
        "log: lifecycle": [e for e in log._lifecycle_events if start < e.at_scan_id <= end],
    }


def _stale_in_the_read_mirror(plc: Any) -> list[str]:
    """Tags the runner's read mirror answers differently from its state.

    The next interpreted scan reads the mirror as it stands when it says it is of this
    state, and no copy of the runner carries it, so it is compared here.
    """
    mirror = plc._state_tags_read_cache
    if mirror is None or plc._state_tags_read_cache_state is not plc._state:
        return []
    tags = plc._state.tags
    return sorted(
        name
        for name in mirror.keys() | tags.keys()
        if name not in mirror or name not in tags or _exact(mirror[name]) != _exact(tags[name])
    )


# A coast keeps wider checkpoints in a stretch and thins them past a cap, which costs a replay
# run-up and no value.  So what it does keep has to be stepping's.
_KEEPS_FEWER_CHECKPOINTS = frozenset({"coast"})


def _hold_to_a_subset(
    kind: str,
    end: int,
    ours: dict[int, Any],
    theirs: dict[int, Any],
    a: dict[str, Any],
    b: dict[str, Any],
) -> None:
    """Our checkpoints and their force records are some of stepping's, equal at each."""
    forces = a.pop("log: forces at checkpoints")
    stepped_forces = b.pop("log: forces at checkpoints")
    not_stepped = sorted(set(ours) - set(theirs))
    if not_stepped:
        raise _mismatch(
            f"fold-check ({kind}): the runner at scan {end} has checkpoints stepping does not: "
            f"{not_stepped[:6]}"
        )
    # A replay anchored on a checkpoint reads its forces there.
    no_record = sorted(set(ours) - set(forces))
    if no_record:
        raise _mismatch(
            f"fold-check ({kind}): checkpoints with no force record at scan {end}: {no_record[:6]}"
        )
    for scan, held in forces.items():
        if scan not in stepped_forces or stepped_forces[scan] != held:
            raise _mismatch(
                f"fold-check ({kind}): forces recorded at scan {scan}: {held!r}, "
                f"stepped {stepped_forces.get(scan, 'none')!r}"
            )


def compare_runners(
    kind: str, plc: Any, twin: Any, start: int, *, own_twin_of: _OwnTwin | None = None
) -> None:
    """The runner around the state: flags, RTC base, scan log, checkpoints, playhead.

    *own_twin_of* is a twin that isn't a fork of *plc*: each read the wall clock when it was
    built, so their RTC bases differ by that and each answers to its own start.  A base that
    moved from it has to match the other's as far as what moved it says: all of a set_rtc(),
    the fields a rung's apply sets.
    """
    end = plc._state.scan_id
    a, b = _runner_image(plc, start, end), _runner_image(twin, start, end)
    if own_twin_of is not None:
        _compare_rtc_as_own_twins(plc, twin, a, b, own_twin_of)
    a["playhead"], b["playhead"] = plc._playhead, twin._playhead
    stale = _stale_in_the_read_mirror(plc)
    if stale:
        raise _mismatch(
            f"fold-check ({kind}): the read mirror at scan {end} is not of its state: {stale[:8]}"
        )
    # Retention trims the older ones, and each runner counts from its own start.
    ours = _in_range(plc._checkpoints, start, end)
    theirs = _in_range(twin._checkpoints, start, end)
    if kind in _KEEPS_FEWER_CHECKPOINTS:
        _hold_to_a_subset(kind, end, ours, theirs, a, b)
    else:
        if ours and plc._history_retention_scans is not None:
            theirs = {scan: state for scan, state in theirs.items() if scan >= min(ours)}
        a["checkpoints"], b["checkpoints"] = sorted(ours), sorted(theirs)
    if a != b:
        keys = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
        diff = ", ".join(f"{k}: folded={a.get(k)!r} stepped={b.get(k)!r}" for k in keys)
        raise _mismatch(f"fold-check ({kind}): the runner differs at scan {end}: {diff}")
    for scan, state in ours.items():
        _compare(f"{kind}, checkpoint", scan, state, theirs[scan])


def _touching(ranges: Any, first: int, last: int) -> list[Any]:
    """The written-out ranges with a scan in *first*..*last*, a repeat read as its copies."""
    return list(rung_firings._expanded(ranges, first, last))


def _spans(ranges: Any, first: int, last: int) -> list[tuple[int, int]]:
    """Scan ranges clipped to *first*..*last*, joined where they touch."""
    spans: list[tuple[int, int]] = []
    for r in _touching(ranges, first, last):
        low, high = max(r.start_scan_id, first), min(r.end_scan_id, last)
        if spans and spans[-1][1] == low - 1:
            spans[-1] = (spans[-1][0], high)
        else:
            spans.append((low, high))
    return spans


# The first scan a settle replaced in each timelines object, by id.  A settled range counts
# its column's values from its own first scan, so the column may stay known after stepping's
# went unknown.
_SETTLED_FROM: dict[int, int] = {}
_replace_range = rung_firings.RungFiringTimelines.replace_range


def _noting_replace_range(self: Any, settled: Any, first: int, last: int) -> None:
    _SETTLED_FROM[id(self)] = min(first, _SETTLED_FROM.get(id(self), first))
    _replace_range(self, settled, first, last)


rung_firings.RungFiringTimelines.replace_range = _noting_replace_range  # type: ignore[method-assign]


def _column_at(ranges: Any, scan: int) -> Any:
    """A column's value at *scan*; a scan no range covers is a scan with no write."""
    for r in _touching(ranges, scan, scan):
        return rung_firings._value_at(r, scan)
    return rung_firings._MISSING_WRITE


def _column_differs(
    ours: Any, theirs: Any, first: int, last: int, settled_from: int | None
) -> int | None:
    """Scan where two value columns differ.  Ours says ``Unknown`` wherever stepping does.

    Ours may also say ``Unknown`` where stepping knows the value.  From *settled_from* on, ours
    may know a value where stepping says ``Unknown``.

    Between range boundaries each side is constant, alternating or arithmetic, so the
    two scans at each end of a stretch of no boundary settle the whole of it.
    """
    ours, theirs = _touching(ours, first, last), _touching(theirs, first, last)
    cuts = sorted(
        {first, last + 1}
        | {r.start_scan_id for r in (*ours, *theirs) if first < r.start_scan_id <= last}
        | {r.end_scan_id + 1 for r in (*ours, *theirs) if first < r.end_scan_id + 1 <= last}
    )
    for low, stop in zip(cuts, cuts[1:], strict=False):
        high = stop - 1
        for scan in sorted({low, min(low + 1, high), max(high - 1, low), high}):
            mine = _column_at(ours, scan)
            stepped = _column_at(theirs, scan)
            if stepped is rung_firings._UNKNOWN_VALUE:
                if mine is not rung_firings._UNKNOWN_VALUE and (
                    settled_from is None or scan < settled_from
                ):
                    return scan  # a column counts its values as stepping does
                continue
            if mine is rung_firings._UNKNOWN_VALUE:
                continue  # a range the loop could not speak for
            if _exact(mine) != _exact(stepped):
                return scan
    return None


def _unknown_scans(ranges: Any, first: int, last: int) -> list[tuple[int, int]]:
    touching = _touching(ranges, first, last)
    return _spans([r for r in touching if isinstance(r.payload, rung_firings.Unknown)], first, last)


def _record_pieces(plc: Any, first: int, end: int) -> list[tuple[Any, int, int, bool]]:
    """Who holds the records of *first*..*end*: *plc*, or for scans it left out a replay of them.

    It is what a settle would install, stepped here on the side, since a settle changes the
    runner under test: a question that comes later still has to find those scans as it left them.
    """
    pieces = []
    cursor = first
    for low, high in sorted(plc._unrecorded_scans):
        low, high = max(low, first), min(high, end)
        if low > high:
            continue
        if cursor < low:
            pieces.append((plc, cursor, low - 1, False))
        pieces.append((plc._stepped_again(low, high, None), low, high, True))
        STATS["scans out of the records, settled to compare"] += high - low + 1
        cursor = high + 1
    if cursor <= end:
        pieces.append((plc, cursor, end, False))
    return pieces


def compare_records(kind: str, plc: Any, twin: Any, start: int) -> None:
    """What scans after *start* left in the firing indexes, against what stepping left."""
    for holder, first, last, settled in _record_pieces(plc, start + 1, plc._state.scan_id):
        _compare_records_over(kind, holder, twin, first, last, settled)
    STATS[f"records compared: {kind}"] += 1


def _compare_records_over(
    kind: str, plc: Any, twin: Any, first: int, end: int, settled: bool
) -> None:
    for name, ours, theirs in (
        ("rung", plc._rung_firing_timelines, twin._rung_firing_timelines),
        ("node", plc._node_firing_timelines, twin._node_firing_timelines),
        ("committed", plc._committed_tag_timelines, twin._committed_tag_timelines),
    ):
        for key in ours._fired_ranges.keys() | theirs._fired_ranges.keys():
            a = _spans(ours._fired_ranges.get(key, ()), first, end)
            b = _spans(theirs._fired_ranges.get(key, ()), first, end)
            if a != b:
                raise _mismatch(
                    f"fold-check ({kind}): {name} {key!r} fired on {a[:6]}, stepped on {b[:6]}"
                )
        for key in ours._value_timelines.keys() | theirs._value_timelines.keys():
            a = ours._value_timelines.get(key, ())
            b = theirs._value_timelines.get(key, ())
            # A column goes unknown at its hundredth value, so ours goes when stepping's does.
            # Ours may say Unknown sooner for a range the loop could not speak for.
            settled_from = first if settled else _SETTLED_FROM.get(id(ours))
            if (
                key in ours._unknown_keys
                and key not in theirs._unknown_keys
                and _SETTLED_FROM.get(id(theirs)) is None
            ):
                raise _mismatch(f"fold-check ({kind}): {name} {key!r} went unknown, stepped not")
            scan = _column_differs(a, b, first, end, settled_from)
            if scan is not None:
                raise _mismatch(
                    f"fold-check ({kind}): {name} {key!r} at scan {scan}: "
                    f"{_column_at(a, scan)!r}, stepped {_column_at(b, scan)!r}"
                )
            STATS["record cells Unknown"] += sum(
                high - low + 1 for low, high in _unknown_scans(a, first, end)
            )
        for key in ours._varied_ranges.keys() | theirs._varied_ranges.keys():
            a = _spans(ours._varied_ranges.get(key, ()), first, end)
            b = _spans(theirs._varied_ranges.get(key, ()), first, end)
            if a == b:
                continue
            # Saying it varied asks a reader to replay, so only an Unknown may say it.
            unknown = _unknown_scans(ours._value_timelines.get(key, ()), first, end)
            mine = {scan for low, high in a for scan in range(low, high + 1)}
            stepped = {scan for low, high in b for scan in range(low, high + 1)}
            allowed = {scan for low, high in unknown for scan in range(low, high + 1)}
            if stepped - mine or mine - stepped - allowed:
                raise _mismatch(
                    f"fold-check ({kind}): {name} {key!r} varied on {a[:6]}, stepped on {b[:6]}"
                )


def _one_scan(plc: Any) -> type[BaseException] | None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            plc.run(1)
        except Exception as exc:
            return type(exc)
    return None


def compare_continuation(kind: str, plc: Any, twin: Any, scans: int = CONTINUE_SCANS) -> None:
    """Scan on from the landing on copies, so what the state does not show comes out.

    A stale flag or a stale RTC base lands equal and differs only once the next scan
    reads it.  Copies, so the caller's runners do not move; a copy starts with no read
    mirror, which is why :func:`compare_runners` compares the real runner's own.
    """
    ours, theirs = _twin(plc), _twin(twin)
    base = ours._state.scan_id
    for _ in range(scans):
        raised_ours, raised_theirs = _one_scan(ours), _one_scan(theirs)
        if raised_ours is not raised_theirs:
            raise _mismatch(
                f"fold-check ({kind}): past the landing, scan {theirs._state.scan_id}: "
                f"folded raised {raised_ours}, stepped raised {raised_theirs}"
            )
        if raised_ours is not None:
            return
        _compare(f"{kind}, past the landing", ours._state.scan_id, ours._state, theirs._state)
        STATS["scans continued past a landing"] += 1
    compare_runners(f"{kind}, past the landing", ours, theirs, min(base, ours._state.scan_id))


def compare_history(kind: str, plc: Any, twin: Any, start: int, truth: dict[int, Any]) -> None:
    """``history.at`` and ``fork_from`` inside the range, against the states stepped.

    Both runners rebuild from checkpoints and the scan log, so a commit a range skipped
    shows up here even if the landing is right.  *truth* holds the twin's states as it
    stepped them, and the seek is asked of both runners with their caches emptied: the
    stepped runner too, since a seek that is wrong by itself must not pass by agreeing
    with itself.
    """
    end = plc._state.scan_id
    n = end - start
    middle = start + max(1, n // 2)
    kept = {
        runner: (dict(runner._recent_state_cache), dict(runner._replay_slabs))
        for runner in (plc, twin)
    }
    for runner in kept:
        runner._recent_state_cache.clear()
        runner._replay_slabs.clear()
    for scan, state in sorted(truth.items()):
        if plc.history.contains(scan) and twin.history.contains(scan):
            _compare("history", scan, plc.history.at(scan), state)
            _compare("history, stepped runner", scan, twin.history.at(scan), state)
            STATS["history.at compared"] += 1
    for runner, (recent, slabs) in kept.items():
        runner._recent_state_cache.clear()
        runner._recent_state_cache.update(recent)
        runner._replay_slabs.clear()
        runner._replay_slabs.update(slabs)
    if plc.history.contains(middle) and twin.history.contains(middle):
        fork, twin_fork = plc.fork_from(middle), twin.fork_from(middle)
        # A public fork drops forces and PILOT holds, by contract, and both forks do.
        # It keeps a harness: the twin holds the plant and has no harness to keep.
        if plc._harness is not None:
            plc._harness.fork_onto(twin_fork)
        _compare("history", middle, fork._state, twin_fork._state)
        for _ in range(FORK_SCANS):
            raised, twin_raised = _one_scan(fork), _one_scan(twin_fork)
            if raised is not twin_raised:
                raise _mismatch(
                    f"fold-check ({kind}): on a fork from scan {middle}: "
                    f"folded raised {raised}, stepped raised {twin_raised}"
                )
            if raised is not None:
                break
            _compare("history", fork._state.scan_id, fork._state, twin_fork._state)
            STATS["scans compared on a fork from inside a range"] += 1


# how() ends on these, and they are BaseExceptions so its own catches leave them alone.
_SEARCH_LIMITS = (SearchOutOfTime, SearchOutOfMemory, SearchQuestionRefused)


class _OpaqueAvoid:
    """What an opaque ``avoid=`` member said on the twin's scan, apart from the rest of the ask.

    The contract judges such a member on the scans a coast executes and not in between, and
    nothing else in the predicate shares that.  So on a twin's scan it answers here and False to
    the ask, which goes on to the triggers after it; :func:`_predicate_on_twin` puts the answers
    together, and ``alone`` says an opaque member was all that made it true.

    A class because it is what a predicate's private copy shares with the original.
    """

    on_twin = False
    answered_true = False
    alone = False


def _judged_apart(trigger: Any) -> Any:
    """*trigger*, an opaque ``avoid=`` member, answering a twin through :class:`_OpaqueAvoid`."""

    def predicate(state: Any, _member: Any = trigger.predicate) -> bool:
        if not _OpaqueAvoid.on_twin:
            return _member(state)
        if _member(state):
            _OpaqueAvoid.answered_true = True
        return False

    return replace(trigger, predicate=predicate)


class _NotTheTwin(BaseException):
    """The predicate touched the real runner's state while the twin was asked.

    A BaseException so a predicate's own ``except Exception`` cannot swallow it; the
    poison also sets a flag that is tested after the call.
    """


class _PoisonedState:
    """Stands in for the real runner's state while a predicate is asked on a twin."""

    def __init__(self) -> None:
        object.__setattr__(self, "touched", False)

    def __getattr__(self, name: str) -> Any:
        object.__setattr__(self, "touched", True)
        raise _NotTheTwin(name)

    def __getitem__(self, key: Any) -> Any:
        object.__setattr__(self, "touched", True)
        raise _NotTheTwin(str(key))


def _note_skip(reason: str) -> None:
    """Count a run (or predicate check) that went unchecked, with the test that ran it."""
    STATS[reason] += 1
    SKIPS.setdefault(reason, []).append(_test_id())


def _shared_types() -> tuple[type, ...]:
    """Immutable-by-contract pyrung objects a predicate refers to: shared, not copied."""
    from pyrung.core.condition import Condition
    from pyrung.core.expression import Expression
    from pyrung.core.memory_block import Block
    from pyrung.core.program import Program
    from pyrung.core.state import SystemState
    from pyrung.core.structure import _StructRuntime
    from pyrung.core.tag import Tag

    return (Tag, Condition, Expression, Block, Program, SystemState, _StructRuntime)


def _isolated(predicate: Callable[[Any], bool], plc: Any) -> Callable[[Any], bool] | None:
    """A copy of ``predicate`` whose closure state is private to the twin, or None.

    A stateful predicate (a call counter, a recorder, ``rise()``'s edge memory) must
    not be advanced by the twin's extra calls, and must meet the twin's scans from the
    state it had when the folded run began.  So it is copied before the folded run starts,
    cell by cell.  The runner itself is shared, not copied.  A class is not copied either, so
    a function reached only through a class (a ``__call__`` or another method) still reads and
    writes the real module-level names, class attributes and closure cells.  An uncopyable
    predicate is not asked.
    """
    try:
        return _PrivateCopy(plc).of(predicate)
    except Exception:
        return None


class _PrivateCopy:
    """A deep copy that clones functions too, built on public protocols only.

    Python 3.14's ``copy.deepcopy()`` returns a function unchanged before it reads its dispatch
    table, so no hook can make it clone one.  This copier walks the predicate itself instead.  It
    rebuilds other objects through the pickle protocol (``copyreg.dispatch_table``,
    ``__reduce_ex__(4)``), as ``deepcopy()`` does.  A module is shared with the run, so it
    cannot be copied.

    An object's own ``__deepcopy__`` is checked after it runs.  Its copy is refused if it
    reaches any of these:

    - a function this copier did not make
    - a bound method whose function is not its class's own
    - a method or a builtin bound to an object that was not copied
    - a live weak reference or weak proxy to an object that was not copied
    - a module
    - a runner other than the one shared
    - more than ``MAX_WALK`` objects
    """

    # The most objects the check of an own ``__deepcopy__`` walks before it refuses the copy.
    MAX_WALK = 20_000

    _LEAVES = frozenset(
        {
            types.NoneType,
            types.EllipsisType,
            types.NotImplementedType,
            int,
            float,
            bool,
            complex,
            str,
            bytes,
            range,
            types.CodeType,
            property,
        }
    )

    def __init__(self, plc: Any) -> None:
        self.plc_type = type(plc)
        self.shared = _shared_types()
        self.memo: dict[int, Any] = {id(plc): plc}
        # Each original stays alive until the copy is done, so no id in the memo is reused.
        self.originals: list[Any] = [plc]
        # One private globals dict per module, so two functions of it still share their names.
        self.module_globals: dict[int, dict[str, Any]] = {}

    def _remember(self, obj: Any, new: Any) -> Any:
        self.memo[id(obj)] = new
        self.originals.append(obj)
        return new

    def _is_shared(self, obj: Any) -> bool:
        return isinstance(obj, type) or type(obj) is self.plc_type or isinstance(obj, self.shared)

    def of(self, obj: Any) -> Any:
        if id(obj) in self.memo:
            return self.memo[id(obj)]
        cls = type(obj)
        if cls in self._LEAVES or self._is_shared(obj):
            return obj
        if isinstance(obj, types.ModuleType):
            raise TypeError(f"module {obj.__name__} is shared with the run")
        if cls is weakref.ref:
            referent = obj()
            if referent is None or self._is_shared(referent):
                return obj
            raise TypeError("a live weak reference reaches an object the run shares")
        if cls is types.FunctionType:
            return self._function(obj)
        if cls is types.MethodType:
            func = obj.__func__
            if not _defined_on_its_class(obj):
                func = self.of(func)
            owner = self.of(obj.__self__)
            if id(obj) in self.memo:  # a cycle through the method copied it already
                return self.memo[id(obj)]
            return self._remember(obj, types.MethodType(func, owner))
        if cls is list:
            new_list: list[Any] = self._remember(obj, [])
            new_list.extend(self.of(item) for item in obj)
            return new_list
        if cls is dict:
            new_dict: dict[Any, Any] = self._remember(obj, {})
            for key, value in obj.items():
                new_dict[self.of(key)] = self.of(value)
            return new_dict
        if cls is tuple:
            items = [self.of(item) for item in obj]
            if id(obj) in self.memo:  # a cycle through the tuple copied it already
                return self.memo[id(obj)]
            same = all(a is b for a, b in zip(items, obj, strict=True))
            return self._remember(obj, obj if same else tuple(items))
        copier = getattr(obj, "__deepcopy__", None)
        if copier is not None:
            return self._remember(obj, self._own_deepcopy(obj, copier))
        return self._rebuilt(obj)

    def _own_deepcopy(self, obj: Any, copier: Callable[[dict[int, Any]], Any]) -> Any:
        """*obj*'s own ``__deepcopy__``, refused where its copy still reaches the original's code.

        That copy usually goes through ``copy.deepcopy()``, which never clones a function.  So a
        function in it that this copier did not make is the user's own.  The check walks the copy
        through ``gc.get_referents()`` and refuses a copy too large to walk.  It follows a weak
        reference or a weak proxy too, which ``gc.get_referents()`` does not.

        Some refusals fail closed and refuse a copy that would have been private.  A method, a
        weak reference or a weak proxy in the copy must point to one of the copies in the memo,
        or to the copy itself.  So the check refuses one that points to an object the copy made
        without putting it in the memo.  It refuses a weak proxy to a shared object too, since a
        proxy does not say what it points to.
        """
        made = {id(v) for v in self.memo.values()}
        new = copier(self.memo)
        name = type(obj).__name__
        if new is obj and callable(obj):
            raise TypeError(f"{name}.__deepcopy__ returns the callable itself")
        # An object bound to a method or a builtin must be one of the copies, or the copy itself.
        copied = {id(v) for v in self.memo.values()} | {id(new)}
        seen: set[int] = set()
        todo = [new]
        while todo:
            o = todo.pop()
            if id(o) in seen or id(o) in made:
                continue
            seen.add(id(o))
            if len(seen) > self.MAX_WALK:
                raise TypeError(f"{name}.__deepcopy__ made a copy too large to check")
            if isinstance(o, weakref.ProxyTypes):
                # Checked first, since isinstance() asks a proxy's target for its class.
                target = self._proxied(o, new, name)
                if target is not None:
                    todo.append(target)
                continue
            if type(o) is self.plc_type:
                raise TypeError(f"{name}.__deepcopy__ copies the runner")
            if isinstance(o, (type, *self.shared)):
                continue
            if isinstance(o, types.ModuleType):
                raise TypeError(
                    f"{name}.__deepcopy__ keeps module {o.__name__}, shared with the run"
                )
            if isinstance(o, types.FunctionType):
                raise TypeError(f"{name}.__deepcopy__ keeps a function of the original")
            if isinstance(o, weakref.ref):
                referent = o()
                if referent is None or self._is_shared(referent):
                    continue
                if isinstance(referent, types.MethodType):
                    # A WeakMethod makes a new bound method on each call.
                    if not (
                        _defined_on_its_class(referent)
                        and self._bound_to_a_copy(referent.__self__, copied)
                    ):
                        raise TypeError(f"{name}.__deepcopy__ keeps a method bound to the original")
                    todo.append(referent.__self__)
                    continue
                if id(referent) not in copied:
                    raise TypeError(f"{name}.__deepcopy__ keeps a weak reference to the original")
                todo.append(referent)
                continue
            if isinstance(o, types.MethodType):
                if not _defined_on_its_class(o):
                    raise TypeError(f"{name}.__deepcopy__ keeps a method of the original")
                if not self._bound_to_a_copy(o.__self__, copied):
                    raise TypeError(f"{name}.__deepcopy__ keeps a method bound to the original")
                todo.append(o.__self__)
                continue
            if isinstance(o, (types.BuiltinFunctionType, types.MethodWrapperType)):
                if not self._bound_to_a_copy(o.__self__, copied):
                    raise TypeError(f"{name}.__deepcopy__ keeps a builtin bound to the original")
                continue
            todo.extend(gc.get_referents(o))
        return new

    def _proxied(self, proxy: Any, new: Any, name: str) -> Any:
        """The copy *proxy* points to, None for a dead one, or TypeError for any other."""
        try:
            proxy.__class__  # noqa: B018
        except ReferenceError:
            return None
        for candidate in (*self.memo.values(), new):
            if any(ref is proxy for ref in weakref.getweakrefs(candidate)):
                return candidate
        raise TypeError(f"{name}.__deepcopy__ keeps a weak proxy to an object it did not copy")

    def _bound_to_a_copy(self, owner: Any, copied: set[int]) -> bool:
        return (
            owner is None
            or isinstance(owner, (type, types.ModuleType))
            or self._is_shared(owner)
            or id(owner) in copied
        )

    def _function(self, fn: types.FunctionType) -> types.FunctionType:
        cells = None
        if fn.__closure__ is not None:
            cells = tuple(types.CellType() for _ in fn.__closure__)
        # Module-level names the function reads are state too.  So the copy gets a private
        # globals dict holding copies of just those names.
        names: set[str] = set()
        todo = [fn.__code__]
        while todo:
            code = todo.pop()
            names.update(code.co_names)
            todo.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
        private = self.module_globals.get(id(fn.__globals__))
        if private is None:
            private = {
                "__builtins__": fn.__globals__.get("__builtins__", {}),
                "__name__": fn.__globals__.get("__name__", ""),
            }
            self.module_globals[id(fn.__globals__)] = private
            self.originals.append(fn.__globals__)
        new = types.FunctionType(fn.__code__, private, fn.__name__, None, cells)
        self._remember(fn, new)
        for name in names:
            if name in fn.__globals__ and name not in private:
                private[name] = self.of(fn.__globals__[name])
        new.__kwdefaults__ = self.of(fn.__kwdefaults__)
        new.__defaults__ = self.of(fn.__defaults__)
        new.__dict__.update(self.of(fn.__dict__))
        for old, cell in zip(fn.__closure__ or (), cells or (), strict=True):
            if _filled(old):
                cell.cell_contents = self.of(old.cell_contents)
        return new

    def _rebuilt(self, obj: Any) -> Any:
        """*obj* rebuilt from what pickling it would save, each part copied."""
        reductor = copyreg.dispatch_table.get(type(obj))
        saved: Any = reductor(obj) if reductor is not None else obj.__reduce_ex__(4)
        if isinstance(saved, str):  # saved by its name, as a class is
            return obj
        parts: list[Any] = [*saved, None, None, None, None]
        rebuild, args, state, list_items, dict_items, state_setter = parts[:6]
        new = self._remember(obj, rebuild(*self.of(args)))
        if state is not None:
            state = self.of(state)
            if state_setter is not None:
                state_setter(new, state)
            elif hasattr(new, "__setstate__"):
                new.__setstate__(state)
            else:
                slots = None
                if isinstance(state, tuple) and len(state) == 2:
                    state, slots = state
                if state:
                    new.__dict__.update(state)
                for key, value in (slots or {}).items():
                    setattr(new, key, value)
        for item in list_items or ():
            new.append(self.of(item))
        for key, value in dict_items or ():
            new[self.of(key)] = self.of(value)
        return new


def _defined_on_its_class(method: types.MethodType) -> bool:
    """The method's function is its class's own, which is shared since no class is copied."""
    owner = method.__self__
    func = method.__func__
    for cls in (owner if isinstance(owner, type) else type(owner)).__mro__:
        for attr in vars(cls).values():
            if attr is func or (
                isinstance(attr, (classmethod, staticmethod)) and attr.__func__ is func
            ):
                return True
    return False


def _reachable(root: Any, plc: Any, stop: tuple[type, ...] = ()) -> Iterator[Any]:
    """Objects reachable from a predicate: closure cells, defaults, the module-level
    names a function reads, containers and instance attributes (capped).  The runner,
    modules and classes are not entered, nor are instances of ``stop`` types."""
    import types

    seen: set[int] = set()
    todo = [root]
    count = 0
    while todo and count < 20000:
        obj = todo.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        count += 1
        if obj is plc or isinstance(obj, (types.ModuleType, type)):
            continue
        yield obj
        if stop and isinstance(obj, stop):
            continue
        if isinstance(obj, types.FunctionType):
            todo.extend(c.cell_contents for c in obj.__closure__ or () if _filled(c))
            todo.extend(obj.__defaults__ or ())
            codes = [obj.__code__]
            while codes:
                code = codes.pop()
                todo.extend(obj.__globals__[n] for n in code.co_names if n in obj.__globals__)
                codes.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
        elif isinstance(obj, types.MethodType):
            todo.append(obj.__self__)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                todo.extend((k, v))
        elif isinstance(obj, (list, tuple, set, frozenset)):
            todo.extend(obj)
        elif hasattr(obj, "__dict__"):
            todo.extend(vars(obj).values())
        elif hasattr(obj, "__slots__"):
            todo.extend(getattr(obj, name) for name in obj.__slots__ if hasattr(obj, name))


def _fingerprint(root: Any, plc: Any) -> tuple[Any, ...]:
    """What a predicate could carry between calls, flattened (shared types excluded)."""
    out: list[Any] = []
    for obj in _reachable(root, plc, _shared_types()):
        if obj is None or isinstance(obj, (bool, int, float, str, bytes, complex)):
            out.append((type(obj).__name__, obj))
        elif isinstance(obj, (dict, list, tuple, set, frozenset)):
            out.append((type(obj).__name__, len(obj)))
        else:
            out.append((type(obj).__name__, id(obj) if callable(obj) else 0))
    return tuple(out)


def _filled(cell: Any) -> bool:
    try:
        _ = cell.cell_contents
    except ValueError:
        return False
    return True


def _predicate_on_twin(predicate: Callable[[Any], bool], plc: Any, twin: Any) -> bool | None:
    """``predicate`` as the twin sees it, or None when the twin cannot answer it.

    Asked on the twin's plain state, as a user's predicate would read it, and not through
    the read trace: the twin is the truth a trace that misreads a predicate has to answer
    to.  The real runner's state is poisoned for the call: a closure over the real runner
    is detected, not answered.  A predicate that raises is unknown on this scan only
    (``False`` is not assumed).
    """
    _OpaqueAvoid.answered_true = False
    _OpaqueAvoid.on_twin = True
    real = plc._state
    poison = _PoisonedState()
    plc._state = poison
    try:
        hit = bool(predicate(twin._state))
    except _NotTheTwin:
        return None
    except Exception as exc:
        STATS["predicate raised on a twin scan"] += 1
        return _Raised(type(exc))
    finally:
        plc._state = real
        _OpaqueAvoid.on_twin = False
    _OpaqueAvoid.alone = _OpaqueAvoid.answered_true and not hit
    return None if poison.touched else hit or _OpaqueAvoid.answered_true


class _Raised:
    """A predicate's answer on a twin scan: it raised this."""

    def __init__(self, raised: type[Exception]) -> None:
        self.raised = raised


def _judge_a_raised_run(
    kind: str,
    plc: Any,
    twin: Any,
    start: int,
    truth: dict[int, Any],
    raised: BaseException,
    stop_raised: type[Exception] | None,
    predicate_answers: bool,
) -> None:
    """The twin is at the scan the run raised after: it must raise the same, and match.

    A raise out of a run leaves a runner the caller can go on with, and it is the one
    place the run's landing goes uncompared otherwise.  The raise is either the stop
    predicate's, on the landing itself, or the next scan's.
    """
    global _depth
    end = plc._state.scan_id
    # how()'s limits are no scan's doing and stepping has none to run out of, so it isn't
    # asked to raise.  What the run left is compared below like any other.
    if stop_raised is not type(raised) and not isinstance(raised, _SEARCH_LIMITS):
        _depth += 1
        try:
            on_twin = _one_scan(twin)
        finally:
            _depth -= 1
        if on_twin is None and end == start:
            _note_skip("raised run not judged: raised before any scan")
            return
        if on_twin is None and not predicate_answers:
            _note_skip("raised run not judged: the predicate has no copy to ask")
            return
        if on_twin is not type(raised):
            stepped = "ran on" if on_twin is None else f"raised {on_twin.__name__}"
            raise _mismatch(
                f"fold-check ({kind}): run raised {type(raised).__name__} after scan {end}, "
                f"stepping {stepped}"
            )
    _depth += 1
    try:
        _compare(kind, end, plc._state, twin._state)
        compare_runners(kind, plc, twin, start)
        if truth:
            compare_history(kind, plc, twin, start, truth)
    finally:
        _depth -= 1
    STATS["raised runs compared"] += 1


def _judge_the_raise_alone(
    kind: str,
    plc: Any,
    raised: BaseException,
    ask: Callable[[Any], bool] | None,
    predicate: Callable[[Any], bool] | None,
) -> None:
    """A raised run no twin can follow: a scan stepped from its landing must raise the same.

    That says the run stopped on a scan that raises.  The scans before it are not stepped,
    so one stepping would have raised on goes unseen.
    """
    global _depth
    landing = _twin(plc)
    hit = None if ask is None else _predicate_on_twin(ask, plc, landing)
    if isinstance(hit, _Raised) and hit.raised is type(raised):
        return
    _depth += 1
    try:
        on_landing = _one_scan(landing)
    finally:
        _depth -= 1
    if on_landing is None and predicate is not None and ask is None:
        _note_skip("raised run not judged: the predicate has no copy to ask")
        return
    if on_landing is not type(raised):
        stepped = "ran on" if on_landing is None else f"raised {on_landing.__name__}"
        raise _mismatch(
            f"fold-check ({kind}): run raised {type(raised).__name__} after scan "
            f"{plc._state.scan_id}, stepping {stepped}"
        )
    STATS["raised runs too long to compare, their last scan judged"] += 1


def _compare_with_plain_compiled(
    kind: str,
    plc: Any,
    twin: Any,
    scans: int,
    start: int,
    predicate: Callable[[Any], bool] | None,
) -> None:
    """Too long to step: the landing against real compiled scans, the floor under the loop.

    A guard this deep (a wrap, an hour's edge) is not reached by any shorter run.  The
    twin is given the scans asked for and asked *predicate* on every scan, so a run that
    overshoots or stops early lands it elsewhere.  Scans between are not compared; the
    summary counts these apart.  The twin's scans are the check's own: the run's totals,
    which a rate test reads, do not gain them.
    """
    global _depth
    traces = scan_trace.MAX_TRACES_PER_RUN
    totals = scan_trace.TOTALS.copy()
    scan_trace.MAX_TRACES_PER_RUN = 0
    _depth += 1
    try:
        try:
            compiled_run.run_scans(twin, scans, predicate)
        except Exception as exc:
            raise _mismatch(
                f"fold-check ({kind}): plain compiled raised {type(exc).__name__} short of "
                f"scan {plc._state.scan_id}"
            ) from exc
        _compare(kind, plc._state.scan_id, plc._state, twin._state)
        compare_runners(kind, plc, twin, start)
    finally:
        _depth -= 1
        scan_trace.MAX_TRACES_PER_RUN = traces
        scan_trace.TOTALS.clear()
        scan_trace.TOTALS.update(totals)
    _note_skip("compared at the landing only: too long to step")
    STATS["scans compared with plain compiled"] += plc._state.scan_id - start


# Stands for the predicate where a run may stop short for a reason of its own.
_STOPPED_BY_THE_RAISE: Any = object()


def _hold_to_the_scans_asked(
    kind: str, start: int, end: int, budget: int | None, predicate: Any
) -> None:
    n = end - start
    if budget is not None and (n > budget or (n < budget and predicate is None)):
        raise _mismatch(
            f"fold-check ({kind}): asked for {budget} scans from scan {start}, landed at {end}"
        )


def _budget_in_scans(
    kind: str, start: int, end: int, budget: int | None, loop_scans: int, per_unit: int
) -> int | None:
    """*budget* as scans, for a run paying a unit per *per_unit* of its loop scans.

    What it charged has to fit *budget*, by the coast's own ``work_charged`` on the loop scans
    counted as they ran, and all of it spent is a run that stopped on its budget.  Whatever
    is left would have paid for that many more loop scans, so stopping there is a short
    landing, judged as any other.
    """
    if budget is None or per_unit == 1:
        return budget
    n = end - start
    charged = work_charged(n, n - loop_scans, per_unit)
    if charged > budget:
        raise _mismatch(
            f"fold-check ({kind}): asked for {budget} units at {per_unit} loop scans a unit "
            f"from scan {start}, landed at {end} having charged {charged}"
        )
    return n + (budget - charged) * per_unit


def _judge_a_short_landing_on_its_last_scan(
    kind: str, plc: Any, twin: Any, ask: Any, budget: int | None, n: int
) -> None:
    """Too long to step, on a predicate the compiled twin can't ask: ask it at the landing."""
    hit = None if ask is None else _predicate_on_twin(ask, plc, twin)
    if hit is None or isinstance(hit, _Raised):
        _note_skip("short landing not judged: too long to step, no answer from the predicate")
    elif not hit:
        raise _mismatch(
            f"fold-check ({kind}): returned at scan {plc._state.scan_id} with "
            f"{(budget or n) - n} of {budget} scans left and the stop predicate false"
        )


def _judge_a_ledger_stop_at_its_landing(kind: str, plc: Any, twin: Any, ask: Any) -> None:
    """Too long to step: a coast asks its predicate before its ledger, so it was false here."""
    hit = None if ask is None else _predicate_on_twin(ask, plc, twin)
    if hit is None or isinstance(hit, _Raised):
        _note_skip("ledger stop not judged: too long to step, no answer from the predicate")
    elif hit:
        raise _mismatch(
            f"fold-check ({kind}): stopped for its ledger at scan {plc._state.scan_id} with "
            "the stop predicate true"
        )


def _hold_to_the_ledger(kind: str, left: int | None, said: dict[str, int]) -> None:
    """A coast's stop for its ledger against what the ledger had left as the coast began.

    The ledger is the search's own, so this can't say *left* is right, and a loop scan is
    charged after its seek, so it says nothing about those.  Interpreted scans it can count.
    A raised coast says none of this.
    """
    if "kernel_scans" not in said:
        return
    interpreted = said["kernel_scans"]
    if "out_of_work" in said:
        if left is None or interpreted < left:
            raise _mismatch(
                f"fold-check ({kind}): stopped for its ledger after {interpreted} interpreted "
                f"scans with {left} left"
            )
    # A landing a stretch ran is noted too, past the last scan the ledger could stop.
    elif left is not None and interpreted > left + 1:
        raise _mismatch(
            f"fold-check ({kind}): ran {interpreted} interpreted scans with {left} left"
        )


def _checked(
    kind: str,
    plc: Any,
    call: Callable[[], Any],
    predicate: Callable[[Any], bool] | None = None,
    *,
    fine: bool = True,
    compiled_predicate: bool = False,
    budget: int | None = None,
    out_of_work: Callable[[], bool] | None = None,
    loop_scans_per_unit: int = 1,
    executed_scans: Collection[int] | None = None,
) -> Any:
    """Run *call* and check it against a stepped twin; *budget* is the scans asked of it.

    *loop_scans_per_unit* is what a unit of *budget* buys of them in the loop.

    *executed_scans* are the scans a coast ran interpreted, filled by *call*: an opaque
    ``avoid=`` is held to its answer on those, and without them on every scan.

    *out_of_work* says how() stopped the run for its ledger: as with its clock that is no
    scan's doing, so it isn't held to its budget.  The coast asks its predicate first, so the
    predicate has to be false where it landed.  All else is compared at that landing.
    """
    global _depth
    if _depth:
        return call()
    block = next((b for b in reversed(_own_twin_blocks) if b.plc is plc), None)
    if block is not None:
        start = plc._state.scan_id
        loop_scans_before = scan_trace.TOTALS["loop scans"]
        try:
            result = call()
        finally:
            block.landed = plc._state.scan_id
        end = plc._state.scan_id
        n = end - start
        budget = _budget_in_scans(
            kind,
            start,
            end,
            budget,
            scan_trace.TOTALS["loop scans"] - loop_scans_before,
            loop_scans_per_unit,
        )
        _hold_to_the_scans_asked(
            kind,
            start,
            end,
            n if out_of_work is not None and out_of_work() else budget,
            predicate,
        )
        return result
    reason = _skip_reason(plc)
    if reason is not None:
        _note_skip(f"skipped: {reason}")
        return call()
    start = plc._state.scan_id
    twin = _twin(plc)
    # Parked or not: stepping moves a playhead only if it stands at the tip.
    twin._playhead = plc._playhead
    ask = None
    if predicate is not None:
        ask = _isolated(predicate, plc)
        if ask is None:
            _note_skip("predicate skipped: not copyable")
    _depth += 1
    between_firings: dict[int, Any] = {}

    def observe(runner: Any, state: Any) -> None:
        if runner is plc:
            between_firings[state.scan_id] = state

    if fine:
        compiled_run.LOOP_OBSERVERS.append(observe)
    loop_scans_before = scan_trace.TOTALS["loop scans"]
    result = None
    raised: BaseException | None = None
    try:
        result = call()
    except (FoldMismatch, scan_trace.SelfCheckMismatch):
        raise
    except (Exception, *_SEARCH_LIMITS) as exc:
        raised = exc
    finally:
        _depth -= 1
        if fine:
            compiled_run.LOOP_OBSERVERS.remove(observe)
    loop_scans = scan_trace.TOTALS["loop scans"] - loop_scans_before
    end = plc._state.scan_id
    n = end - start
    ledger_stop = out_of_work is not None and out_of_work()
    budget = _budget_in_scans(kind, start, end, budget, loop_scans, loop_scans_per_unit)
    if ledger_stop:
        budget = n
    # A raise is a run stopped early, by whatever raised: that is judged below.
    _hold_to_the_scans_asked(
        kind, start, end, budget, predicate if raised is None else _STOPPED_BY_THE_RAISE
    )
    if n <= 0 and raised is None:
        if budget:
            raise _mismatch(f"fold-check ({kind}): asked for {budget} scans, ran none")
        STATS["no scans"] += 1
        return result
    # A short landing has to be the scan the predicate stops on, so it is asked there too.
    short = raised is None and budget is not None and n < budget
    if n > MAX_SCANS:
        if kind in ("compiled", "coast") and n <= MAX_PLAIN_COMPILED_SCANS:
            plain_ask = ask if compiled_predicate and raised is None else None
            scans = budget if budget is not None and plain_ask is not None else n
            _compare_with_plain_compiled(kind, plc, twin, scans, start, plain_ask)
            STATS["loop scans, landing only (a run too long to step)"] += loop_scans
            if short and plain_ask is None:
                _judge_a_short_landing_on_its_last_scan(kind, plc, twin, ask, budget, n)
            if ledger_stop:
                _judge_a_ledger_stop_at_its_landing(kind, plc, twin, ask)
            if raised is not None:
                hit = None if ask is None else _predicate_on_twin(ask, plc, twin)
                stop_raised = hit.raised if isinstance(hit, _Raised) else None
                _judge_a_raised_run(
                    kind,
                    plc,
                    twin,
                    start,
                    {},
                    raised,
                    stop_raised,
                    ask is not None or predicate is None,
                )
                raise raised
        else:
            _note_skip("skipped: longer than FOLD_CHECK_MAX_SCANS")
            STATS["loop scans, not compared (a run too long to compare)"] += loop_scans
            if raised is not None:
                _judge_the_raise_alone(kind, plc, raised, ask, predicate)
                raise raised
        return result
    STATS["loop scans compared scan by scan"] += loop_scans
    if n >= LONG_ENOUGH_FOR_THE_LOOP and kind == "compiled":
        STATS["compiled runs long enough for the loop"] += 1
        STATS["compiled runs long enough, in the loop"] += bool(loop_scans)
        by_file = LONG_RUNS_BY_FILE.setdefault(_test_id().split("::")[0], [0, 0])
        by_file[0] += 1
        by_file[1] += bool(loop_scans)
    produced = {
        sid: entry[0] for sid, entry in plc._recent_state_cache.items() if start < sid <= end
    }
    STATS["states between loop firings"] += len(between_firings)
    produced.update({sid: state for sid, state in between_firings.items() if start < sid <= end})
    last_equal = start
    asked = False
    # Bounded: a handful of the stepped states, for the seek to answer to.
    truth: dict[int, Any] = {}
    truth_every = max(1, n // HISTORY_SAMPLES)
    sample = {start + 1, end - 1}
    changes = sorted(_in_range(plc._scan_log._rtc_base_changes, start, end))
    for scan in changes[:RTC_CHANGES_SAMPLED]:
        sample.update((scan - 1, scan))
    stop_held = None
    stop_raised = None
    let_off = False
    for _ in range(n):
        try:
            twin._run_single_scan(consume_pause_request=False)
        except Exception as exc:
            raise _mismatch(
                f"fold-check ({kind}): stepping raised {type(exc).__name__} after scan "
                f"{twin._state.scan_id}, the run went on to scan {end}"
            ) from exc
        sid = twin._state.scan_id
        if (sid - start) % truth_every == 0 or sid in sample:
            truth[sid] = twin._state
        if ask is not None and (sid < end or short or ledger_stop or raised is not None):
            first = not asked
            before = _fingerprint(ask, plc) if first else None
            hit = _predicate_on_twin(ask, plc, twin)
            asked = True
            if first and hit is not None and _fingerprint(ask, plc) != before:
                _note_skip("predicate skipped: carries state between calls")
                ask = None
            elif hit is None:
                _note_skip("predicate skipped: reads the real runner")
                ask = None
            elif isinstance(hit, _Raised):
                if sid == end:
                    stop_raised = hit.raised
            else:
                STATS["predicate scans"] += 1
                if sid == end:
                    stop_held = bool(hit)
                elif (
                    hit
                    and _OpaqueAvoid.alone
                    and executed_scans is not None
                    and sid not in executed_scans
                ):
                    if not let_off:
                        _note_skip("predicate let off: true between the scans it is judged on")
                    let_off = True
                elif hit:
                    raise _mismatch(
                        f"fold-check ({kind}): stop predicate true at scan {sid}, "
                        f"folded run stopped at {end}"
                    )
        if sid in produced:
            _compare(kind, sid, produced[sid], twin._state, last_equal)
            last_equal = sid
    if raised is not None:
        predicate_answers = predicate is None or ask is not None
        _judge_a_raised_run(kind, plc, twin, start, truth, raised, stop_raised, predicate_answers)
        raise raised
    if twin._state.scan_id != end:
        raise _mismatch(
            f"fold-check ({kind}): folded run ended at scan {end}, stepping at "
            f"{twin._state.scan_id}"
        )
    _compare(kind, end, plc._state, twin._state)
    if ledger_stop and stop_held:
        raise _mismatch(
            f"fold-check ({kind}): stopped for its ledger at scan {end} with the stop "
            "predicate true"
        )
    if short:
        if stop_held is False:
            raise _mismatch(
                f"fold-check ({kind}): returned at scan {end} with {budget - n} of {budget} "
                "scans left and the stop predicate false"
            )
        if stop_held is None:
            _note_skip("short landing not judged: no answer from the predicate")
    if kind in ("compiled", "coast"):
        _depth += 1
        try:
            compare_runners(kind, plc, twin, start)
            if kind == "coast":
                compare_records(kind, plc, twin, start)
            compare_history(kind, plc, twin, start, truth)
            compare_continuation(kind, plc, twin)
        finally:
            _depth -= 1
    STATS[f"checked: {kind}"] += 1
    STATS["checked scans"] += n
    return result


def install() -> None:
    """Wrap the entry points (idempotent)."""
    import pyrung.core.analysis.pilot.coast as pilot_coast
    import pyrung.core.coast_run as coast_run

    if getattr(compiled_run, "_fold_check_installed", False):
        return
    setattr(compiled_run, "_fold_check_installed", True)  # noqa: B010

    compiled_scans = compiled_run.run_scans
    coast = coast_run.coast_until
    # Send every run through the kernel: a short run is the common test.
    setattr(compiled_run, "INTERPRETED_PREFIX", 1)  # noqa: B010
    # Every run in the loop, finely, and a self check mismatch is a failure.
    setattr(scan_trace, "MIN_RUN_SCANS", 1)  # noqa: B010
    setattr(scan_trace, "BURST_SIZES", _LOOP_BURSTS)  # noqa: B010
    setattr(scan_trace, "STRICT_SELF_CHECK", True)  # noqa: B010
    # The loop jumps the cycles it predicts.  STRICT_CYCLE_CHECK fails a run whose scans
    # disagree with a prediction.
    setattr(coast_run, "CYCLE_PREDICTIONS", "jump")  # noqa: B010
    setattr(coast_run, "STRICT_CYCLE_CHECK", True)  # noqa: B010
    if _note_self_check_mismatch not in scan_trace.SELF_CHECK_OBSERVERS:
        scan_trace.SELF_CHECK_OBSERVERS.append(_note_self_check_mismatch)

    def run_scans(runner: Any, scans: int, predicate: Any = None, **kwargs: Any) -> Any:
        # A run too long for the twin is not compared, so it need not fire finely.
        scan_trace.BURST_SIZES = _LOOP_BURSTS if scans <= MAX_SCANS else None
        try:
            return _checked(
                "compiled",
                runner,
                lambda: compiled_scans(runner, scans, predicate, **kwargs),
                predicate,
                fine=scans <= MAX_SCANS,
                compiled_predicate=predicate is not None and kwargs.get("compiled_predicate", True),
                budget=scans,
            )
        finally:
            scan_trace.BURST_SIZES = _LOOP_BURSTS

    setattr(compiled_run, "run_scans", run_scans)  # noqa: B010

    def coast_until(
        plc: Any, predicate: Any, *, budget: int, stats: Any = None, **kwargs: Any
    ) -> Any:
        said = {} if stats is None else stats
        said.pop("out_of_work", None)
        said.pop("kernel_scans", None)
        memo = plc._coast_memo
        left = None if memo is None or memo.work_left is None else max(1, memo.work_left())
        executed: set[int] = set()
        noted = kwargs.pop("on_kernel_scan", None)

        def on_kernel_scan(scan: int) -> None:
            executed.add(scan)
            if noted is not None:
                noted(scan)

        result = _checked(
            "coast",
            plc,
            lambda: coast(
                plc, predicate, budget=budget, stats=said, on_kernel_scan=on_kernel_scan, **kwargs
            ),
            predicate,
            budget=budget,
            out_of_work=lambda: "out_of_work" in said,
            loop_scans_per_unit=kwargs.get("loop_scans_per_unit", 1),
            executed_scans=executed,
        )
        _hold_to_the_ledger("coast", left, said)
        return result

    setattr(coast_run, "coast_until", coast_until)  # noqa: B010
    setattr(pilot_coast, "coast_until", coast_until)  # noqa: B010

    arm_avoid = pilot_coast.CoastSession.arm_avoid

    def arm_avoid_apart(session: Any, avoid: Any) -> None:
        arm_avoid(session, avoid)
        session._avoid_triggers = tuple(
            _judged_apart(t) if t.condition is None and t.predicate_reads is None else t
            for t in session._avoid_triggers
        )

    setattr(pilot_coast.CoastSession, "arm_avoid", arm_avoid_apart)  # noqa: B010


def export() -> dict[str, Any]:
    """This process's record, in plain types: what an xdist worker sends its controller."""
    return {
        "stats": dict(STATS),
        "totals": dict(scan_trace.TOTALS),
        "skips": SKIPS,
        "mismatches": MISMATCHES,
        "self_check_mismatches": SELF_CHECK_MISMATCHES,
        "declared": DECLARED,
        "long_runs_by_file": LONG_RUNS_BY_FILE,
    }


def absorb(record: dict[str, Any]) -> None:
    """Add a worker's export() here, so the session is judged on all of its runs."""
    STATS.update(record["stats"])
    scan_trace.TOTALS.update(record["totals"])
    for reason, tests in record["skips"].items():
        SKIPS.setdefault(reason, []).extend(tests)
    MISMATCHES.extend(tuple(m) for m in record["mismatches"])
    SELF_CHECK_MISMATCHES.extend(tuple(m) for m in record["self_check_mismatches"])
    DECLARED.extend(tuple(m) for m in record["declared"])
    for name, (runs, in_loop) in record["long_runs_by_file"].items():
        by_file = LONG_RUNS_BY_FILE.setdefault(name, [0, 0])
        by_file[0] += runs
        by_file[1] += in_loop


def wall_budget(seconds: float) -> float:
    """A wall-clock test budget, widened while the check steps a twin of every run."""
    return seconds * 10 if getattr(compiled_run, "_fold_check_installed", False) else seconds


def vacuous() -> str | None:
    """Why this session proved little about the loop, or None.

    By run and by share, so one scan among thousands cannot pass.  A share, since a
    fair number of programs decline for honest reasons (see scan_trace's docstring).
    """
    runs = STATS["compiled runs long enough for the loop"]
    if runs < MIN_RUNS_TO_JUDGE:
        return None
    in_loop = STATS["compiled runs long enough, in the loop"]
    if in_loop < runs * MIN_SHARE_OF_RUNS_IN_THE_LOOP:
        return (
            f"of {runs} compiled runs long enough for the scan loop, only {in_loop} used it "
            f"(at least {MIN_SHARE_OF_RUNS_IN_THE_LOOP:.0%} expected)"
        )
    scans = STATS["checked scans"]
    loop_scans = STATS["loop scans compared scan by scan"]
    if loop_scans < scans * MIN_SHARE_OF_SCANS_IN_THE_LOOP:
        return (
            f"of {scans} checked scans, only {loop_scans} ran in the scan loop "
            f"(at least {MIN_SHARE_OF_SCANS_IN_THE_LOOP:.0%} expected)"
        )
    return None


def files_without_the_loop() -> list[tuple[str, int]]:
    """Test files with enough long runs to judge, none of which used the loop.

    Reported, not failed on: vacuous() judges the session, so a file whose loop died goes
    unseen there, but a file of programs that all decline for honest reasons looks the same.
    """
    return [
        (name, runs)
        for name, (runs, in_loop) in sorted(LONG_RUNS_BY_FILE.items())
        if runs >= MIN_RUNS_TO_JUDGE and not in_loop
    ]


_SKIPS_LISTED_BY_TEST = (
    "predicate skipped",
    "predicate let off",
    "compared at the landing",
    "skipped: longer",
    "skipped: the test steps its own twin",
    "raised run not judged",
    "short landing not judged",
)


def summary() -> list[str]:
    lines = [f"  {key}: {count}" for key, count in sorted(STATS.items())]
    lines.extend(f"  scan loop, {key}: {count}" for key, count in sorted(scan_trace.TOTALS.items()))
    lines.extend(f"  MISMATCH in {test}: {message}" for test, message in MISMATCHES)
    lines.extend(
        f"  SELF CHECK MISMATCH in {test}: {message}" for test, message in SELF_CHECK_MISMATCHES
    )
    lines.extend(f"  deliberate mismatch in {test}: {message}" for test, message in DECLARED)
    lines.extend(
        f"  no scan loop in {name}: {runs} long compiled runs"
        for name, runs in files_without_the_loop()
    )
    for reason, tests in sorted(SKIPS.items()):
        if reason.startswith(_SKIPS_LISTED_BY_TEST):
            lines.append(f"  {reason}, tests:")
            lines.extend(f"    {t}" for t in sorted(set(tests))[:50])
    return lines
