# pyrung validators: taxonomy, new rules, and `packml_bench_3.py` fixes

Working document for v0.11.0 (or the release after, for the new rules). Three parts: integration with the existing static-validator system (including a rename/bucketing proposal), fixes to the PackML benchmark, and the specification for the new condition-analysis rules. Every new rule reuses existing analysis machinery (prover interval domains, `simplified()`, the `CORE_CONFLICTING_OUTPUT` exclusivity prover, crossing registry, `classify_dimensions`, writer-membership index) — the new work is detection wiring and diagnostic formatting, not solver work.

---

## Part 0 — Integration with the existing validator system

### One rule system, not two

Do **not** build `pyrung lint` as a separate pass. `logic.validate()` with `select`/`ignore`, `ValidationReport`, and rule codes already *is* the linter. The new rules register into the same registry; `pyrung lint` becomes the CLI face of `validate()`. One rule registry, one suppression mechanism, two entry points (API + CLI).

Machinery already in place that the new rules build on:

- `CORE_CONFLICTING_OUTPUT` already proves mutual exclusivity (CompareEq different-constant pairs, `BitCondition`/`NormallyClosedCondition` complements, range-complement `Lt`/`Ge`, `Le`/`Gt` pairs) — a satisfiability checker in embryo; `RUNG_CONTRADICTION` generalizes it to full rung conditions.
- The stuck-bits (structural) vs `stranded_bits()` (reachability) split in the docs is the same two-grade distinction the new rules need: **UNSAT, period** vs **unreachable given initial conditions**. Reuse the design language.

### Rename: bucket the flat `CORE_` namespace

Sixteen rules of four-plus kinds don't belong under one prefix, and codes are load-bearing (`select`/`ignore`, `ValueError` on unknowns) — the rename only gets more expensive later. Keep the house style of descriptive word codes (no ruff-style numerics: `CMP_TRUE_AT_RESET` self-documents in a config; `PYR013` never will). Prefix = category; enable category selection: `select={"CMP"}` expands to the bucket.

| Current | New | Category |
|---|---|---|
| `CORE_READONLY_WRITE` | `TAG_READONLY_WRITE` | Declaration contracts |
| `CORE_CHOICES_VIOLATION` | `TAG_CHOICES_VIOLATION` | Declaration contracts |
| `CORE_RANGE_VIOLATION` | `TAG_RANGE_VIOLATION` | Declaration contracts¹ |
| `CORE_FINAL_MULTIPLE_WRITERS` | `TAG_FINAL_MULTIPLE_WRITERS` | Declaration contracts |
| `CORE_CONFLICTING_OUTPUT` | `COIL_CONFLICTING_OUTPUT` | Write-site discipline |
| `CORE_STUCK_HIGH` | `COIL_STUCK_HIGH` | Write-site discipline |
| `CORE_STUCK_LOW` | `COIL_STUCK_LOW` | Write-site discipline |
| `CORE_POINTER_DEFAULT_BEFORE_BLOCK_START` | `PTR_DEFAULT_BEFORE_BLOCK_START` | Indirect addressing |
| `CORE_MISSING_PROFILE` | `PHYS_MISSING_PROFILE` | Physical realism (dt-aware) |
| `CORE_ANTITOGGLE` | `PHYS_ANTITOGGLE` | Physical realism (dt-aware) |
| *(new)* | `RUNG_CONTRADICTION` | Condition satisfiability |
| *(new)* | `RUNG_TAUTOLOGY` | Condition satisfiability |
| *(new)* | `CMP_EQ_ON_MONOTONE` | Comparison semantics |
| *(new)* | `CMP_DIM_MISMATCH` | Comparison semantics |
| *(new)* | `CMP_HAND_ROLLED_DONE` | Comparison semantics |
| *(new)* | `CMP_TRUE_AT_RESET` | Comparison semantics |
| *(new)* | `CMP_STATIC_ON_LEFT` | Comparison semantics |

¹ Decision needed: docs currently group `RANGE_VIOLATION` with the physical-realism dt rules, but the rule checks literal writes against declared min/max — a contract check that happens to accept `dt`. Recommendation: `TAG_`, with dt-forwarding as an implementation detail.

**Migration:** accept `CORE_*` in `select`/`ignore` as deprecated aliases for one release — `DeprecationWarning`, map to new code, remove the release after. Keeps every existing `validate(ignore={"CORE_ANTITOGGLE"})` working through v0.11.0.

### `ValidationReport` changes

- **Add `.severity` to findings** (error / warning / info / advisory). Currently findings carry only `.code`, `.target_name`, `.message` — advisory rules would break the documented `assert not report` idiom, since one style note fails CI.
- Add `report.errors()` / `report.warnings()` filters. New recommended test idiom: `assert not report.errors(), report.summary()`.
- Diagnostic format shared with the existing unreachable diagnostics (`BlockingRelation` candidates) and the `CLK_STATUS_BIT_NOT_PORTABLE` family. One-screenshot friendly.
- Messages state **behavior, not style**. "This rung is TRUE from the scan the timer starts" gets read; "consider reordering operands" gets ignored.

### Severity vocabulary

| Level | Meaning |
|---|---|
| error | Provably wrong: no input sequence can make the rung behave as any reasonable intent |
| warning | High-confidence bug pattern; repair hint offered |
| info | Convention/consistency; auto-fixable where semantics-preserving |
| advisory | Off by default or info-level; intent heuristics too weak for warning |

---

## Part 1 — Bench fixes: `packml_bench_3.py`

### 1.1 Invalid-mode-request guard rung is unsatisfiable

**Location:** main program, after the `on_delay(StateTimer, 1, "sec")` rung.

**Current:**

```python
with rung(Or(StateCurrent != S.IDLE, StateCurrent != S.STOPPED, StateCurrent != S.ABORTED),
          UnitModeCmd < 1, UnitModeCmd > 3):
    copy(0, UnitModeCmd)
    reset(ModeChgRequest)
```

**Problem:** rung args AND together. `UnitModeCmd < 1 AND UnitModeCmd > 3` is a contradiction — no integer satisfies it — so the rung never fires. The `Or` of three `!=` over one variable is also a tautology (a value can't equal all three states at once), so even a corrected range check would leave the state gate meaningless. Intent: reject a mode-change request when the machine is *not* in a mode-changeable state, or the requested mode is out of range. As written, invalid requests are never reset: `ModeChgRequest` with `UnitModeCmd = 5` passes through, `mode_change` runs, `ModeConfigIdx` lands on `dh[205]` (unwritten), and `DisabledStates` loads a default.

**Repair:**

```python
with rung(Or(
    And(StateCurrent != S.IDLE, StateCurrent != S.STOPPED, StateCurrent != S.ABORTED),
    UnitModeCmd < 1,
    UnitModeCmd > 3,
)):
    copy(0, UnitModeCmd)
    reset(ModeChgRequest)
```

(Use pyrung's actual conjunction-inside-`Or` form. The shape is the point: the buggy version has And/Or inverted at both levels — a double De Morgan's slip.)

**Keep the buggy condition** as a validator fixture: it is the canonical should-fail case for `RUNG_CONTRADICTION`/`RUNG_TAUTOLOGY`, and the author-confessed worked example for the release notes.

### 1.2 Init-order mode clobber

**Problem:** the first init rung sets `UnitModeCurrent = 1`, but the later `with rung(~InitDone): call(mode_change)` runs while `UnitModeCmd == 0`. Inside `mode_change`, the unconditional `copy(UnitModeCmd, UnitModeCurrent)` overwrites the 1 with 0. The machine boots in mode Undefined; `ModeConfigIdx = 200`; `DisabledStates` reads `dh[200]`, which init never writes (only `dh[201..203]`).

**Repair (either):**

- Set `copy(1, UnitModeCmd)` in an init rung before the `call(mode_change)`, or
- Guard the copy inside `mode_change` with `UnitModeCmd != 0`.

**Related regression:** `why UnitModeCurrent == 0` after scan 1 must name the `mode_change` copy as the clobbering writer, not the earlier init rung.

**Note:** `PTR_DEFAULT_BEFORE_BLOCK_START` fires on the adjacent pattern — `dh[ModeConfigIdx]` with `ModeConfigIdx` defaulting to 0, below the block start. The syntax-level rule and the `why()` runtime trace bracket the same bug family from both sides; worth a line in the docs.

### 1.3 EXECUTE has no completion row — decide and make explicit

**Problem:** task step 5 sets `StateCompleteBool = 1`, but `sm_state_complete2_request` has no `StateCurrent == S.EXECUTE` rung. Nothing transitions; `_CurStep` resets to 0 and the task restarts next scan. `COMPLETING` is reachable only via the external Complete command.

**Repair (either):**

- Add the `EXECUTE → COMPLETING` row to `sm_state_complete2_request`, or
- Comment that continuous-cycle behavior is intentional, and add a test asserting the task restart.

### 1.4 `StateTimer.Done` never consumed

**Problem:** the TON runs and `Acc` is reset on state transitions, but `Done` goes nowhere. Dead logic; pollutes coverage.

**Repair:** wire it (e.g., acting-state watchdog → an `AlarmCoil` slot) or remove the timer.

### 1.5 Regression tests to add

- `validate()` on the (preserved buggy) guard rung reports `RUNG_CONTRADICTION` with `BlockingRelation` naming the contradictory pair `UnitModeCmd < 1` / `UnitModeCmd > 3`, plus `RUNG_TAUTOLOGY` on the `Or` term with the residual condition.
- `how StateCurrent == S.HELD` from EXECUTE — coordinated `CmdHold` (level) + `rise(CmdChgRequest)` (edge) on the same scan; `CtrlCmd` is both external and program-written, so route redirection (`via=`) has two paths to `CtrlCmd == 4`.
- `how StateCurrent == S.COMPLETED` from cold start under `fold=True` — init fold, edge-gated Start, STARTING auto-advance, three 1 s TON dwells through the odd-step counter, Complete command.
- `why StateCurrent == S.ABORTED` via `LoopIndex > 10` overflow — attribution through the affine counter and the `ds[150 + StateRequested]` jump table.
- `how` into a mode-disabled state — planner must discover the `StateMask & DisabledStates` block, follow the jump-table redirect, and respect the LoopIndex bailout; acceptable outcomes are a correct redirect discovery or a loud budget exhaustion, never a silent wrong plan.

---

## Part 2 — New rule specifications

### RUNG_CONTRADICTION — condition simplifies to False

Per rung condition, run interval satisfiability over the existing `simplified()` / prover domain machinery (generalizing the `COIL_CONFLICTING_OUTPUT` exclusivity prover).

- Condition simplifies to `False` → the rung can never fire. **Error** under `strict=True`.
- Two grades, matching the stuck/stranded design language: **UNSAT** (unsatisfiable, period — no input sequence anywhere can fire it; zero false positives; error) vs **unreachable given initial conditions** (warning).
- Suppress on bare `rung()` (the intentional always-rung).
- Diagnostic carries the blocking pair(s), e.g. `UnitModeCmd < 1` / `UnitModeCmd > 3`.

### RUNG_TAUTOLOGY — subterm simplifies to True

- A subterm that simplifies to `True` (canonical case: `Or(x != a, x != b, x != c)` over one variable) contributes nothing; the rung's effective condition is the residual.
- **Report the residual explicitly** — "this rung's condition reduces to `UnitModeCmd < 1 AND UnitModeCmd > 3`" — half the diagnostic value is making the real gate visible before saying "unreachable."

### De Morgan repair-hint synthesis (attaches to RUNG_CONTRADICTION / RUNG_TAUTOLOGY)

When a finding involves one variable across flipped operators:

1. Compute the And↔Or dual over the offending terms.
2. Test the dual for satisfiability.
3. Emit `did you mean:` **only when** the original is degenerate (True/False) **and** the dual is informative (neither always-true nor always-false).
4. Include a plain-language description of exactly when the dual fires.

Confidence boosters:

- Single-variable interval contradiction (`x < a AND x > b`, a ≤ b) ⇒ almost certainly an intended union.
- Rung body containing `reset()` / `copy(0, …)` ⇒ rejection-rung prior; expect union-of-bad-cases shape.

Why a named check, not a generic unreachability warning: ladder has no group-negation primitive — series is And, parallel is Or, and "reject when NOT valid" forces the engineer to distribute the negation by hand across the diagram. The medium manufactures this exact bug on a schedule. Example diagnostic:

```
RUNG_CONTRADICTION main:12
  condition:  Or(State != IDLE, State != STOPPED, State != ABORTED)
              AND UnitModeCmd < 1 AND UnitModeCmd > 3
  simplifies: False — rung never fires
  note:       Or(State != IDLE, != STOPPED, != ABORTED) is tautological
  did you mean:
              Or(And(State != IDLE, State != STOPPED, State != ABORTED),
                 UnitModeCmd < 1, UnitModeCmd > 3)
  → dual is satisfiable and fires exactly on: state not in {IDLE, STOPPED,
    ABORTED} or command outside 1..3
```

---

### CMP_EQ_ON_MONOTONE — equality against a self-advancing register

- `==` / `!=` against a self-advancing register (Timer.Acc, counter accumulators). Source monotonicity and stride from the crossing registry (`Affine(source, scale, offset)`).
- `Timer.Acc == N` may step over the crossing (Acc advances by dt per scan; counters may increment by > 1). Suggest `>=` (or `<=` for down-counters).
- **Error level.** Also protects fold's bit-equal guarantee socially: the user who writes `==`, misses the crossing, and blames folding files the support ticket nobody wants.

### CMP_DIM_MISMATCH — time compared against unproven-time

- Comparison between a time-dimensioned operand (`Timer.Acc`, `td[]`) and an operand `classify_dimensions` cannot prove time-valued (no unit annotation, no provenance from another timer).
- Message must ask the unit question explicitly — ms vs sec vs counts — because Click `td` resolution depends on the timer's configured time base. This is the 1000× bug class: `Setpoint = 5` meaning seconds against an Acc counting milliseconds simulates fine if the test assumed the same wrong unit. **Warning.**

### CMP_HAND_ROLLED_DONE — reimplemented completion bit

- "Not yet" comparisons (`Setpoint > Timer.Acc` shape) where the comparand equals the timer's configured preset: the author has hand-rolled `~Timer.Done` with boundary risk at the exact-equal scan.
- Note that `Timer.Done` / `~Timer.Done` is the boundary-safe form.
- **Advisory only** (off by default or info-level). Direction heuristics are where lints become noise; only the preset-equals-comparand case is high-confidence, and it escalates through CMP_TRUE_AT_RESET anyway.

### CMP_TRUE_AT_RESET — condition true at the register's reset value

The core insight: for a monotone-from-zero register, evaluate the condition **at the reset value**. `Setpoint > Timer.Acc` with Acc = 0 is true on the first scan for any positive setpoint — it fires the instant the timer starts and turns *off* at the crossing, the exact complement of a completion check. Where Acc is reset on state transitions (as in the bench), the inverted comparison manufactures a spurious pulse on every state entry.

Detection and disposition:

- For any comparison over a monotone-from-zero register, evaluate at reset value (0).
- **True at reset + comparand matches the timer's preset** (magnitude or the preset register itself) → botched completion check. **Warning**, repair hint: `did you mean Timer.Acc >= Setpoint (or Timer.Done)?`
- **True at reset + `<=` form with small constant or Min-shaped comparand name** → the legitimate early-window idiom (debounce, minimum dwell, "not during the first N ms"). Rule stays quiet.
- Message states behavior on this program: "TRUE from the scan the timer starts, FALSE at the crossing; Acc resets on state transitions here, so this rung fires on every state entry."
- **No autofix.** Flipping `Setpoint > Timer.Acc` to `Timer.Acc >= Setpoint` is a semantics *change* (the complement), not a canonical form. This rule proposes repairs, never applies them.

### CMP_STATIC_ON_LEFT — operand-order convention with severity escalation

Convention: **the machine on the left, the expectation on the right, the operator pointing the way the value moves.** `Mode == 1`, not `1 == Mode`; `Timer.Acc >= Setpoint`, not `Setpoint <= Timer.Acc`. Every rung condition reads as a question about the machine — subject, verb, expectation — which is how the engineer thinks and how the CLICK contact reads.

"Static" means: **not written by the program and not self-advancing** — a literal, an `S.` constant, an HMI-only setpoint tag. Classification comes free from the writer-membership index. A program-written tag is dynamic even if it looks like a reference. Dynamic-vs-dynamic comparisons (`Task1._CurStep == StateJumpTarget`) are **exempt** — no canonical side exists.

Three-tier severity:

1. Static-on-left with `==` / `!=` → **info, auto-fixable.** Pure formatting; same predicate either way. The only true autofix in the rule set.
2. Static-on-left with an ordered operator, dynamic operand an ordinary tag → **warning**, suggest the flip (which requires reversing the operator); propose, don't apply.
3. Static-on-left with an ordered operator, dynamic operand a monotone-from-zero register → **escalate through CMP_TRUE_AT_RESET**: evaluate at reset, check comparand against preset, emit the behavioral message.

Why the convention earns enforcement: ordered comparisons on a moving register are where operand order and comparison direction get entangled — the author writes the operator thinking about magnitude ("setpoint is bigger") and accidentally encodes the pre-crossing window instead of the crossing. Normalized to *moving thing, threshold, crossing operator*, the wrong predicate becomes hard to type: `Timer.Acc < Setpoint` visibly says "while the timer hasn't reached it," and nobody writes that when they mean completion.

---

## Rule summary (new rules)

| Code | Trigger | Severity | Autofix |
|---|---|---|---|
| RUNG_CONTRADICTION | Condition simplifies to False | error (UNSAT) / warning (init-conditional) | no |
| RUNG_TAUTOLOGY | Subterm simplifies to True; report residual | warning | no |
| — De Morgan repair hint | One variable, flipped operators; dual satisfiable | attaches to RUNG_* | no |
| CMP_EQ_ON_MONOTONE | `==`/`!=` vs self-advancing register | error | no |
| CMP_DIM_MISMATCH | Time vs unproven-time operand | warning | no |
| CMP_HAND_ROLLED_DONE | "Not yet" comparison, comparand = preset | advisory | no |
| CMP_TRUE_AT_RESET | True at reset value; window-idiom suppression | warning (preset match) | no |
| CMP_STATIC_ON_LEFT | Static operand on left | info → warning → CMP_TRUE_AT_RESET escalation | `==`/`!=` only |

## Release-notes material

- The guard-rung bug, exactly as it happened: shipped in our own benchmark, written by the author, caught by the analysis. Include the RUNG_CONTRADICTION diagnostic output verbatim. Skeptical controls engineers trust confessions over claims.
- The rename: one table (old → new), the `CORE_*` deprecation-alias policy, and the new `assert not report.errors()` idiom.
- Theme line for the validator expansion: *the diagram compiles; the validators tell you whether it means what it says.*


## Others

- **RUNG_DUPLICATE** — identical condition + body across two rungs (copy-paste never differentiated)
- **CALC_DUPLICATE** — two math/copy instructions with structurally identical expression body, same or different destination
- **COIL_WRITE_IN_UNCALLED_SUB** — tag written inside a subroutine that's never `call()`ed
- **CTR_LEVEL_DRIVEN** — counter count-up/down inside a rung with no edge detection on the counting path
- **CMP_OVERFLOW_ON_ARITHMETIC** — arithmetic result provably exceeds destination tag's declared range or integer width
- **RUNG_NO_EFFECT** — rung with conditions but empty body (no output instruction)
- **TAG_CLOBBER_ACROSS_CALL** — tag written before `call()`, callee unconditionally writes same tag
- **SCAN_DEPENDENT_NOT_MANAGED** — timer, `out()` coil, or counter lives in a sub not called from every branch of a mutually exclusive call group; retains state silently on transition
