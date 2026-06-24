# Pilot loop: extract layers into named functions

`_pilot_loop` in `src/pyrung/core/analysis/pilot/pilot.py` is a ~700-line
monolith.  Each acceptance layer is inlined inside a deeply nested
for/if chain.  The widening block (lines 1051-1221) duplicates most of
the single-candidate gating logic.

## Current execution order vs numbering

Docstring numbers the layers 0-3 but code executes 0+2, 1, 3:

| Execution order | Docstring | Name            | Cost           |
|-----------------|-----------|-----------------|----------------|
| 1st             | L0 + L2   | Spin + Hallucinate | key compare (cheap) |
| 2nd             | L1        | Cycle           | set lookup (cheap) |
| 3rd             | L3        | Dead-End        | `trace_back()` (expensive) |
| post-commit     | L4 + L5   | Wander + Repeat | trend + cause chase |

L2-Hallucinate is a subcase of L0-Spin (key matched, but changed then
reverted), so they share the same `if new_key == key` branch.  This
execution order is correct — dead-end is last because it's the most
expensive gate.

## Proposed extractions

Drop layer numbers from function names.  Keep L0-L5 taxonomy in the
module docstring for architecture reference only.

### Per-candidate gates (return accept/reject/continue)

```
_gate_spin(new_key, key, post_pulse_key, pending, ...) -> GateResult
```
Key-change check.  On match: checks post-pulse key for excursion
(L2 subcase).  On excursion: diagnoses reverted dims, derives holds,
attempts retry.  Returns REJECT (spin/no-holds), RETRY_OK (excursion
recovered), or PASS.

```
_gate_cycle(new_key, seen_keys, pending, inf_prescribed) -> GateResult
```
Visited-key check.  L6 influence-prescribed steps can override.
Returns REJECT or PASS.

```
_gate_dead_end(fork, fork_snap, target_tag, target_value, ...) -> GateResult
```
Calls `trace_back()` on the fork state.  Checks: empty frontier with
no pending effects = pocket.  Also checks lateral moves (no new
frontier, no trend improvement).  L6 influence-prescribed steps can
override both.  Returns REJECT or PASS with the new tree/trend.

### Post-commit monitors

```
_monitor_trend(new_trend, best_trend, checkpoints, ...) -> MonitorResult
```
L4: checkpoint on improvement.  L5: on regression, chase cause roots
on watch tags, install holds, revert to checkpoint.  Returns updated
best_trend and optionally a reverted work fork.

### Candidate construction

```
_build_candidates(tree, snap, key, ...) -> CandidateList
```
Trace actions (nogood-filtered) + L6 influence candidates + upstream
cone candidates, with blast-radius filter.  Returns ordered list plus
the L6 path/tag metadata.

### Composite trial functions

```
_try_candidate(candidate, work, snap, key, ...) -> TrialResult
```
Fork -> pulse -> settle -> run gates in order (spin, cycle, dead-end)
-> commit or reject.  Records L6 observations.  Returns accepted fork
or None.

```
_try_widening(active_trace_actions, work, snap, key, ...) -> TrialResult
```
Progressive widening (width 2+).  Reuses the same gate functions
instead of duplicating the gating logic inline.

## Renumbered layer docstring

After extraction, update the module docstring to match execution order:

```
Layers 0-2 gate each candidate action:
  0. Don't Spin      — state key must change
     0a. Excursion   — key changed then reverted; derive holds, retry
  1. Don't Cycle     — new key must not have been visited
  2. Don't Dead-End  — frontier must be non-empty or async pending

Layers 3-4 monitor the committed sequence:
  3. Don't Wander    — checkpoint on trend improvement
  4. Don't Regress   — cause-chain recovery on trend regression

Layer 5 (influence mapping):
  5. Don't Rediscover — observed transitions become known topology
```

## Out of scope

- No behavioral changes — pure refactor, all tests must pass as-is.
- No new layers or gate logic.
- L6 influence mapping stays in `influence.py`; only the inline
  candidate-gen and observation-recording move into named helpers.
