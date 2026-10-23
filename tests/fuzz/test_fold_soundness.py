"""Fold soundness fuzzer — saturated-heartbeat and frozen-rung shapes.

**Saturated heartbeat:**  A system clock gating a rung that reads an accumulator
through a comparison is exactly the family `_runtime_soft_clocks` promotes soft
once the comparison saturates (`fold._comparisons_saturated`).  Before saturation
the clock must stay bound (every edge honored); after, its edges may be skipped —
but only while the gated output stays frozen, which the observe-before-skip
plateau guard re-confirms each window.

**Frozen-rung writes:**  A clock-gated rung whose inputs are *structurally*
unreachable from any varying tag (accumulators, churn, etc.), and whose writes
are read only by rungs after it, has those writes excluded from the plateau
guard (``_frozen_rung_writes``): the landing scan rebuilds their exact stepped
value, so clock edges no longer break the plateau window.  Every tag, frozen or
not, must still be bit-equal to scan-by-scan.

Both families compare the whole tag image exactly, timer accumulators
included: a fold replays each timer's per-scan carry recurrence.

Reproducer on failure: the drawn ``spec`` is the reproducer — it is printed via
``note`` and embedded in the assertion message, and ``_build(spec)`` /
``_build_frozen(spec)`` reconstructs the exact program.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import example, given, note, settings

from pyrung import Bool, Counter, Int, Program, Rung, Timer, calc, count_up, on_delay, out
from pyrung.core import rise, system
from pyrung.core.runner import PLC

from .conftest import MAX_EXAMPLES

_CLOCKS = ["clock_500ms", "clock_1s"]
_FORMS = ["gt", "ge", "lt", "le", "eq"]
_SRC = ["timer", "counter"]
_BODY = ["calc", "coil"]


@st.composite
def _heartbeat_spec(draw: st.DrawFn) -> dict:
    """A saturated-heartbeat program: a ramping timer/counter, a clock-gated
    rung reading its accumulator through one comparison, and a frozen-input
    body.  The threshold sits strictly below the preset so the comparison
    saturates partway up the ramp (the interesting window)."""
    src = draw(st.sampled_from(_SRC))
    if src == "timer":
        preset = draw(st.integers(min_value=2_000, max_value=20_000))  # ms
    else:
        preset = draw(st.integers(min_value=200, max_value=2_000))  # counts
    return {
        "src": src,
        "preset": preset,
        "threshold": draw(st.integers(min_value=1, max_value=preset - 1)),
        "form": draw(st.sampled_from(_FORMS)),
        "clock": draw(st.sampled_from(_CLOCKS)),
        "body": draw(st.sampled_from(_BODY)),
        "dt": draw(st.sampled_from([0.005, 0.010, 0.020])),
        "a": draw(st.integers(min_value=0, max_value=50)),
        "b": draw(st.integers(min_value=0, max_value=50)),
    }


def _cmp(acc, form: str, k: int):
    if form == "gt":
        return acc > k
    if form == "ge":
        return acc >= k
    if form == "lt":
        return acc < k
    if form == "le":
        return acc <= k
    return acc == k


def _build(spec: dict):
    """Reconstruct the program from a spec.  Returns ``(program, done_tag)``."""
    Enable = Bool("Enable", external=True)
    Reset = Bool("Reset", external=True)  # left False — the ramp runs to Done
    A = Int("A", external=True)
    B = Int("B", external=True)
    Extent = Int("Extent")
    Beat = Bool("Beat")
    clock_tag = getattr(system.sys, spec["clock"])

    with Program(strict=False) as prog:
        if spec["src"] == "timer":
            src = Timer.clone("Tmr")
            with Rung(Enable):
                on_delay(src, spec["preset"], "ms")
        else:
            src = Counter.clone("Ctr")
            with Rung(Enable):
                count_up(src, spec["preset"]).reset(Reset)
        with Rung(rise(clock_tag), _cmp(src.Acc, spec["form"], spec["threshold"])):
            if spec["body"] == "calc":
                calc(A + B, Extent)
            else:
                out(Beat)
    return prog, src.Done


def _run(spec: dict, *, fold: bool) -> dict:
    prog, done = _build(spec)
    plc = PLC(prog, dt=spec["dt"])
    plc.patch({"Enable": True, "A": spec["a"], "B": spec["b"]})
    plc.run_until(done, max_cycles=40_000, fold=fold)
    # Every tag, the scan-time diagnostics included: a landing records the
    # normal dt of the scan it stands for.
    return dict(plc.state.tags)


def test_saturated_heartbeat_fold_is_bit_equal() -> None:
    """Folded run_until(Done) must equal scan-by-scan across the heartbeat family."""

    @given(spec=_heartbeat_spec())
    @settings(max_examples=MAX_EXAMPLES, deadline=None)
    # The canonical case from the unit suite — always exercised.
    @example(
        spec={
            "src": "timer",
            "preset": 20_000,
            "threshold": 100,
            "form": "gt",
            "clock": "clock_1s",
            "body": "calc",
            "dt": 0.010,
            "a": 3,
            "b": 4,
        }
    )
    # Boundary-straddle regression: Done (1676 counts) lands at t=16.75 s, exactly
    # a clock_500ms rise edge.  Accumulated scan time drifts to 16.74999999999982
    # (clock low) while the fold's big-step timestamp lands on 16.75 (clock high) —
    # the OTE coil disagreed (Beat True vs False) until clock phase was grid-snapped
    # (system_points.clock_phase).
    @example(
        spec={
            "src": "counter",
            "preset": 1676,
            "threshold": 1,
            "form": "gt",
            "clock": "clock_500ms",
            "body": "coil",
            "dt": 0.010,
            "a": 0,
            "b": 0,
        }
    )
    def inner(spec: dict) -> None:
        folded = _run(spec, fold=True)
        stepped = _run(spec, fold=False)
        if folded != stepped:
            diffs = {
                k: (folded.get(k, "<missing>"), stepped.get(k, "<missing>"))
                for k in sorted(set(folded) | set(stepped))
                if folded.get(k) != stepped.get(k)
            }
            note(f"spec={spec}")
            note(f"fold != nofold: {diffs}")
            raise AssertionError(
                f"fold disagreed with scan-by-scan\n  spec={spec}\n  diffs={diffs}"
            )

    inner()


# ---------------------------------------------------------------------------
# Frozen-rung write exclusion
# ---------------------------------------------------------------------------

_FROZEN_BODY = ["calc", "coil"]
_N_FROZEN = [1, 2, 3]


@st.composite
def _frozen_write_spec(draw: st.DrawFn) -> dict:
    """A program mixing frozen and non-frozen clock-gated rungs.

    - A ramping timer/counter advancing to Done.
    - One clock-gated rung reading the accumulator through a comparison
      (non-frozen — makes the clock hard).
    - One or more clock-gated rungs reading only external inputs (frozen).

    The frozen rungs' writes are excluded from the plateau guard; non-frozen
    tags must stay bit-equal to scan-by-scan.
    """
    src = draw(st.sampled_from(_SRC))
    if src == "timer":
        preset = draw(st.integers(min_value=2_000, max_value=20_000))
    else:
        preset = draw(st.integers(min_value=200, max_value=2_000))
    n_frozen = draw(st.sampled_from(_N_FROZEN))
    return {
        "src": src,
        "preset": preset,
        "threshold": draw(st.integers(min_value=1, max_value=preset - 1)),
        "form": draw(st.sampled_from(_FORMS)),
        "clock": draw(st.sampled_from(_CLOCKS)),
        "dt": draw(st.sampled_from([0.005, 0.010, 0.020])),
        "a": draw(st.integers(min_value=0, max_value=50)),
        "b": draw(st.integers(min_value=0, max_value=50)),
        "n_frozen": n_frozen,
        "frozen_bodies": [draw(st.sampled_from(_FROZEN_BODY)) for _ in range(n_frozen)],
    }


def _build_frozen(spec: dict):
    """Build a program with mixed frozen/non-frozen clock-gated rungs."""
    Enable = Bool("Enable", external=True)
    A = Int("A", external=True)
    B = Int("B", external=True)
    AccView = Int("AccView")
    clock_tag = getattr(system.sys, spec["clock"])

    frozen_tags: list[str] = []

    with Program(strict=False) as prog:
        if spec["src"] == "timer":
            src = Timer.clone("Tmr")
            with Rung(Enable):
                on_delay(src, spec["preset"], "ms")
        else:
            src = Counter.clone("Ctr")
            Reset = Bool("Reset", external=True)
            with Rung(Enable):
                count_up(src, spec["preset"]).reset(Reset)

        # Non-frozen rung: reads accumulator → makes clock hard
        with Rung(rise(clock_tag), _cmp(src.Acc, spec["form"], spec["threshold"])):
            calc(src.Acc, AccView)

        # Frozen rungs: read only external inputs
        for i, body in enumerate(spec["frozen_bodies"]):
            if body == "calc":
                tag = Int(f"Frozen{i}")
                frozen_tags.append(f"Frozen{i}")
                with Rung(rise(clock_tag)):
                    calc(A + B + i, tag)
            else:
                tag = Bool(f"FBeat{i}")
                frozen_tags.append(f"FBeat{i}")
                with Rung(rise(clock_tag)):
                    out(tag)

    return prog, src.Done, frozenset(frozen_tags)


def _run_frozen(spec: dict, *, fold: bool) -> dict:
    prog, done, _frozen = _build_frozen(spec)
    plc = PLC(prog, dt=spec["dt"])
    plc.patch({"Enable": True, "A": spec["a"], "B": spec["b"]})
    plc.run_until(done, max_cycles=40_000, fold=fold)
    return dict(plc.state.tags)


def test_frozen_write_exclusion_is_bit_equal() -> None:
    """Frozen and non-frozen tags alike must be bit-equal to scan-by-scan."""

    @given(spec=_frozen_write_spec())
    @settings(max_examples=MAX_EXAMPLES, deadline=None)
    @example(
        spec={
            "src": "timer",
            "preset": 5_000,
            "threshold": 100,
            "form": "gt",
            "clock": "clock_1s",
            "dt": 0.010,
            "a": 3,
            "b": 4,
            "n_frozen": 2,
            "frozen_bodies": ["calc", "coil"],
        }
    )
    def inner(spec: dict) -> None:
        folded = _run_frozen(spec, fold=True)
        stepped = _run_frozen(spec, fold=False)
        if folded != stepped:
            diffs = {
                k: (folded.get(k, "<missing>"), stepped.get(k, "<missing>"))
                for k in sorted(set(folded) | set(stepped))
                if folded.get(k) != stepped.get(k)
            }
            note(f"spec={spec}")
            note(f"fold != nofold: {diffs}")
            raise AssertionError(
                f"fold disagreed with scan-by-scan\n  spec={spec}\n  diffs={diffs}"
            )

    inner()


# ---------------------------------------------------------------------------
# Composite readers: the shapes the fold reviews kept finding
# ---------------------------------------------------------------------------

_SOURCES = ["timer_ms", "timer_s", "counter", "counter_pulse_reset", "selfcalc"]
_READERS = [
    "cmp_below",  # a comparison rung after the source
    "cmp_above",  # read before the source moves this scan (flips one scan late)
    "eq",  # flips twice: reaching k and passing it
    "expr",  # (acc // d) == k
    "display",  # calc(acc // d, Disp), nothing reads Disp
    "stored",  # calc(acc // d, Q) then Rung(Q == k)
    "snapshot",  # Rung(rise(X)): copy(acc, Snap) then Rung(Snap > k)
    "operand",  # acc compared against the other source's acc
]


@st.composite
def _composite_spec(draw: st.DrawFn) -> dict:
    n_src = draw(st.integers(min_value=1, max_value=2))
    sources = [draw(st.sampled_from(_SOURCES)) for _ in range(n_src)]
    readers = [
        {
            "src": draw(st.integers(min_value=0, max_value=n_src - 1)),
            "shape": draw(st.sampled_from(_READERS)),
            "k": draw(st.integers(min_value=1, max_value=60)),
            "d": draw(st.sampled_from([1, 7, 100])),
            "form": draw(st.sampled_from(_FORMS)),
            "count": draw(st.booleans()),
        }
        for _ in range(draw(st.integers(min_value=1, max_value=3)))
    ]
    return {
        "sources": sources,
        "readers": readers,
        "gate": draw(st.sampled_from(["none", "clock_500ms", "clock_1s"])),
        "dt": draw(st.sampled_from([0.01, 0.03, 0.1])),
        "seconds": draw(st.sampled_from([3.0, 12.0, 40.0])),
    }


def _build_composite(spec: dict):
    from pyrung import copy

    # Each source's accumulator, made before any rung so a reader can sit above it.
    blocks: list = []
    accs: list = []
    for i, kind in enumerate(spec["sources"]):
        if kind.startswith("timer"):
            blocks.append(Timer.clone(f"T{i}"))
            accs.append(blocks[-1].Acc)
        elif kind.startswith("counter"):
            blocks.append(Counter.clone(f"C{i}"))
            accs.append(blocks[-1].Acc)
        else:
            blocks.append(None)
            accs.append(Int(f"S{i}"))

    with Program(strict=False) as prog:
        for j, r in enumerate(spec["readers"]):
            if r["shape"] == "cmp_above":
                _reader_body(j, r, _cmp(accs[r["src"]], r["form"], r["k"] * r["d"]))
        for i, kind in enumerate(spec["sources"]):
            gate = () if spec["gate"] == "none" or i > 0 else (getattr(system.sys, spec["gate"]),)
            with Rung(*gate):
                if kind.startswith("timer"):
                    unit = "s" if kind == "timer_s" else "ms"
                    on_delay(blocks[i], 30 if unit == "s" else 30_000, unit)
                elif kind.startswith("counter"):
                    pulse = kind == "counter_pulse_reset"
                    count_up(blocks[i], 30_000).reset(
                        rise(system.sys.clock_1s) if pulse else Bool(f"CR{i}")
                    )
                else:
                    calc((accs[i] + 3) % 997, accs[i])
        for j, r in enumerate(spec["readers"]):
            acc = accs[r["src"]]
            other = accs[(r["src"] + 1) % len(accs)]
            shape, k, d = r["shape"], r["k"], r["d"]
            if shape == "cmp_below":
                _reader_body(j, r, _cmp(acc, r["form"], k * d))
            elif shape == "eq":
                _reader_body(j, r, acc == k * d)
            elif shape == "expr":
                _reader_body(j, r, (acc // d) == k)
            elif shape == "display":
                with Rung():
                    calc(acc // d, Int(f"Disp{j}"))
            elif shape == "stored":
                with Rung():
                    calc(acc // d, Int(f"Q{j}"))
                _reader_body(j, r, Int(f"Q{j}") == k)
            elif shape == "snapshot":
                with Rung(rise(Bool(f"X{j}"))):
                    copy(acc, Int(f"Snap{j}"))
                _reader_body(j, r, Int(f"Snap{j}") > k)
            elif shape == "operand":
                _reader_body(j, r, acc > other)
    return prog


def _reader_body(j: int, r: dict, cond) -> None:
    from pyrung import latch

    with Rung(cond):
        if r["count"]:
            calc(Int(f"N{j}") + 1, Int(f"N{j}"))
        else:
            latch(Bool(f"Hit{j}"))


def _run_composite(spec: dict, *, fold: bool) -> tuple[int, dict]:
    plc = PLC(_build_composite(spec), dt=spec["dt"])
    plc.step()
    plc.run_for(spec["seconds"], fold=fold)
    image = dict(plc.state.tags)
    image.update({f"mem:{k}": v for k, v in plc.state.memory.items()})
    return plc.state.scan_id, image


def test_composite_readers_fold_is_bit_equal() -> None:
    """Every reader shape the reviews found must leave stepping's exact state."""

    def spec_of(sources, readers, gate="none", dt=0.01, seconds=12.0):
        return {"sources": sources, "readers": readers, "gate": gate, "dt": dt, "seconds": seconds}

    def reader(shape, *, src=0, k=5, d=100, form="ge", count=True):
        return {"src": src, "shape": shape, "k": k, "d": d, "form": form, "count": count}

    @given(spec=_composite_spec())
    @settings(max_examples=MAX_EXAMPLES, deadline=None)
    # Review probes, always exercised: read-above (a6), slow-timer == (a7),
    # pulse reset (a3), expression / stored copy (gap_dataread), affine self-calc (a1).
    @example(spec=spec_of(["timer_ms"], [reader("cmp_above")]))
    @example(spec=spec_of(["timer_s"], [reader("eq", k=7, d=1)], dt=0.1))
    @example(spec=spec_of(["counter_pulse_reset"], [reader("cmp_below", k=1, d=7)]))
    @example(spec=spec_of(["timer_ms"], [reader("expr", k=1, d=100), reader("stored", k=1, d=100)]))
    @example(spec=spec_of(["selfcalc", "timer_ms"], [reader("operand"), reader("display")]))
    def inner(spec: dict) -> None:
        f_scan, folded = _run_composite(spec, fold=True)
        s_scan, stepped = _run_composite(spec, fold=False)
        if (f_scan, folded) != (s_scan, stepped):
            diffs = {
                k: (folded.get(k, "<missing>"), stepped.get(k, "<missing>"))
                for k in sorted(set(folded) | set(stepped))
                if folded.get(k) != stepped.get(k)
            }
            note(f"spec={spec}")
            raise AssertionError(
                f"fold disagreed with scan-by-scan (scans {f_scan} vs {s_scan})\n"
                f"  spec={spec}\n  diffs={diffs}"
            )

    inner()
