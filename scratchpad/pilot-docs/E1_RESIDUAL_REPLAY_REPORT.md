# E1 residual replay measurement

Measured on 2026-07-28 with:

```text
uv run python -u devtools/profile_pilot_replay.py --write
```

The machine-readable result is
[`E1_RESIDUAL_REPLAY_DATA.json`](E1_RESIDUAL_REPLAY_DATA.json). The profiler
retains aggregate counters and exact call intervals only; it does not add a
per-scan replay log or expand `CoastReceipt`.

## Result

The avoided-Complete route remained semantically stable:

- target: `Sts_State_Completed=True`;
- excluded action: `Cmd_State_Complete`;
- reached at scan 631,797;
- route time: 97.878 seconds, excluding feasibility-shadow work;
- decision skeleton: 184 events,
  SHA-256 `06f07354d42ff4329139ae7ce367de3d0bf0273fe74556ac3a6756a188fa35f4`.

E3 is not yet feasible on this evidence. A representative replay coast under
the same recorded executable-overlay fingerprint showed a warm backend speedup,
but failed endpoint parity. Consequently:

- backend-supported replay residual scans: 1,999;
- parity-qualified compiled residual scans: **0**;
- defensible modeled savings ceiling: **not established**.

This is stronger than a disappointing benchmark. It says the next problem is
the exact advancement/World contract in E2, not kernel throughput.

## Work partition

The current executor divided the route as follows:

| Coast kind | Logical scans | Ordinary-folded | Cycle-folded | Residual |
| --- | ---: | ---: | ---: | ---: |
| investigation replay | 4,699 | 0 | 2,700 | 1,999 |
| live bearing coast | 665,375 | 0 | 658,900 | 6,475 |
| departure settlement | 812 | 0 | 0 | 812 |

Cycle folding removed 57.5% of investigation replay scans. The remaining 1,999
interpreted scans are the work a residual backend could potentially address,
subject to parity. The route also executed 92 interpreted witness scans.

Existing causal-slab replay already used the compiled backend for 2,210 actual
scans. Those are measured current behavior, not projected E3 savings.

## Replay and reuse

- replay-capture requests: 226;
- capture hits / misses: 134 / 92;
- causal slab refills: 3;
- slab states materialized: 2,210;
- candidate-history calls: 6;
- unique overlay-fingerprint/interval/candidate shapes: 5;
- repeated exact shapes: 1;
- candidate-history time: 3.361 seconds.

All six candidate-history calls had one recorded executable-overlay fingerprint
and the same 570-candidate set. Their intervals were 45, 45, 1, 1,031, 1,731,
and 24 scans. The repeated 45-scan call is real reuse pressure, but the longer
historical intervals must not be treated as one stable executable World merely
because their endpoint runner has one current overlay.

## Timing

| Boundary | Calls | Seconds |
| --- | ---: | ---: |
| cold compiled-kernel creation | 3 | 0.881 |
| current warm compiled backend | 2,210 scans | 2.683 |
| observation handoff | 92 | 0.014 |
| interpreted witness execution | 92 | 0.469 |
| replay-capture envelope | 226 | 1.168 |
| candidate-history intervals | 6 | 3.361 |
| complete route | 1 | 97.878 |

The boundary timings are nested and non-additive. For example, replay-capture
time includes observation handoff and witness work, while route time contains
all instrumented baseline activity. The route figure is instrumented wall time
with feasibility-shadow time subtracted; it is not an uninstrumented benchmark.

The cold figure is intentionally separate. It is the aggregate of the three
lazy kernel creations observed on the route, not a warm execution cost.

## Feasibility shadow

The profiler shadows a replay coast immediately after it finishes, before that
runner's synthesis overlay can change. The overlay fingerprint records program,
synthesis rungs, and runner options; it is not proof of a complete exact World.
Endpoint parity is the qualification gate. A passing shadow would qualify only
the same fingerprint and exact replay-coast interval, never every
backend-supported interval. Shadow calls are excluded from baseline counters
and route time, and any kernel cache created by the shadow is restored afterward.

The first representative bounded interval was:

- recorded executable-overlay fingerprint: `26c67786803b5a8d`;
- scans 112 through 152;
- logical / residual scans: 40 / 40;
- interpreted: 0.089602 seconds;
- warm compiled: 0.071043 seconds;
- raw speed ratio: 1.261x;
- endpoint parity: **failed**.

The endpoint had the same scan id and timestamp, but differed on 507 tags and
one edge-memory entry (`_prev:A_Alm14_DoorOpen_Trig`). The tag sample includes
alarm state and historian values, so this cannot be dismissed as timing noise
or an unobserved private scalar.

The raw 18.6ms saving is therefore ineligible. Applying its speed ratio to all
1,999 residual scans would manufacture a performance promise from a different
machine state. E1 deliberately reports no modeled ceiling instead.

## Implication for E2 and E3

E2 must define advancement from one exact executable World and preserve the
observation boundary explicitly. In particular:

- coast/fold proof owns which logical scans may be skipped;
- the backend owns only state transition application;
- the receipt distinguishes ordinary-folded, cycle-folded, residual, and
  endpoint work;
- causal consumers remain independent of repeating-history encodings through
  their public query APIs;
- a history interval cannot inherit the runner's current synthesis overlay by
  implication.

Only after that contract produces endpoint parity should E3 use the 1,999
backend-supported residual scans as a possible savings population. Qualification
remains interval-local until further shadows cover further advancement
identities. Re-run this profiler after E2; a positive ceiling requires a
representative parity-qualified shadow.
