# Fill Station Safety Review — June 3, 2026

## Summary

We used the pyrung analysis workflow against the live fill station program (via ClickNick) to review the logic, simulate failure scenarios, and formally verify a safety property. The process caught two bugs — one obvious, one that only exhaustive verification could find.

## How we worked

The pyrung toolchain lets you read, simulate, search, and prove PLC programs without touching the real machine. We connected to the running ClickNick session with `clicknick-cli`, which gave us the generated pyrung project — a Python model of the fill station's ladder logic. From there:

- **Read the code** — understood the step machine (off → waiting → filling → repeat), the alarm logic, the solenoid output, and the stacklight indicators
- **Queried the structure** — `dataview` showed 19 inputs, 18 outputs, 27 internal pivots. `simplified` resolved the solenoid's logic chain back to its root inputs
- **Searched for paths** — `how fill_stepNumber == 5` found the exact input sequence to reach the filling state (turn on the machine, set the level low, wait for timers)
- **Simulated failures** — forced the level sensor high mid-fill to watch the alarm cascade
- **Found bugs** during simulation and verification (see below)
- **Proved the fix** — exhaustively checked all 7,149 reachable states to confirm the safety property holds

## Issue 1 — Manual and auto fill cancel each other out

**Found during simulation.** We set up a scenario where both the automatic fill cycle and the manual operator override (`HMI_fill`) were active at the same time. The solenoid shut off — no alarm, no indication, it just stopped filling.

**Root cause:** The I/O subroutine used exclusive-OR logic on the solenoid. It was designed so that `HMI_fill` would *toggle* the valve — if the auto fill was running, pressing manual fill would stop it, and vice versa. But that means an operator manually filling the tank gets a surprise shutoff when the auto cycle happens to engage.

**Fix:** Changed XOR to OR — the solenoid opens when *either* source requests it. Only an alarm can shut it off. Verified with `simplified fill_solv_nc` which now reads `Or(fill_stepNumber == 5, HMI_fill), ~alarm` — clean and obvious.

## Issue 2 — Solenoid could briefly open during an alarm

**Found by the prover.** After fixing Issue 1, we formally verified: "the solenoid can never be open while an alarm is active." The prover came back with a counterexample — a specific sequence of inputs that violated the property.

We replayed the counterexample in simulation to confirm. The sequence:
1. Machine running, level exceeds max → alarm fires
2. Operator presses manual fill → solenoid opens for one scan *before* the alarm blocks it

**Root cause:** Subroutine call order. In the main program, `call(io)` ran at rung 11 and `call(error)` ran at rung 16. The solenoid output was computed *before* the alarm was updated, so for one scan cycle (a few milliseconds on real hardware) the solenoid saw the *previous* scan's alarm value. A one-scan window where the safety interlock didn't hold.

This is the kind of bug you don't find by reading code or manual testing. The window is one PLC scan wide. The prover found it because it checks every reachable state, not just the ones you think to test.

**Fix:** Moved `call(error)` before `call(io)` in the main program so alarm state is always current when the solenoid output is computed. The prover re-verified: **Proven across all 7,149 reachable states.** Both fixes were applied back to Click via `clicknick-cli rung apply` and round-trip verified against the regenerated source.

## Tools used

| Tool | What it did |
|------|-------------|
| `clicknick-cli ping` | Discovered the project path and sync status |
| `clicknick-cli tag set-range/set-choices` | Annotated 6 tags to enable formal verification |
| `clicknick-cli prompt-save` | Signaled the engineer to review and save |
| `clicknick-cli rung apply/preview` | Pushed fixes back to Click's ladder format with copy-paste |
| `pyrung live dataview` | Mapped inputs, outputs, and internal tags |
| `pyrung live simplified` | Resolved the solenoid's logic to a readable expression |
| `pyrung live how` | Found the input sequence to reach the filling state |
| `pyrung live patch/force/step` | Simulated normal operation and sensor failures |
| `pyrung live why` | Traced alarm causes backward through the logic |
| `pyrung live prove never` | Exhaustively verified the safety property |
