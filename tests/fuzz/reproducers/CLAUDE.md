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

## File naming

- `soundness_*.py` — `prove()` disagreement
- `reachability_*.py` — `reachable_states()` disagreement

Each file contains a `test_reproducer()` function runnable by pytest.
