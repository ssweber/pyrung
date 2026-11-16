# Fuzz Reproducers

Each file here is a minimized program on which the interpreted `PLC` and `CompiledPLC` disagree. `tests/fuzz/test_parity.py` writes one when it finds a disagreement.

## Usage

```powershell
# Run a reproducer
uv run pytest tests/fuzz/reproducers/parity_YYYYMMDD_HHMMSS_000.py
```

The reproducer steps both engines through the inputs up to the first scan that differs. It then asserts that their tags and memory are equal. Fix the engine that is wrong, then move the case into a regression test next to the code it covers.

## File naming

- `parity_*.py` holds an engine disagreement.

Each file contains a `test_reproducer()` function runnable by pytest.

## Running fuzz tests longer

Two env vars control duration:

- `FUZZ_MAX_EXAMPLES` is the number of random programs Hypothesis generates (default 200).
- `FUZZ_SCANS` is the number of scans each program runs (default 50).

More programs beats longer scans. Most bugs surface in the first 50 scans, but rare topologies need volume to appear.

```powershell
# 10x more programs
$env:FUZZ_MAX_EXAMPLES = 2000; make test-fuzz

# More programs and longer simulation per program
$env:FUZZ_MAX_EXAMPLES = 2000; $env:FUZZ_SCANS = 500; make test-fuzz
```
