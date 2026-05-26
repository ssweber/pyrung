# `diagnose()` — backward reachability from a snapshot

## Summary

Given a tag dump from a live PLC, reconstruct how it got there — without history, without simulation, without logging. Just a snapshot and the program.

```python
data = read_plc_data("fault_dump.csv", skip_default=True)
tags = mapping.tags_from_plc_data(data)
runner = PLC(logic, initial_state=SystemState().with_tags(tags))
result = runner.diagnose(FaultAlarm)
```

One call. "Here's my faulted machine. What happened?"

Multiple tags narrow the explanation — one unified tree, not N independent trees:

```python
# "fault alarm AND motor stall — are these related?"
result = runner.diagnose(FaultAlarm, MotorStall)

# each additional tag is a constraint that prunes inconsistent branches
result = runner.diagnose(FaultAlarm, MotorStall, CoolingPumpOff)
```

---

## Architecture

### Where it lives

```
src/pyrung/core/analysis/
├── causal/
│   ├── recorded.py      # cause() with history
│   ├── projected.py     # cause(to=) without history (what-if)
│   ├── diagnosed.py     # NEW — diagnose() from snapshot
│   ├── models.py        # CausalChain, ChainStep, etc. (add mode="diagnosed")
│   └── support.py       # _HistoricalView, attribute() helpers (reuse)
```

Sibling to `recorded.py` and `projected.py`. Same models, same SP tree engine, same PDG. Different input: a frozen snapshot instead of a scan log or hypothetical.

### Runner method

```python
# runner.py — ~20 lines

def diagnose(
    self,
    *tags: Tag | str,
) -> CausalChain:
    """Diagnose how tags reached their current values from a snapshot.

    No history required. Walks the program graph backward from each
    tag, using the current state as evidence. Terminates at external
    inputs.

    Multiple tags produce one unified tree — a single explanation
    consistent with all observations. Branches that explain one tag
    but conflict with another are pruned. Each additional tag narrows
    the diagnosis.

    Returns a CausalChain with mode='diagnosed'. Conjunctive roots
    are external inputs that jointly caused the state. Ambiguous roots
    are alternatives (OR paths where the actual trigger is unknown).
    """
    if not tags:
        raise ValueError("diagnose() requires at least one tag")

    from pyrung.core.analysis.causal import diagnosed_cause

    return diagnosed_cause(
        logic=self._logic,
        state=self._state,
        tags=[_resolve_tag_name(t) for t in tags],
        pdg=self._ensure_pdg(),
    )
```

**Note:** `cause()` and `effect()` accept a single tag. `diagnose(*tags)` is a
new pattern — justified because the full system state is already loaded, so tags
are queries into the same snapshot, not separate analyses. `prove()` similarly
accepts `*conditions` as multiple queries over the same program.

### Internal function

```python
# causal/diagnosed.py

def diagnosed_cause(
    logic: list[Rung],
    state: SystemState,
    tags: list[str],
    pdg: ProgramGraph,
) -> CausalChain:
```

Minimal inputs: program, snapshot state, target tag(s), dependency graph. No history, no timelines, no firings.

Single-tag: walks backward from one tag. Multi-tag: walks backward from each tag sharing the same `visited` set, accumulating into the same `steps`/`conjunctive_roots`/`ambiguous_roots`. The shared walk naturally merges at common internal tags.

---

## Algorithm

### Core idea

**`attribute()` recursively applied, with the snapshot as the evaluator, terminating at externals.**

The SP tree's `attribute()` function finds minimal load-bearing contacts for why a rung evaluates TRUE. Apply it recursively: at each rung, find load-bearing contacts, classify them as external (leaf) or internal (recurse), until the entire tree bottoms out at physical inputs.

### Three branches

**Stateless (OTE, calc, copy):** The rung MUST be TRUE right now for the output to hold. `attribute()` on the snapshot is definitive. No ambiguity.

**Stateful (latch, counter, timer):** The trigger may have cleared. The rung can be FALSE while the output holds. Enumerate candidate trigger paths through the SP tree structure. Report as ambiguous when multiple paths exist.

**Reset path (latch complement):** For every latched tag, also check its reset rungs. A reset rung that's FALSE confirms the latch is held — the reset condition isn't met. A reset rung that's TRUE is a contradiction (latch is ON but reset should have cleared it) — flag as inconsistency. The reset side completes the picture: "latched because X, *still latched because reset Y hasn't fired*."

### "Why NOT" — blocking analysis

The algorithm handles both TRUE and FALSE targets. When a tag is FALSE and the engineer asks "why isn't this running?":

- **Stateless (OTE):** The rung IS the answer. `attribute()` with the FALSE case finds the blocking contacts (SERIES FALSE → return FALSE children = blockers). These are the reasons the output isn't ON.
- **Stateful (latch):** Either (a) never latched (no trigger history — ambiguous) or (b) was reset (check reset rungs — if reset rung is TRUE, `attribute()` finds what's holding the reset active).

This is the dual of the "why TRUE" walk. The engineer often knows what *should* be running and isn't — "why is MotorRun OFF?" is as natural as "why is FaultAlarm ON?"

### Pseudocode

```python
def diagnosed_cause(logic, state, tags, pdg):
    visited = set()
    steps = []
    conjunctive_roots = []
    ambiguous_roots = []

    # Multi-tag: walk each query tag, sharing visited/roots.
    # Second tag reuses already-visited nodes — its walk adds only
    # the branches unique to it, or terminates immediately at shared
    # internal nodes that were already explained.
    for tag_name in tags:
        _walk_backward_from_snapshot(
            logic, state, pdg, tag_name,
            visited, steps, conjunctive_roots, ambiguous_roots,
        )

    return CausalChain(
        effect=...,   # first tag's transition
        effects=...,  # all tag transitions (multi-tag only)
        mode="diagnosed",
        steps=steps,
        conjunctive_roots=conjunctive_roots,
        ambiguous_roots=ambiguous_roots,
    )


def _walk_backward_from_snapshot(
    logic, state, pdg, tag_name, visited, steps, conjunctive_roots, ambiguous_roots
):
    writers = pdg.writers_of.get(tag_name, frozenset())

    if not writers:
        # External input — leaf of the diagnosis
        conjunctive_roots.append(...)
        return

    if tag_name in visited:
        return  # cycle guard — also merges multi-tag walks at shared nodes
    visited.add(tag_name)

    # Build snapshot evaluator
    view = _HistoricalView(state)
    def snapshot_eval(cond): return cond.evaluate(view)

    for rung_idx in writers:
        rung = logic[rung_idx]
        sp_tree = rung.sp_tree()

        if _is_latch(rung, tag_name) and not evaluate_sp(sp_tree, snapshot_eval):
            # STATEFUL, TRIGGER CLEARED
            # The latch is ON but its rung is FALSE — trigger was transient.
            # Enumerate all contacts on the rung as candidate triggers.
            leaves = _collect_sp_leaves(sp_tree)
            for leaf in leaves:
                contact_tag = _condition_tag_name(leaf.condition)
                if writers_of[contact_tag] is empty:
                    ambiguous_roots.append(...)  # external, can't confirm
                else:
                    # Internal — its current value is evidence
                    _walk_backward_from_snapshot(...)

        elif evaluate_sp(sp_tree, snapshot_eval):
            # STATELESS or LATCH STILL ACTIVE
            # attribute() gives minimal load-bearing contacts
            attributions = attribute(sp_tree, snapshot_eval)
            for attr in attributions:
                contact_tag = _condition_tag_name(attr.condition)
                if writers_of[contact_tag] is empty:
                    conjunctive_roots.append(...)  # external, confirmed
                else:
                    _walk_backward_from_snapshot(...)  # internal, recurse

    # RESET PATH — for latched tags, explain why the reset hasn't fired
    if _is_latch(rung, tag_name) and state.tags.get(tag_name):
        reset_writers = _reset_writers_of(pdg, logic, tag_name)
        for reset_rung_idx in reset_writers:
            reset_rung = logic[reset_rung_idx]
            reset_sp = reset_rung.sp_tree()
            if reset_sp and evaluate_sp(reset_sp, snapshot_eval):
                # INCONSISTENCY: reset rung is TRUE but latch is still ON
                steps.append(ChainStep(..., fidelity="structural", inconsistency=True))
            elif reset_sp and not evaluate_sp(reset_sp, snapshot_eval):
                # Reset not firing — attribute() FALSE case finds blockers
                blockers = attribute(reset_sp, snapshot_eval)
                # Record as confirmatory: "still latched because reset blocked by..."
                steps.append(ChainStep(..., fidelity="structural"))
```

### Multi-tag merging

The `visited` set is the merge mechanism. When multiple tags share upstream structure (common in fault cascades), the second walk hits already-visited internal tags and stops — the shared roots are counted once, not duplicated.

This means:
- **Independent faults** produce disjoint subtrees in one chain (separate roots, separate steps).
- **Related faults** (cascading from a common cause) converge on shared roots — the unified tree is smaller than two independent diagnoses.
- **Conflicting observations** (tag A's explanation requires B=TRUE but B=FALSE in snapshot) surface as steps where the rung evaluates FALSE for a non-latch — currently unhandled by either branch. These should be flagged as **inconsistencies** in the output.

Each additional tag either (a) merges into existing structure (confirms the diagnosis) or (b) adds new branches (extends it). The engineer iteratively adds tags they find surprising, watching the tree simplify or split.

### SP tree structure gives MBD for free

- **SPSeries (AND):** All children must be true → conjunctive roots (all had to be true)
- **SPParallel (OR):** Any child being true suffices → ambiguous roots (either could be the "real" cause)

The four-rule attribution walk (`attribute()`) already handles this:
- SERIES TRUE → all children mattered
- PARALLEL TRUE → only TRUE children mattered

This IS the Model-Based Diagnosis minimal conflict/hitting set computation, encoded in the SP tree structure.

---

## Leveraging the prover infrastructure

Optional acceleration for cleaner output, not required for correctness.

### Cone of influence (already exists)

```python
upstream_tags = pdg.upstream_slice(tag_name)
```

Scope the walk. Everything outside is irrelevant. Already public on `ProgramGraph`.

### Functional dependency back-propagation

If the program has `Y = X + offset`, then a snapshot showing `Y=42` immediately constrains `X=32`. Back-propagate through reverse edges deterministically. No search needed.

**Extracted** to `analysis/reverse_edges.py`: `calc_reverse_edge()`, `tag_name_from_value()`, `literal_value_from_value()`, `compose_invert()`, `InvertFn`, `IDENTITY`, `build_reverse_edge_map()`. Pure expression analysis, zero prover coupling. The prover and `elision/slice.py` now import from this module.

Still needed for Phase 2: `back_propagate_value(edge_map, tag, value) -> dict[str, Any]` — the snapshot-facing convenience function that inverts the source→target map and applies invert functions to concrete values. The building blocks are extracted; the final API is not yet written.

Reverse edge types:
- Identity: `Y = X` → `X = Y`
- Linear: `Y = X + K` → `X = Y - K`
- Linear multiply: `Y = X * K` → `X = Y // K`
- Unary: `Y = -X` → `X = -Y`

### Init-constant pinning

Tags written only under first-scan or monotonic latch guards with literal values. In the snapshot these are evidence anchors — their values are fixed, they eliminate branches.

**Extracted** to `analysis/init_constants.py`: `find_instruction_at_site()`, `detect_init_constants()` (three patterns: self-latching Bool guard, co-latching nondeterministic guard, first_scan guard). Takes `program`, `graph`, `sites_by_target`, `candidate_tags`, optional `nondeterministic_inputs` and `edge_source_tags`. Prover's `_pass_detect_init_constants` delegates to it.

### Write-before-read tag skipping

Tags always written-before-read within a scan. Their scan-entry values don't matter. Skip them in the walk — they're noise, not causal.

The PDG already exposes `pdg.unconditional_write_before_read(tag_name)` — the fast-path check that covers the common case (def-use chains prove no read precedes the first unconditional write). This is sufficient for `diagnose()`.

The full enumeration engine (`_SliceElision` in `prove/elision/slice.py`) handles edge cases where the fast path fails: conditional writes that still always precede reads across all domain combinations. This is heavily coupled to prover domain knowledge (`stateful_dims`, `nondeterministic_dims`). For `diagnose()`, the fast-path PDG check is the right abstraction — it's already public, needs no extraction, and covers the vast majority of scan-local tags. Tags that are scan-local only under the full enumeration are rare enough to be noise in a diagnosis.

### Application

```python
def diagnosed_cause(
    logic, state, tags, pdg,
    *,
    use_projections: bool = True,
):
    if use_projections:
        # 1. Scope to cone (multi-tag: union of upstream slices)
        cone = set()
        for t in tags:
            cone |= pdg.upstream_slice(t)
        # 2. Back-propagate functional deps from snapshot values
        # 3. Pin init-constant tags as evidence
        # 4. Skip elidable tags in walk
    ...
```

---

## Output model

### CausalChain with mode="diagnosed"

```python
# Single-tag
CausalChain(
    effect=Transition("FaultAlarm", scan_id=0, from_value=None, to_value=True),
    mode="diagnosed",
    steps=[...],
    conjunctive_roots=[...],   # externals that jointly caused this
    ambiguous_roots=[...],     # OR-branch alternatives (genuine uncertainty)
)

# Multi-tag — one tree, multiple entry points
CausalChain(
    effect=Transition("FaultAlarm", scan_id=0, from_value=None, to_value=True),
    effects=[
        Transition("FaultAlarm", scan_id=0, from_value=None, to_value=True),
        Transition("MotorStall", scan_id=0, from_value=None, to_value=True),
    ],
    mode="diagnosed",
    steps=[...],               # unified steps from all walks
    conjunctive_roots=[...],   # shared external roots (deduplicated)
    ambiguous_roots=[...],
)
```

- `scan_id=0` — sentinel, no real scan history
- `from_value=None` — unknown prior value (trigger cleared)
- New `mode` literal: `"diagnosed"`
- New `effects` field: `list[Transition]`, default empty. Populated for multi-tag diagnosis. `effect` remains the first/primary tag for backward compatibility — consumers that only read `effect` still work.

### Model changes

```python
# models.py additions

@dataclass
class CausalChain:
    effect: Transition
    mode: Literal["recorded", "projected", "unreachable", "diagnosed"]  # add "diagnosed"
    steps: list[ChainStep] = field(default_factory=list)
    conjunctive_roots: list[Transition] = field(default_factory=list)
    ambiguous_roots: list[Transition] = field(default_factory=list)
    blockers: list[BlockingCondition] = field(default_factory=list)
    effects: list[Transition] = field(default_factory=list)  # NEW — multi-tag diagnosed
```

`effects` defaults empty so existing recorded/projected chains are unaffected.
Single-tag diagnosed: `effects` is empty, `effect` is the sole tag.
Multi-tag diagnosed: `effects` contains all queried tags, `effect` is `effects[0]`.

### ChainStep fidelity

```python
fidelity: Literal["full", "timeline", "structural"] = "full"
```

New value `"structural"` — inferred from program structure + snapshot, no history. The trigger/enabler distinction collapses: everything is "contributing" because we can't tell which transitioned last.

### Confidence model

| Case | Confidence | Method |
|------|-----------|--------|
| Stateless chain (all OTE/calc) | 1.0 — definitive | `attribute()` on active rung |
| Single-path latch (one way to trigger) | High — structurally necessary | Only one SP path exists |
| Multi-path latch (OR rung) | 1/N — genuinely ambiguous | N satisfied parallel branches |
| Counter/timer | Partial | Know accumulated value, not event history |
| Reset path not firing | 1.0 — definitive | `attribute()` FALSE on reset rung |
| "Why NOT" — stateless blocker | 1.0 — definitive | `attribute()` FALSE on OTE rung |
| "Why NOT" — latch never set | Low — no evidence | No trigger history |
| Steady-state snapshot | Higher | All steps self-consistent across one scan |
| Transient snapshot | Lower | Some steps may be mid-cascade |

---

## What it catches

| Element | Method |
|---------|--------|
| Active rung chains (OTE → OTE → ...) | `attribute()` definitive |
| Latch with trigger still active | `attribute()` definitive |
| Latch with cleared trigger, single path | Structural inference from SP tree |
| Why a latch hasn't unlatched | Reset path analysis — `attribute()` FALSE on reset rung |
| Why a tag is OFF (blocking analysis) | `attribute()` FALSE case → blocking contacts |
| Physical inputs sustaining the fault | External termination |
| Algebraic constraints through calc chains | Reverse edge back-propagation |
| Snapshot is transient (mid-cascade) | Steady-state check — one forward scan |

## What it doesn't catch

| Limitation | Why |
|-----------|-----|
| Trigger vs enabler distinction | Requires history (which transitioned last?) |
| Multiple latch-reset-latch cycles | Snapshot shows only current state |
| Race conditions within a scan | Program order gives partial info only |
| Transient external that spiked and cleared | Gone — but latch is evidence it happened |
| Counter event-by-event history | Know count value, not individual events |

All of these are **genuinely unresolvable from a snapshot** — no algorithm beats this without history.

---

## Example walkthrough

### Program

```
Rung 0:  X001 (pushbutton, external)      → LATCH StartCmd
Rung 1:  StartCmd AND NOT FaultActive      → OUT MotorRun
Rung 2:  MotorRun AND (TempSensor > 180)   → LATCH OverTemp
Rung 3:  OverTemp                          → OUT FaultAlarm
Rung 4:  OverTemp                          → OUT FaultActive
Rung 5:  ResetButton (external)            → RESET OverTemp
```

### Snapshot

```
X001 = FALSE, StartCmd = TRUE, MotorRun = FALSE, TempSensor = 185
OverTemp = TRUE, FaultAlarm = TRUE, FaultActive = TRUE, ResetButton = FALSE
```

### Walk

1. **FaultAlarm** — Rung 3 (OTE): `OverTemp` TRUE → rung active → `attribute()` → OverTemp is load-bearing. Internal, recurse.

2. **OverTemp** — Rung 2 (LATCH): SP tree = `Series(MotorRun, TempSensor > 180)`. Evaluate: MotorRun=FALSE → rung FALSE. But OverTemp is ON (latch). **Trigger cleared.** Enumerate contacts:
   - `TempSensor > 180`: external, currently TRUE (185). Consistent. → conjunctive root.
   - `MotorRun`: internal (writers_of non-empty). Must have been TRUE. Recurse.

3. **MotorRun** — Rung 1 (OTE): `Series(StartCmd, NOT FaultActive)`. Currently FALSE (because FaultActive is TRUE now). But we're asking what it WAS. StartCmd=TRUE and FaultActive would have been FALSE before OverTemp latched → consistent.
   - `StartCmd`: internal, recurse.
   - `NOT FaultActive`: confirmed by temporal ordering (FaultActive driven by OverTemp, which is downstream).

4. **StartCmd** — Rung 0 (LATCH): SP tree = `Leaf(X001)`. X001=FALSE → rung FALSE. Trigger cleared. Only one contact. → inferred external root.

5. **OverTemp reset path** — Rung 5 (RESET): `Leaf(ResetButton)`. ResetButton=FALSE → reset not firing. `attribute()` FALSE → ResetButton is the blocker. → confirmatory: "OverTemp still latched because ResetButton not pressed."

### Output

```
FaultAlarm = True  [diagnosed]
  └─ OverTemp LATCHED (trigger cleared)
       ├─ TempSensor > 180  ← external (currently 185)
       ├─ MotorRun was TRUE (inferred)
       │    ├─ StartCmd LATCHED (trigger cleared)
       │    │    └─ X001 momentary TRUE  ← external (cleared)
       │    └─ NOT FaultActive (was FALSE before this fault)
       └─ reset blocked: ResetButton = FALSE  ← external
```

---

## Implementation plan

### Phase 0: Extract reusable helpers from prover internals  ✅

Extract capabilities from `prove/` into shared modules that both the prover and `diagnose()` can consume. The prover's private call sites switch to the new public API — no behavior change, just a seam.

1. **Reverse edge computation** ✅ — extracted to `analysis/reverse_edges.py`: `InvertFn`, `IDENTITY`, `tag_name_from_value()`, `literal_value_from_value()`, `calc_reverse_edge()`, `compose_invert()`, `build_reverse_edge_map()`. Prover (`classify.py`) and `elision/slice.py` import from new module. `back_propagate_value()` deferred to Phase 2 — building blocks are in place.

2. **Init-constant detection** ✅ — extracted to `analysis/init_constants.py`: `find_instruction_at_site()`, `detect_init_constants()` (three patterns). Takes `sites_by_target` as pre-computed input to avoid coupling to `_all_write_targets` in `prove/absorb.py`. Prover's `_pass_detect_init_constants` delegates to it.

3. **Write-before-read detection** ✅ (no extraction needed) — `pdg.unconditional_write_before_read(tag_name)` is already public on `ProgramGraph`. This is the fast-path check sufficient for `diagnose()`. The full enumeration engine (`_SliceElision`) stays in the prover — it requires domain knowledge that `diagnose()` doesn't have.

### Phase 1: Core algorithm (~290 lines)  ✅

4. ✅ `diagnosed.py` — `diagnosed_cause()` + `_walk_backward()` with three branches (stateless attribution, stateful-cleared enumeration, reset path). Handles branch rungs via `_resolve_rung()`. Detects reset vs latch via instruction inspection.
5. ✅ `mode="diagnosed"` literal on `CausalChain`
6. ✅ `effects: list[Transition]` field on `CausalChain` (default empty, populated for multi-tag)
7. ✅ `fidelity="structural"` literal on `ChainStep`; `__str__` renders `= value` instead of `from→to` for structural steps
8. ✅ `runner.diagnose(*tags)` method (thin wrapper, normalizes tag names, delegates)
9. ✅ `causal/__init__.py` exports `diagnosed_cause`
10. ✅ `tests/core/test_diagnosed.py` — 11 tests: OTE chain, why-NOT blockers, OR disambiguation, latch trigger cleared/active, reset path blocking, multi-tag merging, structural fidelity, fill station snapshot

### Phase 2: Inference integration (cleaner output, uses Phase 0 helpers)  ✅

11. ✅ Cone-of-influence scoping via `upstream_slice()` (multi-tag: union of cones)
12. ✅ Functional dep back-propagation from snapshot values via `back_propagate_value()` — new function in `reverse_edges.py`, transitive inversion through calc/copy chains
13. ✅ Init-constant pinning via `detect_init_constants()` — init-constant tags treated as evidence anchors (leaves)
14. ✅ Write-before-read tag skipping via `pdg.unconditional_write_before_read()` — scan-local tags excluded from walk

### Phase 3: Validation & output

15. Steady-state check: run one forward scan from the snapshot, compare output to input. If different, flag the diagnosis as transient (mid-cascade). Metadata on `CausalChain`: `steady_state: bool`.
16. Forward validation: for stateful candidates, `runner.step()` the hypothesized prior state and check consistency with snapshot
17. ✅ Tree rendering — diagnosed-mode `__str__` with roots-first layout, instruction labels (`latch`, `reset`, `out`, `copy`, etc.), kind annotations (7 kinds: attributed/trigger_cleared/latch_blocked/reset_blocked/reset_active/reset_inconsistent/transient), and rung state descriptions
18. ✅ DAP integration — `diagnose <tag> [tag2 ...]` console verb, `diagnose:Tag1,Tag2` in `pyrungCausal` request, `pyrung live` support. Tested end-to-end against Click conveyor example.

---

## Interactive exploration

The engineer uses `diagnose()` to poke around. Start with one tag, see what comes back, add another, watch branches collapse. Each call is cheap — same snapshot, same program, different query.

```python
# "huh, fault alarm is on"
runner.diagnose(FaultAlarm)

# "oh, motor stalled too — are these related?"
runner.diagnose(FaultAlarm, MotorStall)

# "and the cooling pump is off — that's the link"
runner.diagnose(FaultAlarm, MotorStall, CoolingPumpOff)
```

Each additional tag is a constraint that narrows the explanation. The engineer brings domain knowledge the tool doesn't have — they know which tags smell wrong, which ones are surprising, which ones shouldn't be in that state. The tool does the structural reasoning. The engineer steers.

In the DAP GUI this is selecting tags from a watch list. Check one, see a tree. Check another, tree simplifies. The ambiguous OR branches from a single-tag diagnosis resolve as you add observations. The engineer converges on the root cause by combining what they see on the machine with what the tool knows about the program.

**Performance note:** The backward walk is bounded by `upstream_slice()` — typically a small fraction of program tags. Re-running with an additional tag is not a full re-walk; the shared `visited` set means only new branches are explored. In practice, adding a tag to a diagnosis is near-instant.

---

## Relationship to existing features

| Feature | Input | Output | Certainty |
|---------|-------|--------|-----------|
| `cause()` recorded | Scan history | What DID happen | Definitive |
| `cause(to=)` projected | Current state + desired value | What WOULD need to happen | Structural |
| `diagnose()` diagnosed | Snapshot only | What COULD have happened | Structural + evidence |
| `prove()` | Program + property | All reachable states | Exhaustive |

`diagnose()` fills the gap: you have a real machine state but no history. `cause()` can't help (no history). `prove()` is the wrong question (not asking about all states, asking about THIS state). `diagnose()` is the diagnostic dual of `cause()` — same backward walk, different evidence source.

---

## Design decisions

- **NO SAT solver.** The program structure + `attribute()` + snapshot is sufficient. The SP tree gives you the MBD conflict/hitting-set structure for free.
- **Reuse CausalChain model.** Same output type as `cause()`/`effect()`. Consumers (DAP, CLI, tests) work unchanged. Multi-tag extends with `effects` field; `effect` stays primary for backward compat.
- **`*tags` is a new pattern.** `cause()`/`effect()` take single tags. `diagnose()` takes `*tags` because the full snapshot is already loaded — tags are queries, not inputs. Analogous to `prove(*conditions)` which also takes multiple queries over the same program.
- **Multi-tag = shared walk, not N independent walks.** The `visited` set merges walks at shared internal nodes. This is both a performance optimization (no redundant traversal) and the correct semantics (one unified explanation, not N independent ones).
- **Both directions.** The backward walk handles both TRUE targets ("why is this alarming?") and FALSE targets ("why isn't this running?"). `attribute()` already has the FALSE case — SERIES FALSE returns FALSE children (blockers). No new algorithm needed, just first-class support.
- **Complete latch analysis.** Every latched tag gets both trigger analysis (how it turned ON) and reset analysis (why it hasn't turned OFF). The reset side uses the same `attribute()` infrastructure on the reset rung — FALSE case finds what's blocking the reset.
- **Steady-state awareness.** One forward scan after the backward walk tells you whether the snapshot is stable or transient. This is free (one `step()` call) and changes interpretation of every ambiguous branch. Reported as metadata, not a mode change.
- **Phase 0: extract before consume.** The prover has reusable inference helpers (reverse edges, init-constants, elidable tags) buried as private functions. Extract them into shared `analysis/` modules before `diagnose()` consumes them — the prover switches to the same public API. No new code for these capabilities, just a seam.
- **Honest confidence.** Ambiguous cases reported as ambiguous, not guessed. `confidence` field already exists on the model.
- **Terminate at externals.** `writers_of` empty = `TagRole.INPUT` = physical input = free variable = leaf. Already defined by the system.
- **Snapshot as evaluator.** `_HistoricalView(state)` already exists and works. No new view type needed.
