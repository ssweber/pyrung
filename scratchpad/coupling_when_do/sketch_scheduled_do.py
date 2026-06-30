"""SKETCH 1 — the *executor* primitive: a scheduled / delayed ``do``.

The thread's claim: a bool link is "``when(En-edge).do(flip Fb).after(N)``" — the
harness already implements this privately as the ``_heap`` of ``_ScheduledPatch``
(harness.py). There is no *declarative* surface for it: ``when(p).do(cb)`` fires
post-scan, immediately (runner._evaluate_breakpoints), with no delay slot.

This sketch lifts the heap into a first-class runner primitive so the bool
coupling becomes a *consumer* of it instead of carrying its own scheduler.

It is deliberately self-contained (a ~30-line FakeRunner) so it RUNS and lets us
feel the three semantic decisions that actually matter:

    (D1) edge vs level trigger   — fire once per rising edge, not every held scan
    (D2) cancel vs fire-and-forget — bool link does NOT cancel (transport delay)
    (D3) fold bounding           — expose nearest-due scan, like _harness_nearest_scan

None of this touches reading (Sketch 2). This is purely "how the fork runs it".
"""

from __future__ import annotations

import heapq
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# ───────────────────────────────────────────────────────────────────────────
# The primitive: a delayed action scheduled off a predicate's rising edge.
# ───────────────────────────────────────────────────────────────────────────


@dataclass(order=True)
class _ScheduledAction:
    """One pending action — mirrors harness._ScheduledPatch but the payload is a
    callback, not a (tag, value). That single generalization is the whole move:
    the heap stops being about *patching feedback* and becomes about *running a
    deferred effect*, of which "patch Fb := True" is one instance."""

    target_scan: int
    seq: int
    callback: Callable[[Any], None] = field(compare=False)


@dataclass
class _DelayedRule:
    """A registered ``when(predicate).after(delay).do(callback)``.

    ``prev`` carries the predicate's truth last scan so we fire on the *rising*
    edge (D1). Without this, a predicate that stays True would schedule a fresh
    action every scan — a cascade, not a debounced edge. The harness gets edge
    semantics for free because it hangs off ``monitor(en)`` (inherently a change
    callback); a state-*predicate* ``when()`` has to track the edge itself."""

    predicate: Callable[[Any], bool]
    delay_scans: int
    callback: Callable[[Any], None]
    prev: bool = False
    # D2: no `cancel_predicate` field. A bool link is fire-and-forget transport
    # delay — once the edge schedules the flip, dropping En does NOT unschedule
    # it (verified against harness: _make_en_callback never scans the heap to
    # remove pending entries). A `.until(cond)` / dwell variant WOULD add a
    # cancel hook here — that is the on-delay-timer (TON) cousin, a *different*
    # primitive. Keeping it out is what makes this one match the bool link.


class SchedulerMixin:
    """The slice of PLC this primitive would add. Real code: fold these fields
    into PLC.__init__ and the drain into the post-commit path next to
    _evaluate_breakpoints."""

    def __init__(self) -> None:
        self._delayed_rules: list[_DelayedRule] = []
        self._scheduled: list[_ScheduledAction] = []
        self._sched_seq = 0

    # ---- registration surface --------------------------------------------
    # Fluent shape recommendation:  when(pred).after(n).do(cb)
    # `.after(n)` returns a builder whose `.do()` registers a DELAYED action,
    # parallel to `.pause()/.snapshot()/.do()` hanging off the plain builder.
    # (The alternative `when(p).do(cb).after(n)` can't chain cleanly — `.do()`
    # already returns a handle, not a builder.)

    def schedule_rule(
        self, predicate: Callable[[Any], bool], delay_scans: int, callback: Callable[[Any], None]
    ) -> _DelayedRule:
        rule = _DelayedRule(predicate, delay_scans, callback)
        rule.prev = bool(predicate(self.state))  # seed: an already-True pred is not a fresh edge
        self._delayed_rules.append(rule)
        return rule

    # ---- per-scan hooks (called by the run loop) -------------------------

    def _fire_delayed_rules(self) -> None:
        """Post-commit: schedule actions for rules whose predicate just rose."""
        for rule in self._delayed_rules:
            now = bool(rule.predicate(self.state))
            if now and not rule.prev:  # D1: rising edge only
                self._scheduled.append(
                    _ScheduledAction(self.state.scan_id + rule.delay_scans, self._sched_seq, rule.callback)
                )
                self._sched_seq += 1
                heapq.heapify(self._scheduled)
            rule.prev = now

    def _drain_scheduled(self) -> None:
        """Post-commit: run actions whose target scan has arrived."""
        while self._scheduled and self._scheduled[0].target_scan <= self.state.scan_id:
            action = heapq.heappop(self._scheduled)
            action.callback(self.state)

    def delayed_actions_nearest_scan(self) -> int | None:
        """D3: the fold bounds itself to this, exactly as fold.py's
        ``_harness_nearest_scan`` peeks ``harness._heap[0].target_scan``. A
        scheduled action is a future visible change; the fold must not skip past
        it. Because the heap is peekable, fold integration is free."""
        return self._scheduled[0].target_scan if self._scheduled else None


# ───────────────────────────────────────────────────────────────────────────
# A tiny runner so the sketch actually executes and the decisions are visible.
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class _State:
    scan_id: int
    tags: dict[str, Any]


class FakeRunner(SchedulerMixin):
    def __init__(self) -> None:
        super().__init__()
        self.state = _State(scan_id=0, tags={"En": False, "Fb": False})

    def set(self, **tags: Any) -> None:
        self.state.tags.update(tags)

    def step(self) -> None:
        # commit one scan, then run the post-commit hooks (order matches a real
        # post-scan: evaluate breakpoints / fire rules, then drain due effects).
        self.state = _State(self.state.scan_id + 1, dict(self.state.tags))
        self._fire_delayed_rules()
        self._drain_scheduled()


def _demo() -> None:
    plc = FakeRunner()

    # The bool coupling, expressed declaratively:  En rising → Fb := True after 3 scans.
    plc.schedule_rule(
        predicate=lambda s: bool(s.tags["En"]),
        delay_scans=3,
        callback=lambda s: s.tags.__setitem__("Fb", True),
    )

    print("scan  En     Fb     nearest_due")
    print(f"{plc.state.scan_id:>3}   {plc.state.tags['En']!s:<6} {plc.state.tags['Fb']!s:<6} -")

    plc.set(En=True)          # rising edge at scan 1 → schedule Fb@scan 4
    for target_held in (True, False, False, False, False):
        plc.set(En=target_held)
        plc.step()
        print(
            f"{plc.state.scan_id:>3}   {plc.state.tags['En']!s:<6} "
            f"{plc.state.tags['Fb']!s:<6} {plc.delayed_actions_nearest_scan()}"
        )

    # NOTE what just happened, and which decision each line proves:
    #  - Fb flips True exactly 3 scans after the En edge (D1 + the delay).
    #  - En was dropped at scan 2, well before the flip — and Fb STILL fired
    #    (D2: no cancel = transport delay = the bool link's actual semantics).
    #    A dwell/TON variant would have cancelled here and Fb would stay False.
    #  - nearest_due showed scan 4 while pending, then None (D3: fold bound).


if __name__ == "__main__":
    _demo()
