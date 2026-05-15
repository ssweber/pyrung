# Build: Hypothesis Grammar-Based Fuzzer for BFS Verifier Soundness

## Goal

Build a Hypothesis-based grammar fuzzer that generates random valid PLC programs and uses them for three independent agreement checks — all from the same generator:

1. **Optimization soundness** — run optimized `prove()` vs unoptimized `prove()`. If optimized says Proven but unoptimized finds a Counterexample, the optimization is unsound.
2. **Engine parity via BFS exploration** — use the BFS to enumerate all reachable states of the generated mini-program. At each reachable state, run one scan through both the interpreted engine and the compiled kernel. Diff the resulting tag values. Disagreement at any state is a compilation/semantics bug. This is more thorough than sequential scan testing because the BFS visits states (overflow boundaries, timer-done transitions, edge-case input combinations) that a sequential run might never reach naturally.
3. **Full 3-way oracle** — for programs where all three engines converge, confirm they agree on the reachable state set and the property result.

## First Step — Build the Checklist

Before writing any code, investigate the codebase and emit a complete checklist of:

1. **Every instruction type** in the Click PLC instruction set we support — out, copy, calc, fill, blockcopy, on_delay, off_delay, retentive_timer, count_up, count_down, receive, compare (all forms), for_loop, subroutine calls, oneshot, etc. Get the full list from the code, don't guess.

2. **Every tag type** — Bool, Int, Dint, Real, Word, Char, Timer, Counter, and any compound/block types.

3. **Every operand form** — literal values, tag references, pointer/indirect references (block[ptr]), indirect expressions, timer sub-fields (.Acc, .Done, .Pre, .EN), counter sub-fields.

4. **Every wiring pattern that has historically caused bugs** — based on the test suite, changelog, and what you find in the code:
   - Timer/counter Acc used in downstream comparisons
   - Tags vs literals as timer/counter presets
   - Tag copied into a preset field (dynamic preset)
   - Pointer/indirect addressing in copy/fill/blockcopy destinations and sources
   - Copy chains (multi-hop data flow)
   - Calc with various operations (+, -, *, and non-invertible like %, bitwise)
   - ForLoop with variable count tag (including count=0)
   - OTE inside conditional subroutines
   - OTE with oneshot
   - Exclusive inputs across scans
   - Rising/falling edge conditions
   - Self-referencing accumulators (calc(C + 1, C))
   - Conditional writes (tag only written when rung condition true)
   - receive() destinations with and without external annotation
   - Chained timers (T2 enabled by T1.Done)
   - Count-down and bidirectional counters
   - Tags read by edge instructions (rise/fall) — cross-scan dependency

5. **Degenerate/boundary values to bias toward** — preset=0, preset=1, preset=max, comparison boundary=0, comparison boundary=default value, counter preset=1, external int with min=max, etc.

Print this checklist. Get it reviewed. Then build.

## Architecture

- One Hypothesis `@composite` strategy per instruction type
- A `tag_pool` strategy that maintains a bag of declared tags and sometimes reuses them (this is how wiring happens)
- A `program` strategy that composes 2-8 rungs with biased wiring rules
- A `property` strategy that picks an output tag and generates `prove(tag == False)` or similar
- Wiring bias rules that weight toward interesting patterns (timer Acc in comparisons, copy chains into presets, pointer indirects, etc.)
- The test itself runs three checks per generated program:
  1. **Optimization agreement:** generate program → run optimized prove() → if Proven, run unoptimized prove() → assert no Counterexample
  2. **Engine parity at BFS states:** generate program → BFS enumerates reachable states → at each state, run one interpreted scan and one compiled scan → diff all tag values → disagreement at any state is a failure
  3. **3-way oracle:** if all engines converge, assert they agree on reachable states and property verdict

## Key Constraints

Generated programs must be small enough that the unoptimized path has a chance of converging. Keep rung count low (2-8), timer presets bounded (0-100 for fuzzing), counter presets bounded similarly. The point is soundness checking, not performance testing.

The engine parity check (mode 2) is cheaper than BFS prove — no property evaluation, just state enumeration and scan comparison — so it can run on more programs per hour. Consider separate test targets or time budgets for each mode.

## Markers and Targets

- `@pytest.mark.soundness` — optimization agreement tests (mode 1)
- `@pytest.mark.parity` — engine parity tests (mode 2)
- `@pytest.mark.oracle` — full 3-way oracle tests (mode 3)
- `make test-soundness`, `make test-parity`, `make test-oracle` — run each independently
- `make test-fuzz` — run all three
