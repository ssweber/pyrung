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
