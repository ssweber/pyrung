# Fuzz Reproducers

Minimized cases where optimized `prove()` or `reachable_states()` disagrees with `_skip_optimizations=True`.

## Usage

```powershell
# Run a reproducer
make test -- tests/fuzz/reproducers/soundness_YYYYMMDD_HHMMSS.py

# Triage: reruns optimized vs unoptimized, prints journal, finds minimal restoring set
uv run python devtools/diagnose_reproducer.py tests/fuzz/reproducers/soundness_YYYYMMDD_HHMMSS.py
```

A restoring set = the elided tag(s) that, when force-kept stateful, make optimized agree with unoptimized. Points to the unsound pass or elision decision. Guards (force-keeping) are acceptable short-term, but the goal is a structural fix in the pass that made the wrong call.

The diagnose script auto-detects prove vs reachable mode. If the default `--max-subset-size 2` doesn't find a restoring set, try `3`.

For reachable-mode reproducers where both optimized and unoptimized BFS agree but simulation finds extra states, the diagnose script re-runs BFS with all ND inputs as one joint group (`joint_inputs`) to definitively distinguish multi-flip gaps from real bugs. If joint-BFS reaches all missed states, the gap is a single-flip BFS limitation (not a bug) and the reproducer can likely be deleted.

## Inspecting pass internals

Pass `_debug=True` to `prove()` or `reachable_states()` to attach the frozen `_ExploreContext` to the result as `result._debug_context`. This exposes intermediate pass results — `stateful_dims`, `nondeterministic_dims`, `threshold_vector_specs`, `stateful_names`, `edge_tag_names`, `memory_key_names`, etc. — without reconstructing the pass pipeline by hand. Useful when triaging why an optimization made the wrong call.

```python
result = prove(logic, condition, _debug=True)
ctx = result._debug_context  # _ExploreContext or None
```

### --prove-debug pytest flag

Run any existing prove test with `--prove-debug` to automatically inject `_debug=True` and `journal=True` into all `prove()` and `reachable_states()` calls. On test failure, the full `_ExploreContext` is dumped to stderr — no need to write a standalone script or modify the test.

```powershell
# Run a specific failing/xfailed test with debug context dumping
uv run pytest tests/core/analysis/test_prove_fuzz_reproducer_regressions.py::test_fuzz_band_tagged_range_sum_dest_not_elided --prove-debug --runxfail -x

# Works on any prove test
uv run pytest tests/core/analysis/test_prove_bfs_api.py::test_name --prove-debug -x
```

The dump shows both optimized and unoptimized contexts side-by-side: `stateful_names`, `stateful_dims`, `threshold_vector_specs`, journal decisions, and counterexample traces. Implemented in `tests/core/analysis/conftest.py`.

## File naming

- `soundness_*.py` — `prove()` disagreement
- `reachability_*.py` — `reachable_states()` disagreement

Each file contains a `test_reproducer()` function runnable by pytest.

## Running fuzz tests longer

Two env vars control duration:

- `FUZZ_MAX_EXAMPLES` — number of random programs Hypothesis generates (default: 200)
- `FUZZ_SCANS` — simulation steps per program (default: 100 reachability, 50 parity)

```powershell
# 10x more programs
$env:FUZZ_MAX_EXAMPLES = 2000; make test-fuzz

# More programs and longer simulation per program
$env:FUZZ_MAX_EXAMPLES = 2000; $env:FUZZ_SCANS = 500; make test-fuzz

# Just reachability, cranked up
$env:FUZZ_MAX_EXAMPLES = 1000; $env:FUZZ_SCANS = 300; uv run pytest tests/fuzz/test_reachability.py
```
