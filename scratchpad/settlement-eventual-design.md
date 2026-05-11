You're advising on a design question for `prove()`, an exhaustive state-space verifier for ladder logic programs (PLC automation). The verifier runs BFS over all reachable states and checks safety properties (invariants that must hold in every reachable state).

## Background

Ladder logic programs use timers and counters that accumulate over many scans before firing. A common safety pattern is:

```python
with Rung(Cmd, ~Feedback):
    on_delay(FaultTimer, 3000)      # start a 3-second timer
with Rung(FaultTimer.Done):
    latch(Alarm)                    # latch alarm when timer fires
```

The property to verify: `prove(logic, Or(~Cmd, Feedback, Alarm))` — "whenever Cmd is True and Feedback is False, Alarm must be True."

The problem: on scan 1 (Cmd=True, Feedback=False), the timer just started. Alarm is False. This is a genuine reachable state that violates the property. But the *intent* is that the alarm will *eventually* fire — the transient period is expected.

## What existed before

The BFS had a "settlement" optimization: when a post-step state has pending timer/counter events, it fast-forwards to when those events fire and checks the settled state instead. This made `prove(logic, Or(~Cmd, Feedback, Alarm))` return `Proven` for the pattern above.

The base (pre-settlement) post-step state was only checked for predicate violations when `_has_active_oneshot_memory(kernel)` returned True — a proxy that happened to correlate with "the timer hasn't fired yet" because `latch()` (a one-shot instruction) only executes after the timer fires.

## The soundness bug we fixed

A fuzzer found a program where `count_down(C0, 5).reset(B0)` caused settlement to diverge: the `.reset(B0)` modifier resets the counter when B0 is True, so the settlement fast-forward was undone by the reset, cycling back to the initial state. The base post-step state had B0=True (a real violation), but settlement returned B0=False. Since there was no one-shot instruction, the base state was never checked. `prove()` returned `Proven` incorrectly.

The fix: always check the base post-step state for predicate violations in the settlement slow path. The `_has_active_oneshot_memory` guard was removed entirely.

## Current state

The fix is correct and all 3700+ tests pass. Five timer-gated alarm tests are now `xfail` — they relied on the implicit "settle before evaluate" behavior. The properties they test ARE violated in genuinely reachable states, but the violations are transient (they resolve once the timer fires).

## The design question

How should `prove()` handle properties that are only true *after* timers/counters settle? Options to consider:

1. **Input-side: explicit eventual modifier on the property.** Something like `prove(logic, Or(~Cmd, Fb, Alarm), settle=True)` or a wrapper `eventually(condition)`. The user opts in to "check after settlement" semantics for specific properties.

2. **Reporting-side: surface transient violations differently.** `prove()` always checks the base state, but returns a richer result — e.g. `TransientCounterexample` vs `Counterexample`, or a caveat/annotation on the trace indicating "this violation resolves after N scans of settlement."

3. **Property-side: require users to write timer-aware properties.** Instead of `Or(~Cmd, Fb, Alarm)`, write `Or(~Cmd, Fb, Alarm, FaultTimer.Acc > 0)` — explicitly allowing the transient period. No special `prove()` machinery needed.

4. **Hybrid: settlement still runs for state-space exploration, but predicates are always checked on base states.** Settlement remains an optimization for *reachability* (skipping timer accumulation scans) but not for *predicate evaluation*. Users who want "eventual" semantics use option 1 or 3.

Consider: this is for PLC safety verification. Users are automation engineers, not formal methods experts. Timer-gated alarms are extremely common. The API should make the common case easy without sacrificing soundness.

The settlement optimization itself is valuable for state-space reduction (timers with 3000ms presets at 10ms dt = 300 scans that would otherwise explode the BFS). The question is purely about when predicates are evaluated relative to settlement.
