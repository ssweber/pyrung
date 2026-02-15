# Debug Implementation Plan

## Priority and Effort

1. **Major: force stage integration**
- This is a core engine change, not just a helper API.
- Requires scan-cycle integration (`APPLY FORCES` pre-logic and post-logic).
- Requires CODESYS-style semantics: pre/post force re-application with allowed mid-scan divergence.

2. **Major: history/playhead foundation**
- Required by most debug features.
- Includes history storage, retrieval APIs, playhead movement (`seek`, `rewind`), and eviction behavior.

3. **Medium: rung inspection (`inspect`)**
- Needs execution instrumentation to capture trace data.
- More than a wrapper API because it depends on per-scan/per-rung trace capture.

4. **Minor (after history exists): breakpoints, monitors, diff, fork**
- Mostly orchestration around committed snapshots.
- Straightforward once history/playhead primitives are stable.

## Recommended Implementation Order

1. Build `history + playhead` primitives.
2. Integrate `force` into the scan cycle.
3. Add `breakpoints`, `monitors`, `diff`, and `fork_from`.
4. Add `inspect` trace capture and retrieval.

## Additional Guidance

### Precedence Matrix (Must Be Explicit in Tests)

Document and test precedence for the same tag in one scan:

1. Read inputs
2. Patch
3. Force (pre-logic)
4. Logic writes
5. Force (post-logic)

Expected model:

- Pre-logic force writes prepared values before IEC code.
- IEC code may assign temporary different values mid-cycle.
- Post-logic force writes prepared values again before outputs.

### Performance Targets

- Force application should be `O(number_of_forced_tags)`.
- Avoid scanning all tags each scan just to apply force.
- History operations should be efficient for append and bounded eviction.

### Trace Capture Modes

Add trace capture levels so `inspect` overhead is controllable:

- `off`: no per-rung trace
- `minimal`: rung power and key write events
- `full`: condition/instruction level details

### Behavioral Invariants (Test First)

- No-force execution path remains behaviorally identical to current engine.
- Pre-logic force values are visible at program start.
- IEC assignments can temporarily diverge from forced values mid-scan.
- Post-logic force re-applies values before output write.
- Playhead navigation never changes execution tip behavior.
- Forked runner is independent from parent debug runtime state.

### Error Policy (Lock in Early)

- Forcing read-only tags raises `ValueError`.
- Accessing missing/evicted scans via `at/seek/diff` raises `KeyError`.
- Monitor callback exceptions propagate (fail-fast).

### Internal Rollout Sequence

1. Refactor `PLCRunner.step()` into explicit phase helpers (no behavior change).
2. Add history/playhead primitives and tests.
3. Add force pre/post integration with mid-scan divergence semantics.
4. Add breakpoints/monitors/diff/fork on top of committed snapshots.
5. Add inspect trace capture and retrieval with configurable trace mode.
