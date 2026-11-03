"""Fold differential check: every folded run against a stepped twin.

Enabled by ``--fold-check`` (``make test-fold``).  The three fold entry points
(``fold_run_until``, ``fold_run_for``, PILOT's ``cycle_fold_until``) are wrapped.
Before a folded run starts, a twin is built from the same tip state, forces,
pending patches and synthesis overlay; after the run, the twin steps the same
number of scans one by one, and every state the folded run itself produced
(executed scans and landings, as cached) must equal the twin's state at that
scan: tags and memory exactly, the timestamp to within a microsecond.  A
run_until-style driver (``fold_run_until``, ``cycle_fold_until``) also hands the twin its stop predicate: if
it is true on the twin at a scan before the one where the folded run stopped, the fold
overshot and :class:`FoldMismatch` says so.  A predicate that reads the real runner
instead of its argument (it would answer about the wrong runner on a twin) or that
raises on the twin is not asked, and is counted.  A mismatch raises
:class:`FoldMismatch` inside the test that ran the fold, naming the scan and the differing keys.

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
than fixed-step.  A compiled run longer than ``FOLD_CHECK_MAX_SCANS`` scans (default
200000) is compared at its landing with real compiled scans, and its loop scans
counted apart from those compared scan by scan.

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

import os
import threading
import warnings
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pyrung.core.compiled_run as compiled_run
import pyrung.core.scan_trace as scan_trace

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
# fold suites reach.
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
    except Exception:
        # The test's own failure goes on, and the count is held to as well.
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


def _image(state: Any, ignored: frozenset[str] = frozenset()) -> dict[str, Any]:
    out = {k: _exact(v) for k, v in state.tags.items() if k not in ignored}
    out.update({f"mem:{k}": _exact(v) for k, v in state.memory.items()})
    return out


# The scan loop and real compiled scans add dt as stepping does, so they must land on
# the same bits.  Only these older paths multiply (a fold's macro step, the history
# seek, which uses it): drift this small reads alike there, and a landing inflates the
# per-scan diagnostic (see fold._do_fold).
_MACRO_STEP_KINDS = frozenset(
    {"run_until", "run_for", "cycle", "history", "history, stepped runner"}
)
_TIMESTAMP_TOLERANCE = 1e-6
_MACRO_STEP_IGNORED_TAGS = frozenset({"sys.scan_time_current_ms"})


def _compare(
    kind: str, scan: int, folded: Any, stepped: Any, last_equal: int | None = None
) -> None:
    macro = kind in _MACRO_STEP_KINDS
    ignored = _MACRO_STEP_IGNORED_TAGS if macro else frozenset()
    a, b = _image(folded, ignored), _image(stepped, ignored)
    if macro:
        if abs(folded.timestamp - stepped.timestamp) > _TIMESTAMP_TOLERANCE:
            a[":timestamp"], b[":timestamp"] = folded.timestamp, stepped.timestamp
    else:
        a[":timestamp"], b[":timestamp"] = _exact(folded.timestamp), _exact(stepped.timestamp)
    a[":scan_id"], b[":scan_id"] = folded.scan_id, stepped.scan_id
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
    if ours and plc._history_retention_scans is not None:
        theirs = {scan: state for scan, state in theirs.items() if scan >= min(ours)}
    a["checkpoints"], b["checkpoints"] = sorted(ours), sorted(theirs)
    if a != b:
        keys = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
        diff = ", ".join(f"{k}: folded={a.get(k)!r} stepped={b.get(k)!r}" for k in keys)
        raise _mismatch(f"fold-check ({kind}): the runner differs at scan {end}: {diff}")
    for scan, state in ours.items():
        _compare(f"{kind}, checkpoint", scan, state, theirs[scan])


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
    with itself.  The seek macro-steps (runner._replay_to_compiled), so its time gets
    the macro tolerance; a tag or a memory value does not.
    """
    end = plc._state.scan_id
    n = end - start
    middle = start + max(1, n // 2)
    seek_stats = getattr(plc, "_last_replay_seek_stats", None)
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
    if seek_stats is not None:
        plc._last_replay_seek_stats = seek_stats


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
    from pyrung.core.tag import Tag

    return (Tag, Condition, Expression, Block, Program)


def _isolated(predicate: Callable[[Any], bool], plc: Any) -> Callable[[Any], bool] | None:
    """A copy of ``predicate`` whose closure state is private to the twin, or None.

    A stateful predicate (a call counter, a recorder, ``rise()``'s edge memory) must
    not be advanced by the twin's extra calls, and must meet the twin's scans from the
    state it had when the folded run began.  So it is deep-copied before the folded
    run starts (functions included, cell by cell); the runner itself is shared, not
    copied.  An uncopyable predicate is not asked.
    """
    import copy
    import types

    memo: dict[int, Any] = {}
    shared_types = _shared_types()

    def clone_function(fn: types.FunctionType, memo: dict[int, Any]) -> Any:
        if id(fn) in memo:
            return memo[id(fn)]
        cells = None
        if fn.__closure__ is not None:
            cells = tuple(types.CellType() for _ in fn.__closure__)
        # Module-level names the function reads are state too: it gets a private
        # globals dict holding copies of just those names.
        names: set[str] = set()
        todo = [fn.__code__]
        while todo:
            code = todo.pop()
            names.update(code.co_names)
            todo.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
        private: dict[str, Any] = {"__builtins__": fn.__globals__.get("__builtins__", {})}
        private["__name__"] = fn.__globals__.get("__name__", "")
        new = types.FunctionType(fn.__code__, private, fn.__name__, fn.__defaults__, cells)
        memo[id(fn)] = new
        for name in names:
            if name in fn.__globals__:
                private[name] = copy.deepcopy(fn.__globals__[name], memo)
        new.__kwdefaults__ = copy.deepcopy(fn.__kwdefaults__, memo)
        new.__defaults__ = copy.deepcopy(fn.__defaults__, memo)
        new.__dict__.update(copy.deepcopy(fn.__dict__, memo))
        if cells is not None and fn.__closure__ is not None:
            for old, cell in zip(fn.__closure__, cells, strict=True):
                try:
                    contents = old.cell_contents
                except ValueError:
                    continue
                cell.cell_contents = copy.deepcopy(contents, memo)
        return new

    for obj in _reachable(predicate, plc, shared_types):
        if isinstance(obj, shared_types):
            memo[id(obj)] = obj
    shared = (type(plc),)
    table = copy._deepcopy_dispatch  # type: ignore[attr-defined]
    saved = {t: table.get(t) for t in (types.FunctionType, *shared)}
    table[types.FunctionType] = clone_function
    for t in shared:
        table[t] = lambda x, memo: x
    try:
        return copy.deepcopy(predicate, memo)
    except Exception:
        return None
    finally:
        for t, old in saved.items():
            if old is None:
                table.pop(t, None)
            else:
                table[t] = old


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

    Reuses the fold's own ``_traced_call`` (it swaps the twin's tip for the traced
    state, so a predicate reading ``twin.state`` works).  The real runner's state is
    poisoned for the call: a closure over the real runner is detected, not answered.
    A predicate that raises is unknown on this scan only (``False`` is not assumed).
    """
    from pyrung.core.fold import _traced_call

    real = plc._state
    poison = _PoisonedState()
    plc._state = poison
    try:
        hit = _traced_call(predicate, twin)[0]
    except _NotTheTwin:
        return None
    except Exception as exc:
        STATS["predicate raised on a twin scan"] += 1
        return _Raised(type(exc))
    finally:
        plc._state = real
    return None if poison.touched else hit


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
    raised: Exception,
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
    if stop_raised is not type(raised):
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
    raised: Exception,
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
    plc: Any, twin: Any, scans: int, start: int, predicate: Callable[[Any], bool] | None
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
                f"fold-check (compiled): plain compiled raised {type(exc).__name__} short of "
                f"scan {plc._state.scan_id}"
            ) from exc
        _compare("compiled", plc._state.scan_id, plc._state, twin._state)
        compare_runners("compiled", plc, twin, start)
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


def _checked(
    kind: str,
    plc: Any,
    call: Callable[[], Any],
    predicate: Callable[[Any], bool] | None = None,
    *,
    fine: bool = True,
    compiled_predicate: bool = False,
    budget: int | None = None,
) -> Any:
    """Run *call* and check it against a stepped twin; *budget* is the scans asked of it."""
    global _depth
    if _depth:
        return call()
    block = next((b for b in reversed(_own_twin_blocks) if b.plc is plc), None)
    if block is not None:
        start = plc._state.scan_id
        try:
            result = call()
        finally:
            block.landed = plc._state.scan_id
        _hold_to_the_scans_asked(kind, start, plc._state.scan_id, budget, predicate)
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
    raised: Exception | None = None
    try:
        result = call()
    except (FoldMismatch, scan_trace.SelfCheckMismatch):
        raise
    except Exception as exc:
        raised = exc
    finally:
        _depth -= 1
        if fine:
            compiled_run.LOOP_OBSERVERS.remove(observe)
    loop_scans = scan_trace.TOTALS["loop scans"] - loop_scans_before
    end = plc._state.scan_id
    n = end - start
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
        if kind == "compiled" and n <= MAX_PLAIN_COMPILED_SCANS:
            plain_ask = ask if compiled_predicate and raised is None else None
            scans = budget if budget is not None and plain_ask is not None else n
            _compare_with_plain_compiled(plc, twin, scans, start, plain_ask)
            STATS["loop scans, landing only (a run too long to step)"] += loop_scans
            if short and plain_ask is None:
                _judge_a_short_landing_on_its_last_scan(kind, plc, twin, ask, budget, n)
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
        if ask is not None and (sid < end or short or raised is not None):
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
    if short:
        if stop_held is False:
            raise _mismatch(
                f"fold-check ({kind}): returned at scan {end} with {budget - n} of {budget} "
                "scans left and the stop predicate false"
            )
        if stop_held is None:
            _note_skip("short landing not judged: no answer from the predicate")
    if kind == "compiled":
        _depth += 1
        try:
            compare_runners(kind, plc, twin, start)
            compare_history(kind, plc, twin, start, truth)
            compare_continuation(kind, plc, twin)
        finally:
            _depth -= 1
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
    compiled_scans = compiled_run.run_scans
    # Send every run through the kernel: a short run is the common test.
    setattr(compiled_run, "INTERPRETED_PREFIX", 1)  # noqa: B010
    # Every run in the loop, finely, and a self check mismatch is a failure.
    setattr(scan_trace, "MIN_RUN_SCANS", 1)  # noqa: B010
    setattr(scan_trace, "BURST_SIZES", _LOOP_BURSTS)  # noqa: B010
    setattr(scan_trace, "STRICT_SELF_CHECK", True)  # noqa: B010
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

    def fold_run_until(runner: Any, predicate: Any, *args: Any, **kwargs: Any) -> Any:
        return _checked(
            "run_until", runner, lambda: run_until(runner, predicate, *args, **kwargs), predicate
        )

    def fold_run_for(runner: Any, seconds: float, *args: Any, **kwargs: Any) -> Any:
        return _checked("run_for", runner, lambda: run_for(runner, seconds, *args, **kwargs))

    def cycle_fold_until(plc: Any, predicate: Any, *args: Any, **kwargs: Any) -> Any:
        return _checked("cycle", plc, lambda: cycle(plc, predicate, *args, **kwargs), predicate)

    setattr(fold, "fold_run_until", fold_run_until)  # noqa: B010
    setattr(fold, "fold_run_for", fold_run_for)  # noqa: B010
    setattr(cyclefold, "cycle_fold_until", cycle_fold_until)  # noqa: B010


def wall_budget(seconds: float) -> float:
    """A wall-clock test budget, widened while the check steps a twin of every fold."""
    import pyrung.core.fold as fold

    return seconds * 10 if getattr(fold, "_fold_check_installed", False) else seconds


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
