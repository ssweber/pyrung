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
(their callbacks act on the folded runner only), replay mode, a time mode other
than fixed-step, or a run longer
than ``FOLD_CHECK_MAX_SCANS`` scans (default 200000).
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Iterator
from typing import Any

import pyrung.core.compiled_run as compiled_run
import pyrung.core.scan_trace as scan_trace

MAX_SCANS = int(os.environ.get("FOLD_CHECK_MAX_SCANS", "200000"))
# Firing sizes for the loop: short ones to compare often, long ones to reach far.
_LOOP_BURSTS = (1, 1, 2, 1, 3, 7, 1, 1, 97, 1, 2, 1, 317, 1, 1, 997, 1, 1, 5, 1)
# Per-scan diagnostics a landing legitimately inflates (see fold._do_fold).
_IGNORED_TAGS = frozenset({"sys.scan_time_current_ms"})

STATS: Counter[str] = Counter()
SKIPS: dict[str, list[str]] = {}
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
    out = {k: _nan_free(v) for k, v in state.tags.items() if k not in _IGNORED_TAGS}
    out.update({f"mem:{k}": _nan_free(v) for k, v in state.memory.items()})
    return out


def _nan_free(value: Any) -> Any:
    """A REAL may hold NaN, which equals nothing; spell it so two NaNs compare equal."""
    return "<NaN>" if isinstance(value, float) and value != value else value


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
    SKIPS.setdefault(reason, []).append(os.environ.get("PYTEST_CURRENT_TEST", "?").split(" ")[0])


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
    except Exception:
        STATS["predicate raised on a twin scan"] += 1
        return _RAISED
    finally:
        plc._state = real
    return None if poison.touched else hit


_RAISED: Any = object()


def _checked(
    kind: str,
    plc: Any,
    call: Callable[[], Any],
    predicate: Callable[[Any], bool] | None = None,
    *,
    fine: bool = True,
) -> Any:
    global _depth
    if _depth:
        return call()
    reason = _skip_reason(plc)
    if reason is not None:
        _note_skip(f"skipped: {reason}")
        return call()
    start = plc._state.scan_id
    twin = _twin(plc)
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
    try:
        result = call()
    finally:
        _depth -= 1
        if fine:
            compiled_run.LOOP_OBSERVERS.remove(observe)
    STATS["loop scans in checked runs"] += scan_trace.TOTALS["loop scans"] - loop_scans_before
    end = plc._state.scan_id
    n = end - start
    if n <= 0:
        STATS["no scans"] += 1
        return result
    if n > MAX_SCANS:
        _note_skip("skipped: longer than FOLD_CHECK_MAX_SCANS")
        return result
    produced = {
        sid: entry[0] for sid, entry in plc._recent_state_cache.items() if start < sid <= end
    }
    STATS["states between loop firings"] += len(between_firings)
    produced.update({sid: state for sid, state in between_firings.items() if start < sid <= end})
    last_equal = start
    asked = False
    for _ in range(n):
        twin._run_single_scan(consume_pause_request=False)
        sid = twin._state.scan_id
        if ask is not None and sid < end:
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
            elif hit is not _RAISED:
                STATS["predicate scans"] += 1
                if hit:
                    STATS["mismatch"] += 1
                    raise FoldMismatch(
                        f"fold-check ({kind}): stop predicate true at scan {sid}, "
                        f"folded run stopped at {end}"
                    )
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
    compiled_scans = compiled_run.run_scans
    # Send every run through the kernel: a short run is the common test.
    setattr(compiled_run, "INTERPRETED_PREFIX", 1)  # noqa: B010
    # Every run in the loop, finely, and a self check mismatch is a failure.
    setattr(scan_trace, "MIN_RUN_SCANS", 1)  # noqa: B010
    setattr(scan_trace, "BURST_SIZES", _LOOP_BURSTS)  # noqa: B010
    setattr(scan_trace, "STRICT_SELF_CHECK", True)  # noqa: B010

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
    """Why this session proved nothing about the loop, or None."""
    checked = STATS["checked: compiled"]
    if checked >= 100 and not STATS["loop scans in checked runs"]:
        return f"{checked} compiled runs checked, and the scan loop ran no scan in any of them"
    return None


def summary() -> list[str]:
    lines = [f"  {key}: {count}" for key, count in sorted(STATS.items())]
    lines.extend(f"  scan loop, {key}: {count}" for key, count in sorted(scan_trace.TOTALS.items()))
    for reason, tests in sorted(SKIPS.items()):
        if reason.startswith("predicate skipped"):
            lines.append(f"  {reason}, tests:")
            lines.extend(f"    {t}" for t in sorted(set(tests))[:50])
    return lines
