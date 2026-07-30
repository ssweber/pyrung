# BinACounter.Done frontier — is it "just let-run vs rise/pulse"?

Investigation prompted by the xfail in `tests/core/analysis/test_pilot_examples.py::test_counter_done_reachable`,
whose reason reads:

> "pilot: BinACounter.Done requires the accumulator to reach preset (10) via ten
>  edge-triggered rise(BinASensor) pulses. PILOT does not yet emit a repeated-pulse
>  plan to drive a counter to its target — the count-to-preset frontier."

Question: isn't this the same distinction we already make for Timer.Acc — does the rung
keep the counter enabled (let-run) or is it `rise()` (must pulse)?

**Short answer: yes, that dichotomy is the right lever — but there's a recognition step
missing *before* the lever even gets a chance to fire.** Today trace treats an
accumulator's *Done bit* like a plain OTE coil, so PILOT never realizes a counter is
involved at all.

## Probe evidence (read-only, `scratchpad`-level)

Example: `examples/learn/counters.py`
```python
with rung(rise(BinASensor)):
    count_up(BinACounter, preset=10).reset(CountReset)
```

### The accumulator infra already sees it correctly
- `accumulators.resolve_profile("BinACounter_Done")` → `AccumulatorMatch(via_done=True)`,
  `advance = RisingEdgeCondition`, `preset=10`, `direction=+1`, `rate_per_scan=1`.
- `trace._progress_kinds(logic)` → `{'BinACounter_Acc': 'count_up', ...}` — **keyed on the
  Acc register, not the Done bit.**
- `scans_to_eject` would analytically return 10 ( (10−0)/1 ).
- `compute_edge_tags` → `{BinASensor, BinBSensor}` — so the edge-ness is already known.

### But trace mis-models the Done-bit target
`trace_back("BinACounter_Done", True, …)` produces:
```
- BinACounter_Done=True  sat=False self_adv=False   (writer rung found)
  - BinASensor=True       sat=False steer=True self_adv=False
```
i.e. it walks the counter's *rung condition* into a single steerable leaf, exactly as if
Done were `out(Done)` gated by `rise(BinASensor)`. All accumulator semantics ("Done needs
Acc to cross preset = 10 crossings") are lost. PILOT pulses BinASensor once (Acc 0→1),
Done is still False, no further bearing → **unreachable**.

### This is why even a LEVEL-gated counter's Done is unreachable today
A scratch program with a plain level gate (no `rise()`):
```python
with rung(RunCount):
    count_up(LevelCounter, preset=10).reset(Rst)
```
`trace_back("LevelCounter_Done", True)` →
```
- LevelCounter_Done=True  self_adv=False
  - RunCount=True          steer=True self_adv=False
```
`pilot_how(..., LevelCounter.Done)` → **REACHABLE: False**. The let-run/bearing coast never engages
because the frontier was never classified as self-advancing — proving the gap is *not*
specific to edges.

## Contrast: the `Acc > N` *threshold* form already works
`trace.py:906-921` — when the target Atom is a comparison (`lt/le/gt/ge`) and
`expr.tag (the Acc register) in _progress_kinds`, trace emits a `self_advancing=True` coast
leaf. That is the path timers/counters already ride for `Acc > N`. The **Done bit** never
reaches this branch — it's a Bool equality, traced through the writer rung.

## So the fix is two layers, and the user's dichotomy is layer (B)

**(A) Recognition (the missing precondition).** When tracing a Bool target whose
`resolve_profile(tag).via_done` matches an accumulator, surface a self-advancing
accumulator frontier (with its advance *driver* as a held/pulsed prerequisite) instead of
naively walking the rung condition into a one-shot steerable leaf. This mirrors the
existing `Acc > N` branch and the analog `_coupling_driver_leaf` pattern (which already
surfaces an Enable hold as a *sibling* of an analog coast leaf).

**(B) Lever selection by advance shape (the user's point — already correct).** Once
recognized, the `advance` condition shape — already on the profile — picks the lever:
- **level advance** (`BitCondition`) → hold it True and **let-run/bearing coast** coasts Acc to
  preset (counter increments every held scan, identical to Timer.Acc). `scans_to_eject`
  gives the dwell length.
- **edge advance** (`RisingEdgeCondition`/fall) → holding does nothing past scan 1; emit a
  **pulse train** of N = `scans_to_eject` toggles (= 10). The single-edge pulse already
  exists (`steer._pulse_actions` / `_ops._apply_pulse`, the `needs_edge` release→step→
  patch→step dance); the only new mechanic is repeating it N times.

## Bottom line
The xfail reason frames this as a brand-new "repeated-pulse planner." In reality:
- the **count** is already computed (`scans_to_eject`, analytic = 10),
- the **edge-vs-level test** is already on the profile (`advance` type),
- the **single edge pulse** is already emitted (`needs_edge`),
- the genuine gaps are (A) *recognizing the Done bit as an accumulator frontier* (the same
  classification `Acc > N` already gets) and (B) a pulse-train emit mode for edge advances
  (hold, for level). The user's "let-run vs rise→pulse" is exactly (B); (A) is the
  upstream precondition that makes (B) reachable — and is what's actually blocking even the
  level case today.

(Probes used: scratchpad/throwaway scripts; no core code touched.)
