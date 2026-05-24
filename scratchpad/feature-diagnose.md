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
# runner.py — ~15 lines

def diagnose(
    self,
    tag: Tag | str,
) -> CausalChain:
    """Diagnose how a tag reached its current value from a snapshot.

    No history required. Walks the program graph backward from *tag*,
    using the current state as evidence. Terminates at external inputs.

    Returns a CausalChain with mode='diagnosed'. Conjunctive roots
    are external inputs that jointly caused the state. Ambiguous roots
    are alternatives (OR paths where the actual trigger is unknown).
    """
    from pyrung.core.analysis.causal import diagnosed_cause

    return diagnosed_cause(
        logic=self._logic,
        state=self._state,
        tag=tag,
        pdg=self._ensure_pdg(),
    )
```

### Internal function

```python
# causal/diagnosed.py

def diagnosed_cause(
    logic: list[Rung],
    state: SystemState,
    tag: Tag | str,
    pdg: ProgramGraph,
) -> CausalChain:
```

Minimal inputs: program, snapshot state, target tag, dependency graph. No history, no timelines, no firings.

---

## Algorithm

### Core idea

**`attribute()` recursively applied, with the snapshot as the evaluator, terminating at externals.**

The SP tree's `attribute()` function finds minimal load-bearing contacts for why a rung evaluates TRUE. Apply it recursively: at each rung, find load-bearing contacts, classify them as external (leaf) or internal (recurse), until the entire tree bottoms out at physical inputs.

### Two branches

**Stateless (OTE, calc, copy):** The rung MUST be TRUE right now for the output to hold. `attribute()` on the snapshot is definitive. No ambiguity.

**Stateful (latch, counter, timer):** The trigger may have cleared. The rung can be FALSE while the output holds. Enumerate candidate trigger paths through the SP tree structure. Report as ambiguous when multiple paths exist.

### Pseudocode

```python
def _walk_backward_from_snapshot(
    logic, state, pdg, tag_name, visited, steps, conjunctive_roots, ambiguous_roots
):
    writers = pdg.writers_of.get(tag_name, frozenset())

    if not writers:
        # External input — leaf of the diagnosis
        conjunctive_roots.append(...)
        return

    if tag_name in visited:
        return  # cycle guard
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
```

### SP tree structure gives MBD for free

- **SPSeries (AND):** All children must be true → conjunctive roots (all had to be true)
- **SPParallel (OR):** Any child being true suffices → ambiguous roots (either could be the "real" cause)

The four-rule attribution walk (`attribute()`) already handles this:
- SERIES TRUE → all children mattered
- PARALLEL TRUE → only TRUE children mattered

This IS the Model-Based Diagnosis minimal conflict/hitting set computation, encoded in the SP tree structure.

---

## Leveraging the prover infrastructure

Optional acceleration for cleaner output, not required for correctness:

### Cone of influence (already exists)

```python
upstream_tags = pdg.upstream_slice(tag_name)
```

Scope the walk. Everything outside is irrelevant.

### Functional dependency projections (passes.py:1040-1111)

If the prover knows `Y = X + offset`, then a snapshot showing `Y=42` immediately constrains `X=32`. Back-propagate through reverse edges (classify.py:876-940) deterministically. No search needed.

Reverse edge types already supported:
- Identity: `Y = X` → `X = Y`
- Linear: `Y = X + K` → `X = Y - K`
- Linear multiply: `Y = X * K` → `X = Y // K`
- Unary: `Y = -X` → `X = -Y`

### Init constant projections (passes.py:1113-1376)

Tags written only under first-scan or monotonic latch guards with literal values. In the snapshot these are evidence anchors — their values are fixed, they eliminate branches.

### Elidable tags (elision/slice.py)

Tags always written-before-read within a scan. Their scan-entry values don't matter. Skip them in the walk — they're noise, not causal.

### Application

```python
def diagnosed_cause(
    logic, state, tag, pdg,
    *,
    use_projections: bool = True,
):
    if use_projections:
        # 1. Scope to cone
        cone = pdg.upstream_slice(tag_name)
        # 2. Back-propagate functional deps from snapshot values
        # 3. Pin init-constant tags as evidence
        # 4. Skip elidable tags in walk
    ...
```

---

## Output model

### CausalChain with mode="diagnosed"

```python
CausalChain(
    effect=Transition("FaultAlarm", scan_id=0, from_value=None, to_value=True),
    mode="diagnosed",
    steps=[...],
    conjunctive_roots=[...],   # externals that jointly caused this
    ambiguous_roots=[...],     # OR-branch alternatives (genuine uncertainty)
)
```

- `scan_id=0` — sentinel, no real scan history
- `from_value=None` — unknown prior value (trigger cleared)
- New `mode` literal: `"diagnosed"`

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

---

## What it catches

| Element | Method |
|---------|--------|
| Active rung chains (OTE → OTE → ...) | `attribute()` definitive |
| Latch with trigger still active | `attribute()` definitive |
| Latch with cleared trigger, single path | Structural inference from SP tree |
| Physical inputs sustaining the fault | External termination |
| Algebraic constraints through calc chains | Reverse edge back-propagation |
| Reset NOT having fired | Confirmatory (snapshot consistency) |

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

### Output

```
FaultAlarm = True  [diagnosed]
  └─ OverTemp LATCHED (trigger cleared)
       ├─ TempSensor > 180  ← external (currently 185)
       └─ MotorRun was TRUE (inferred)
            ├─ StartCmd LATCHED (trigger cleared)
            │    └─ X001 momentary TRUE  ← external (cleared)
            └─ NOT FaultActive (was FALSE before this fault)
```

---

## Implementation plan

### Phase 1: Core algorithm (~200-300 lines)

1. `diagnosed.py` — the backward walk with stateless/stateful branching
2. Add `mode="diagnosed"` literal to `CausalChain`
3. Add `fidelity="structural"` literal to `ChainStep`
4. `runner.diagnose()` method (thin wrapper)
5. Wire into `causal/__init__.py` exports

### Phase 2: Prover integration (optional, cleaner output)

6. Cone-of-influence scoping via `upstream_slice()`
7. Functional dep back-propagation from snapshot values
8. Init-constant pinning
9. Elidable tag skipping

### Phase 3: Validation & output

10. Forward validation: for stateful candidates, `runner.step()` the hypothesized prior state and check consistency with snapshot
11. Tree rendering (reuse `CausalChain.__str__` with diagnosed-mode formatting)
12. DAP integration (troubleshoot command in debug session)

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
- **Reuse CausalChain model.** Same output type as `cause()`/`effect()`. Consumers (DAP, CLI, tests) work unchanged.
- **Honest confidence.** Ambiguous cases reported as ambiguous, not guessed. `confidence` field already exists on the model.
- **Terminate at externals.** `writers_of` empty = `TagRole.INPUT` = physical input = free variable = leaf. Already defined by the system.
- **Snapshot as evaluator.** `_HistoricalView(state)` already exists and works. No new view type needed.
