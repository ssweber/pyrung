# Fuzz Reproducers

This directory holds minimized fuzz cases where optimized `prove()` disagrees
with `_skip_optimizations=True`.

Run a reproducer directly with pytest:

```powershell
uv run pytest tests/fuzz/reproducers/soundness_YYYYMMDD_HHMMSS.py -q
```

Most files also include a comment showing the regression-test shape to add to
`tests/core/analysis/test_prove.py`, usually via `_assert_soundness(...)`.

For optimizer triage, use:

```powershell
uv run python devtools/diagnose_prove_soundness.py tests/fuzz/reproducers/soundness_YYYYMMDD_HHMMSS.py
```

The diagnostic reruns optimized and unoptimized `prove(..., journal=True)`,
prints the counterexample trace and journal summary, then force-keeps elided
state-key tags to find small restoring sets. A restoring set means that keeping
those tag(s) stateful makes the optimized result agree with the unoptimized
result, which is a useful starting point for identifying the unsound pass or
elision decision.
