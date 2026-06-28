# PILOT / `how()` xfail survey

Test-wide inventory of `@pytest.mark.xfail` cases related to `how()` / graph /
PILOT, plus the other-subsystem xfails for completeness. These mark behaviour the
engine *should* eventually have — the day a gap closes, the test xpasses and
flags it. Convention: `reason="pilot: <category>"` (current engine) or
`reason="walker: …"` (deprecated walk engine).

Survey date: 2026-06-28.

## `how()` / graph / PILOT (current engine)

| Test | Loc | Reason | Gap |
|---|---|---|---|
| `test_graph.py::TestHow*::test_how_with_avoid_uses_non_avoided_route` | test_graph.py:194 | `pilot: latch-through-OR alternative route` | `how(Done, avoid=Ready)` should route around the avoided tag through the OR alternative |
| `test_graph.py::TestHow*::test_how_multiple_conditions_and` | test_graph.py:247 | `pilot: single-target only` (raises `ValueError`) | `how(Ready, Done)` — multi-target AND goals not supported |
| `test_graph_semantic_path.py::TestSemanticPathIntegration` (whole class) | test_graph_semantic_path.py:448 | `pilot: Int threshold / calc chain programs` (`strict=False`) | `how()` on Int-threshold / calc-chain programs |
| `test_packml_diagnosis.py::TestHiddenEventJumpSelfLoopOnly` | test_packml_diagnosis.py:478 | `pilot: PackML state machine programs` | hidden-event jump must fire only on a self-loop; `how(IDLE)` path fails its own replay |
| `test_packml_diagnosis.py::TestHowAbortedToExecute` | test_packml_diagnosis.py:520 | `pilot: PackML state machine programs` | 7-step waypoint path ABORTED→…→EXECUTE through the PackML SFC |
| `test_pilot_examples.py::test_conveyor_motor_reachable` | test_pilot_examples.py:40 | `pilot: NC-reset latch under state-machine churn` | click_conveyor `ConveyorMotor` — should latch Running and gate the motor, but PILOT wanders the sort state machine |
| `test_pilot_examples.py::test_running_route_ambiguous_resolves` | test_pilot_examples.py:52 | `pilot: route-ambiguous single-target resolution` | click_conveyor `Running` (latch + two NC resets) — PILOT should pick a route without an explicit `choice=` |

## `how()` deprecated walker engine (`walk/`)

| Test | Loc | Reason |
|---|---|---|
| `test_walk_how_e2e.py::test_how_with_callable_predicate` | test_walk_how_e2e.py:137 | `walker: opaque callable predicates need expr decomposition` |
| `test_walk_fold_churn.py` (×3) | test_walk_fold_churn.py:166, :251, :389 | `temporal done_bit fix gives the walker a direct decomposition …` |

## Other subsystems (not how/graph — for completeness)

| Test | Loc | Reason |
|---|---|---|
| `test_prove_passes.py::…::test_elides_canonical_return_early_pulse_flag` | test_prove_passes.py:646 | sliced elision cannot prove return_early pulse patterns scan-local |
| `test_prove_passes.py` (multi-writer pulse+reset) | test_prove_passes.py:669 | sliced elision cannot prove multi-writer pulse+reset patterns scan-local |
| `test_prove_simultaneous_edge_coverage_tests.py` | test_prove_simultaneous_edge_coverage_tests.py:216 | auto-joint detection does not yet infer simultaneous edge pairs spread across multiple rungs |
| `test_fold.py::…::test_inert_scan_toggle_does_not_disable_fold` | test_fold.py:608 (`strict=True`) | inert scan-toggle fold not yet implemented |
| `test_fold.py::…::test_scan_counter_crossing_folds_to_threshold` | test_fold.py:640 (`strict=True`) | scan_counter virtual-crossing fold not yet implemented |
| `test_reachability.py` (fuzz) | tests/fuzz/test_reachability.py:162 | BFS input composition does not enumerate cross-product of simultaneous `rise()` transitions |

## Roadmap themes (how/graph)

The seven how/graph PILOT gaps cluster into four capability themes:

1. **Multi-target** `how(A, B)` — `pilot: single-target only`.
2. **Avoid-aware routing** — `pilot: latch-through-OR alternative route`.
3. **Route-ambiguity auto-resolution** — `Running` (new); pick a route without `choice=`.
4. **Real state machines** — PackML, semantic Int/calc chains, conveyor NC-reset churn (`ConveyorMotor`, new).

## Re-running / regenerating

```bash
# list the live capability-gap reasons
grep -rn 'reason="pilot:' tests/        # current engine
grep -rn 'reason="walker:' tests/       # deprecated walk engine

# force the xfails to actually run (see which now xpass)
uv run pytest tests/core/analysis/test_pilot_examples.py --runxfail -q
```
