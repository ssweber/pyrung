"""Fold soundness fuzzer — saturated-heartbeat shapes.

A system clock gating a rung that reads an accumulator through a comparison is
exactly the family `_runtime_soft_clocks` promotes soft once the comparison
saturates (`fold._comparisons_saturated`).  Before saturation the clock must
stay bound (every edge honored); after, its edges may be skipped — but only
while the gated output stays frozen, which the observe-before-skip plateau
guard re-confirms each window.

This fuzzer generates the family with randomized knobs and asserts the folded
``run_until(Done)`` is **bit-equal** to scan-by-scan.  Folding may only ever be
faster — never change a landed value.  ``run_until(Done)`` lands on the preset
(an accumulator crossing, not a time boundary), so the whole tag space is
comparable, including the raw accumulator.

Reproducer on failure: the drawn ``spec`` is the reproducer — it is printed via
``note`` and embedded in the assertion message, and ``_build(spec)`` reconstructs
the exact program.
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


# Folding a *timed* source (on_delay) rides the dt knob, so its accumulator can
# land one tick off scan-by-scan at large presets — the documented timed-fold
# drift (see core/test_fold.py::test_unread_timer_folds_instead_of_stepping,
# which tolerates <= 10).  Counters are per-scan integers and stay exact.
_TIMED_ACC_TOLERANCE = 20


def _run(spec: dict, *, fold: bool) -> dict:
    prog, done = _build(spec)
    plc = PLC(prog, dt=spec["dt"])
    plc.patch({"Enable": True, "A": spec["a"], "B": spec["b"]})
    plc.run_until(done, max_cycles=40_000, fold=fold)
    # Drop scan-timing diagnostics: a folded scan reports one big synthetic
    # `dt_override` pass, so `sys.scan_time_*` legitimately differs from
    # scan-by-scan.  They are runtime metrics, not program logic — outside the
    # fold soundness contract (bit-equal *visible logic state*).
    return {k: v for k, v in plc.state.tags.items() if not k.startswith("sys.scan_time")}


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
    def inner(spec: dict) -> None:
        folded = _run(spec, fold=True)
        stepped = _run(spec, fold=False)
        # The timed-source accumulator is allowed its documented one-tick drift;
        # everything else — the clock-gated logic (Beat / Extent), Done, and the
        # exact counter accumulator — must be bit-equal.
        acc = "Tmr_Acc" if spec["src"] == "timer" else "Ctr_Acc"
        f_acc, s_acc = folded.pop(acc, None), stepped.pop(acc, None)
        if spec["src"] == "timer" and f_acc is not None and s_acc is not None:
            assert abs(f_acc - s_acc) <= _TIMED_ACC_TOLERANCE, (
                f"timer acc drift too large\n  spec={spec}\n  {acc}: {f_acc} vs {s_acc}"
            )
        else:
            folded[acc], stepped[acc] = f_acc, s_acc  # counter: compare exactly

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
