# Plan: Harness-Coupled Feedback Projection for the Prover

## Context

The prover's BFS enumerates every nondeterministic (nd) input independently at every state. Bool feedback inputs (e.g., `MotorFb`) linked via `link=` + `physical` to enable outputs (e.g., `MotorEn`) are currently treated as fully independent nd inputs with domain `(False, True)`. Each such feedback doubles the nd enumeration at every state, cross-producting with all other nd inputs.

The autoharness metadata declares that Fb is a deterministic function of En. Under the nominal model (`Fb = bool(En)`, or `Fb = (En == trigger_value)` for trigger links), Fb is no longer an independent dimension — it's derived from the enable's known value. This removes it from nd enumeration entirely.

**Scope**: Bool feedback couplings only (not analog profiles). Enables must be stateful (written by the program). Nominal timing model (`Fb = En`, no transient exploration). Caveat attached to results.

## Implementation

### 1. Coupling discovery — new `_discover_bool_harness_couplings()` in `passes.py`

Scan `graph.tags` for tags with `link != None` and `physical != None` and `physical.feedback_type == "bool"`:

- For flat tags: parse `tag.link` via `_parse_link_spec()` → `(en_name, trigger_raw)`. If `trigger_raw`, resolve via `_resolve_trigger_value()`.
- For structure-backed tags: iterate `tag._pyrung_structure_runtime._field_specs` and `_blocks`, resolving indexed instances per structure count (mirror `harness.py:_discover_structure_couplings` logic).
- **Filter**: only yield couplings where `en_name ∈ stateful_dims` AND `fb_name ∈ nondeterministic_dims` AND fb domain is `(False, True)`.
- Return `list[_BoolHarnessCoupling]`.

Import `_parse_link_spec` and `_resolve_trigger_value` from `harness.py`.

New dataclass in `passes.py`:
```python
@dataclass(frozen=True)
class _BoolHarnessCoupling:
    en_name: str
    fb_name: str
    trigger_value: int | str | None = None
```

### 2. New pass: `_pass_apply_harness_couplings`

**Position in pipeline**: after `validate_declared_bounds`, before `heuristic_seed_domains` / `apply_split_at`. Needs `{"graph", "classification"}`.

Logic:
1. Call `_discover_bool_harness_couplings(graph, stateful_dims, nondeterministic_dims)`
2. For each coupling: remove `fb_name` from `ctx.nondeterministic_dims`
3. Store couplings in `ctx._harness_couplings`
4. Append caveat: `"Feedback input(s) [Fb1, Fb2] assumed to track enable output(s) [En1, En2] (nominal harness coupling)"`
5. Journal each coupling as `Decision("apply_harness_couplings", "classification", "harness_coupled", ...)`

### 3. Propagate to `_ExploreContext`

- Add field `harness_couplings: tuple[_BoolHarnessCoupling, ...] = ()` to `_ExploreContext` (`__init__.py`)
- Add `_harness_couplings: list[_BoolHarnessCoupling]` field to `_PassContext` (default empty list)
- In `_PassContext.freeze()`:
  - Pass couplings to `_ExploreContext`
  - Add each `fb_name` to `mutable_tag_names` (BFS writes these directly)
  - Add coupling fb_names to the mutable set

### 4. BFS integration — inject Fb before kernel step

Extract a helper used at all kernel-step sites:

```python
def _inject_harness_feedbacks(context: _ExploreContext, kernel: ReplayKernel) -> None:
    for c in context.harness_couplings:
        en_val = kernel.tags.get(c.en_name, False)
        if c.trigger_value is not None:
            kernel.tags[c.fb_name] = (en_val == c.trigger_value)
        else:
            kernel.tags[c.fb_name] = bool(en_val)
```

**Injection ordering in the BFS inner loop** (bfs.py ~line 418-425):
1. Restore snapshot
2. Set demoted edge prevs
3. Set nd input values (`for name, value in input_assignment`)
4. *(future: apply forces — FMEA `force={}` will go here)*
5. **Inject harness feedbacks** ← reads En after forces, so forced En values propagate to Fb
6. `_step_kernel`

This ordering is critical for future FMEA composability: when `force={En: True}` forces an enable stuck-on, the coupling injection runs AFTER force application and reads the forced En value, so `Fb = True` — physically correct. The coupling comes for free with forced enables without any FMEA-specific logic.

Fb is a pure function of En — it does NOT appear in the state key. Two states with the same En produce the same Fb.

**Also inject in hidden-event paths**: call `_inject_harness_feedbacks` before kernel steps in `_step_event_from_advance` and the variant-enumeration loop in `_maybe_jump_hidden_event` (events.py), using the same helper.

### 5. `_OptConfig` flag

- Add `harness_coupling_projection: bool = True` to `_OptConfig`
- Add `"harness_coupling_projection"` to `_REDUCTION_OPTIMIZATIONS`
- Add `_pass_skip_harness_couplings` no-op stub
- Wire in `_passes_for_opt_config`: when `not opt.harness_coupling_projection`, override `"apply_harness_couplings"` → `_pass_skip_harness_couplings`

### 6. Edge-bearing feedback interaction

If `rise(Fb)` or `fall(Fb)` is used on a coupled feedback:
- With coupling projection, Fb is removed from nd_dims before `_partition_edge_bearing_inputs` runs in `freeze()`. So Fb won't appear in `edge_tag_names`.
- The kernel still maintains `kernel.prev[fb_name]` automatically. Since we set `Fb = bool(En)` before each step, `rise(Fb)` fires when En's effective value changes. This makes `rise(Fb)` equivalent to `rise(En)` under nominal coupling — correct.

## Files to modify

| File | Changes |
|------|---------|
| `passes.py` | `_BoolHarnessCoupling` dataclass; `_discover_bool_harness_couplings()`; `_pass_apply_harness_couplings()`; `_pass_skip_harness_couplings()` stub; `_harness_couplings` field on `_PassContext`; wire into `freeze()`, `_DEFAULT_PRE_BFS_PASSES`, `_OptConfig`, `_REDUCTION_OPTIMIZATIONS`, `_passes_for_opt_config` |
| `__init__.py` | `harness_couplings` field on `_ExploreContext` |
| `bfs.py` | Inject coupled feedback values before `_step_kernel` in the main BFS loop |
| `events.py` | Inject coupled feedback values before kernel steps in hidden-event paths |

## Verification

1. **Unit test**: Program with `En` (OTE) and `Fb` (input, `link="En"`, `physical=Physical("Fb", on_delay="20ms")`). Verify Fb removed from nd_dims, coupling discovered.
2. **Soundness agreement**: Motor-feedback program — compare `always()` results with coupling projection on vs off (sound baseline). Must agree.
3. **State count reduction**: Verify fewer visited states with coupling on.
4. **Trigger value test**: `link="State:RUNNING"` — verify correct target derivation.
5. **Edge-bearing test**: Program using `rise(Fb)` — verify correct behavior with coupling.
6. **Structure test**: UDT with `En`/`Fb` indexed fields — verify coupling discovery.
7. **Hidden-event integration**: Motor with watchdog timer — verify coupling injection in event jump paths.
8. Run `make test-prove` and `make test-soundness`.
