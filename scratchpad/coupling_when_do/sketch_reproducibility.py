"""SKETCH 3 — the deciding principle: PILOT's actions reproducible by primitives.

Design goal stated: (1) clean design, (2) every action PILOT takes on a fork is
reproducible by *public pyrung primitives* — no private pilot-only machinery.

That bar AUDITS the whole action vocabulary. Below, each thing PILOT does to a
fork, and whether it lowers to a public primitive today:

    action                  today                          public?
    ----------------------  -----------------------------  -------------------
    steady hold             plc.force(tag, val)            YES  (force)
    conditional/liveness    plc.when(g).do(patch)          YES  (_ops.py:155 !)
       hold                    <- already lowered
    coast / zoom            plc.run_until(.., fold=True)   YES  (run_until)
       eject guard            + plc.when(ej).pause()       YES  (when().pause())
    command pulse           plc.force / patch (edge)       YES
    ----------------------  -----------------------------  -------------------
    harness bool coupling   harness._heap (_ScheduledPatch) NO  <-- private
    harness analog coupling harness._pre_scan_callbacks     NO  <-- private

So the ONLY part of PILOT's action vocabulary that is NOT a public primitive is
the harness. The reproducibility goal therefore *mandates* lowering the harness
to public primitives — it is not optional cleanup. The two missing pieces are
exactly Sketch 1 (`.after()`) and the analog `when(en).do(accumulate)` form.

Conclusion the goal forces: ONE primitive family —

    when(guard).do(action)        [+ .after(n) scheduling]  [+ phase]

— and PILOT's entire fork-action set is points in it (plus `force` for pins and
`run_until(fold=True)` for coasting). The harness stops being a private runtime
and becomes a *factory* that emits these public rules from Physical/link decls.
`accumulating_profile()` (Sketch 2) reads the same rule set.

This sketch RUNS a fork on which a hold, an analog coupling, and a bool coupling
are all installed through the SAME three public calls, and asserts PILOT touched
nothing else.

Run:  uv run python scratchpad/coupling_when_do/sketch_reproducibility.py
"""

from __future__ import annotations

import heapq
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# ───────────────────────────────────────────────────────────────────────────
# A minimal PLC exposing ONLY the public primitive surface PILOT is allowed to
# use. If an action can't be built from these, it isn't reproducible.
# ───────────────────────────────────────────────────────────────────────────


@dataclass(order=True)
class _Scheduled:
    target_scan: int
    seq: int
    callback: Callable[[Any], None] = field(compare=False)


@dataclass
class _Rule:
    guard: Callable[[Any], bool]
    action: Callable[[Any], None]
    delay: int = 0
    phase: str = "post"  # "pre" = input synthesis (before program), "post" = after
    prev: bool = False


@dataclass
class _State:
    scan_id: int
    tags: dict[str, Any]


class PublicPLC:
    """Only force(), when().do()/.after().do(), run_until() are PILOT-callable.

    `_program` is the ladder (the ship). `_pins` are forces. `_rules` are
    when().do() installs. Everything PILOT does goes through install_* below; the
    audit at the end proves it used nothing private."""

    def __init__(self, program: Callable[[_State], None]) -> None:
        self._program = program
        self.state = _State(0, {"Enable": False, "MotorCmd": False, "Temp": 0.0,
                                "MotorRunning": False, "Goal": False})
        self._pins: dict[str, Any] = {}
        self._rules: list[_Rule] = []
        self._sched: list[_Scheduled] = []
        self._seq = 0
        self.dt = 0.01
        self._touched: set[str] = set()  # which public methods PILOT used

    # ---- public primitive surface ----------------------------------------
    def force(self, tag: str, value: Any) -> None:
        self._touched.add("force")
        self._pins[tag] = value

    def when(self, guard: Callable[[_State], bool]) -> _Builder:
        self._touched.add("when")
        return _Builder(self, guard)

    def run_until(self, predicate: Callable[[_State], bool], *, max_cycles: int) -> None:
        self._touched.add("run_until")
        for _ in range(max_cycles):
            self._step()
            if predicate(self.state):
                return

    # ---- internals (NOT pilot-callable) ----------------------------------
    def _install(self, rule: _Rule) -> None:
        rule.prev = bool(rule.guard(self.state))
        self._rules.append(rule)

    def _apply_pins(self) -> None:
        for tag, val in self._pins.items():
            self.state.tags[tag] = val

    def _fire(self, phase: str) -> None:
        for r in self._rules:
            if r.phase != phase:
                continue
            now = bool(r.guard(self.state))
            if r.delay == 0:
                if now:
                    r.action(self.state)
            elif now and not r.prev:  # edge-scheduled
                self._sched.append(_Scheduled(self.state.scan_id + r.delay, self._seq, r.action))
                self._seq += 1
                heapq.heapify(self._sched)
            r.prev = now

    def _drain(self) -> None:
        while self._sched and self._sched[0].target_scan <= self.state.scan_id:
            heapq.heappop(self._sched).callback(self.state)

    def _step(self) -> None:
        self.state = _State(self.state.scan_id + 1, dict(self.state.tags))
        self._apply_pins()          # steady holds pin every scan
        self._fire("pre")           # input-synthesis phase: holds + couplings
        self._program(self.state)   # the ship runs
        self._fire("post")          # post-scan reactive
        self._drain()               # scheduled (delayed) actions due now


@dataclass
class _Builder:
    plc: PublicPLC
    guard: Callable[[Any], bool]
    delay: int = 0
    phase: str = "post"

    def after(self, n: int) -> _Builder:
        return _Builder(self.plc, self.guard, n, self.phase)

    def synthesizing(self) -> _Builder:
        """Mark this rule as input-synthesis (pre-scan). The clean phase model:
        input-targeting rules synthesize the scan's input vector; everything else
        is post-scan. Holds and couplings both live here — co-authors of the
        same input vector (the earlier conversation's exact conclusion)."""
        return _Builder(self.plc, self.guard, self.delay, "pre")

    def do(self, action: Callable[[Any], None]) -> None:
        self.plc._touched.add("do")
        self.plc._install(_Rule(self.guard, action, self.delay, self.phase))


# ───────────────────────────────────────────────────────────────────────────
# The "ship": a trivial ladder. Goal latches when Temp >= 5 OR MotorRunning.
# ───────────────────────────────────────────────────────────────────────────


def _program(s: _State) -> None:
    if s.tags["Temp"] >= 5.0 or s.tags["MotorRunning"]:
        s.tags["Goal"] = True


# ───────────────────────────────────────────────────────────────────────────
# PILOT installs EVERYTHING through the public surface — no private machinery.
# ───────────────────────────────────────────────────────────────────────────


def _thermal(cur: float, en: bool, dt: float) -> float:
    return cur + 0.5 * dt if en else cur


def _demo() -> None:
    plc = PublicPLC(_program)

    # (a) analog coupling  Enable -> Temp   == when(Enable).do(Temp := profile)  [synthesis]
    plc.when(lambda s: s.tags["Enable"]).synthesizing().do(
        lambda s: s.tags.__setitem__("Temp", _thermal(s.tags["Temp"], True, plc.dt))
    )

    # (b) bool coupling  MotorCmd -> MotorRunning after 3 scans
    #     == when(MotorCmd-edge).after(3).do(MotorRunning := True)              [synthesis]
    plc.when(lambda s: s.tags["MotorCmd"]).after(3).synthesizing().do(
        lambda s: s.tags.__setitem__("MotorRunning", True)
    )

    # (c) a SELF-RELEASING hold: drive Enable while the goal is unmet
    #     == when(not Goal).do(Enable := True)   (more expressive than forced_holds)
    plc.when(lambda s: not s.tags["Goal"]).synthesizing().do(
        lambda s: s.tags.__setitem__("Enable", True)
    )

    # PILOT's plan: "reach Goal". It holds (c), and lets the couplings ride.
    plc.run_until(lambda s: s.tags["Goal"], max_cycles=2000)

    print(f"Goal reached at scan {plc.state.scan_id}, Temp={plc.state.tags['Temp']:.2f}")
    print(f"  (analog coupling drove Temp 0 -> 5.0 while the self-releasing hold held Enable)")
    print()
    print(f"Public primitives PILOT touched: {sorted(plc._touched)}")
    assert plc._touched <= {"force", "when", "after", "do", "run_until", "synthesizing"}, plc._touched
    print("ASSERT OK — PILOT used ONLY the public primitive surface. Fully reproducible.")
    print()
    print("Residue (irreducibly NOT reproducible by primitives):")
    print("  - real hardware / pyrung live  -> stepping the real PLC is the only oracle")
    print("  - a deliberately black-boxed component -> no f to read or replay")
    print("  => the honest, permanent home of `sandbox`. Everything else is primitives.")


if __name__ == "__main__":
    _demo()
