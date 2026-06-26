# PILOT Loop: Direction → Action → Judgment → Memory Refactor

## Context

The PILOT loop architecture review identified that four concerns (Direction, Action, Judgment, Memory) bleed into each other. Gates in `verify.py` mutate `state.nogoods` and install holds — Judgment doing Memory's job. Candidate value proposal functions live in `steer.py` — Action doing Direction's job. `AUTO_EDGE` is dead code in the outcome contract. The pilot never names *why* it's stuck.

**Goal:** Make data flow unidirectional (Direction → Action → Judgment → Memory), remove dead code, add stuck diagnostics. All changes are behavior-preserving refactoring except the additive stuck event. Compass observation recording in steer.py is deferred — it's interleaved with execution and the cost/complexity of extracting it outweighs the architectural gain right now.

## Steps (each independently committable)

### Step 1: Remove `AUTO_EDGE` dead code

`classify_outcome` never returns `Outcome.AUTO_EDGE`. The enum member and its sole reference are dead code. The distinction it would capture ("the PLC moved it, not me") is already handled at finer granularity: compass observation recording (steer.py:307-327) does per-tag causal attribution via `_action_caused_change`, recording PLC-caused transitions under `WAIT` and pilot-caused transitions under the action. AUTO_EDGE at the outcome level would be a lossy summary of what the compass already records correctly.

**outcome.py:** Delete `AUTO_EDGE = "auto_edge"` from `Outcome` enum. Update module docstring from five outcomes to four (remove line 8, renumber).

**progress.py:109-112:** Change `trial.outcome in {Outcome.CONFIRMED, Outcome.AUTO_EDGE}` → `trial.outcome == Outcome.CONFIRMED`.

**CLAUDE.md:** Update the loop section (lines 48-49) and module map (line 152) — four outcomes, not five.

### Step 2: Move `candidate_values_for_tag` and `upstream_candidates` from steer.py to candidates.py

These are Direction concerns ("what values to try"), not Act. candidates.py already imports them from steer.py.

**candidates.py:** Remove the import from steer (line 16). Paste the two function bodies (steer.py:51-75, 78-110) before `_compass_actions_for`. Add `ProgramGraph` to the TYPE_CHECKING imports.

**steer.py:** Delete both function definitions and the "Candidate value proposals" section header. Keep the `_values_match` import (used elsewhere in steer.py).

**tests/core/analysis/test_pilot.py:788,810:** Update import path to `from pyrung.core.analysis.pilot.candidates import upstream_candidates`.

### Step 3: Verify gates return nogoods instead of mutating state

The five `state.nogoods.setdefault(...)` calls in verify.py become data on the return value.

**types.py:** Add `nogood_pairs: frozenset[_ActionPair] = frozenset()` field to `_AttemptResult`.

**verify.py:**
- Add `collected_nogoods: list[_ActionPair]` parameter to `_gate_spin`, `_gate_cycle`, `_gate_dead_end`.
- In each gate, replace `state.nogoods.setdefault(frame.key, set()).add(nogood_pair)` with `collected_nogoods.append(nogood_pair)`.
- In `verify_gates`, create `collected_nogoods: list[_ActionPair] = []`, pass it to each gate, and attach `nogood_pairs=frozenset(collected_nogoods)` to every returned `_AttemptResult`.
- In `verify_gates` BAD_EDGE path (line 431): same — append to `collected_nogoods` instead of mutating state.

**steer.py `_try_widening`:** Accumulate nogoods from rejected sub-attempts and attach them to the final `_AttemptResult`.

**pilot.py:** After each `attempt = _try_*(...)` call (4 sites: zoom, candidate, widening, terminal letrun), apply:
```python
if attempt.nogood_pairs:
    state.nogoods.setdefault(frame.key, set()).update(attempt.nogood_pairs)
```

**Ordering safety confirmed:** No downstream gate within a single `verify_gates` call reads `state.nogoods` after an upstream gate writes it.

### Step 4: Move excursion hold installation out of verify.py

`_gate_spin` (verify.py:116) installs confirmed holds from excursion investigation onto `state.forced_holds`. This is Memory inside Judgment.

**types.py:** Add `excursion_holds: tuple[_ActionPair, ...] = ()` field to `_AttemptResult`.

**verify.py:**
- Add `excursion_holds: list[_ActionPair]` parameter to `_gate_spin` (same pattern as `gate_events`).
- Replace `_install_holds(state.work, result.confirmed_holds, state.forced_holds)` with `excursion_holds.extend(result.confirmed_holds)`.
- In `verify_gates`, create `excursion_holds: list[_ActionPair] = []`, pass to `_gate_spin`, attach to returned `_AttemptResult`.
- Remove unused `_install_holds` import from verify.py.

**pilot.py:** After each `attempt = _try_*(...)` call, apply:
```python
if attempt.excursion_holds:
    _install_holds(state.work, list(attempt.excursion_holds), state.forced_holds)
```

**Timing:** The original install happens inside `_gate_spin` before downstream gates. Downstream gates (`_gate_cycle`, `_gate_dead_end`, `classify_outcome`) don't read `state.forced_holds`, so moving the install after `verify_gates` returns is safe. The meaningful persistence is in `state.forced_holds` (the dict), not `state.work` (which gets replaced by `_commit_trial`). Excursion holds apply for all attempts (accepted or rejected) — install before the acceptance check.

### Step 5: Add structured stuck detection

When the pilot falls through all candidates to the coast fallback, emit a diagnostic event.

**pilot.py:** Before the bounded cone settle (line 862), add:
```python
stuck_reason = _diagnose_stuck(frame, candidates, state)
yield PilotEvent("stuck", state.work.state.scan_id, {
    "reason": stuck_reason,
    "distance": frame.distance_before,
    "candidate_count": len(candidates.candidates),
    "nogoods_at_key": len(state.nogoods.get(frame.key, set())),
})
```

Add helper `_diagnose_stuck` that returns one of:
- `"exhausted_search"` — all candidates are nogoods at this key
- `"no_candidates"` — no candidates surfaced (empty trace + no route + no influence)
- `"all_rejected"` — candidates existed but all failed gates

## Deferred

**Compass observation recording (steer.py):** `_record_compass_observations` is called at 4 sites inside steer.py, interleaved with execution (per-wait-snap, per-dwell-snap). Extracting it requires attaching the full snapshot trajectory to `_AttemptResult` and replaying observations in the pilot loop. The coupling is high and the payoff is modest — "observe while you act" is a defensible boundary for now.

**candidates.py off-path nogoods (line 342):** `_build_candidates` mutates `state.nogoods` for off-path compass masking. Same category of concern but in Direction, not Judgment. Could be a `masked_pairs` field on `_CandidateList` applied by the pilot loop. Low priority.

## Verification

After each step: `make test-pilot && make test-prove`
After steps 3-4 (medium risk): also `make test`

All changes are behavior-preserving except the additive stuck event. No new test failures expected.

## Files modified

| File | Steps |
|------|-------|
| `src/pyrung/core/analysis/pilot/types.py` | 3, 4 |
| `src/pyrung/core/analysis/pilot/outcome.py` | 1 |
| `src/pyrung/core/analysis/pilot/progress.py` | 1 |
| `src/pyrung/core/analysis/pilot/verify.py` | 3, 4 |
| `src/pyrung/core/analysis/pilot/steer.py` | 2, 3 |
| `src/pyrung/core/analysis/pilot/candidates.py` | 2 |
| `src/pyrung/core/analysis/pilot/pilot.py` | 3, 4, 5 |
| `src/pyrung/core/analysis/pilot/CLAUDE.md` | 1 |
| `tests/core/analysis/test_pilot.py` | 2 |
