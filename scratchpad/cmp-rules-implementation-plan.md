# CMP rules — finalized implementation plan

Companion to `lint-spec-and-bench-fixes.md` Part 2. Decisions locked with the user
(2026-07-05):

- **Drop `CMP_HAND_ROLLED_DONE`** — advisory, low-confidence, and its one high-value
  case (comparand == preset) is already caught by `CMP_TRUE_AT_RESET`.
- **One module, one batch** — all rules in `cmp_conditions.py`, wired together.
- **`CMP_DIM_MISMATCH` deferred** — no physical-unit/UoM system exists (see below);
  spec it separately once a real time-proof helper is designed.

Net batch: **three rules** — `CMP_EQ_ON_MONOTONE`, `CMP_TRUE_AT_RESET`,
`CMP_STATIC_ON_LEFT`.

Template throughout: `rung_conditions.py` (module) + `test_rung_contradiction.py`
(tests). No new solver — everything stands on `AccProfile` and the writer index.

---

## Machinery (confirmed APIs)

| Need | API | Path |
|---|---|---|
| Monotone-from-zero register: accumulator, preset, done, reset, direction, rate | `Instruction.accumulating_profile() -> AccProfile` | `core/instruction/accumulating.py:51` |
| Preset from an `.Acc` operand (alt/richer, prove-side) | `_collect_done_acc_pairs(program) -> _DoneAccInfo{pairs, presets, preset_tags, kinds}` | `core/analysis/prove/absorb.py:102` |
| Program-written (dynamic) vs static | `{s.target_name for s in _collect_write_sites(program)}` | `core/validation/_common.py:223` |
| Instruction walk (main+branches+subs) | `walk_instructions(program)` | `core/validation/_common.py:195` |
| Rung/branch/sub walk | `_iter_rungs(program)` — **lift from `rung_conditions.py:120` to `_common.py`** | shared |
| Compare classes + negation | `sat.negate_leaf`, `_COMPARE_COMPLEMENT` | `core/validation/sat.py` |

**Prefer `accumulating_profile()`** over the prove-side `_DoneAccInfo`: it's the
neutral in-package layer (no `validation → prove` coupling), and it already carries
`reset` (needed by TRUE_AT_RESET) and `direction`/`rate_per_scan` (needed by
EQ_ON_MONOTONE). Only reach into `absorb`/`classify` if DIM_MISMATCH lands.

**No UoM system.** `_classify_dimensions` (`prove/classify.py:2602`) classifies
BFS state-space axes, not physical units. "Time-valued" = the operand tag is a
timer/counter accumulator (appears as `profile.accumulator`). That's why
DIM_MISMATCH is deferred — its confidence rests entirely on that one signal.

---

## Shared foundation (`cmp_conditions.py`)

Build once per `validate_cmp_conditions(program)` call:

1. **`_iter_compares(program)`** — reuse `_iter_rungs` + `_flatten_and_conditions`,
   then descend `AnyCondition`/`AllCondition` to yield every comparison, in **both**
   families:
   - `CompareEq|Ne|Lt|Le|Gt|Ge` (`condition.py:78+`) — leaf, `.tag` (Tag) vs
     `.value` (Tag / `ImmediateRef` / literal). `CalculatedTag > 5` is this shape.
   - `ExprCompare(left, right, op, symbol)` (`condition.py:319`, `expression.py:319`)
     — `.left`/`.right` are `Expression` trees (`BinaryExpr`/`UnaryExpr`/wrapped
     tag or const). This is what inline arithmetic `(A + B) > 5` produces; **must be
     walked or `5 < (A + B)` slips through STATIC_ON_LEFT.**

   Normalize each to `(left_operand, op, right_operand)` so both families feed one
   classifier. Resolve both sides — either can be tag, literal, or computed expr.
2. **`_acc_index(program)`** — `{profile.accumulator.name: profile}` over
   `walk_instructions` where `accumulating_profile()` is present.
3. **`_written(program)`** — the writer-name set.

Helper `_monotone_operand(compare, acc_index)` returns
`(register_side, profile, comparand)` when exactly one side is a keyed accumulator,
else `None`. Register can sit on **either** side — check `compare.tag` and
`compare.value`.

Shared `_true_at_reset(compare, profile, comparand) -> Finding | None` so both the
direct TRUE_AT_RESET pass and STATIC_ON_LEFT tier-3 call one implementation and
**dedup** by `(location, code)` (never double-report the same rung).

---

## Rule 1 — `CMP_EQ_ON_MONOTONE` (error)

- Trigger: `CompareEq`/`CompareNe` where one side is a keyed accumulator.
- Repair hint by `profile.direction`: `>=` (up) / `<=` (down), or `Timer.Done`.
- Message (behavioral): "Acc advances by `{rate_per_scan}` per scan and may step over
  `{comparand}` between scans; the `==` can be missed. Use `Acc >= N` (or the
  timer's Done bit)."
- Zero-false-positive: pure structural match against the index.

## Rule 2 — `CMP_TRUE_AT_RESET` (warning)

- Trigger: ordered compare (`Lt/Le/Gt/Ge`) with a monotone-**from-zero** register.
- Evaluate the predicate at `Acc = 0`.
  - **True at reset + comparand matches preset** (`profile.preset` magnitude or the
    preset register itself) → botched completion check. **warning**, hint:
    `did you mean Acc >= Setpoint (or Timer.Done)?`
  - **True at reset + `<=`/`<` form with a small literal or Min-named comparand** →
    legitimate early-window idiom (debounce / min-dwell). **Suppress.**
- Message includes the reset fact when `profile.reset` is not None:
  "TRUE from the scan the timer starts, FALSE at the crossing; Acc resets on state
  transitions here, so this fires on every state entry."
- **No autofix** — flipping the operator is a semantics change (the complement),
  not a canonical form. Propose, never apply.

## Rule 3 — `CMP_STATIC_ON_LEFT` (info / warning-KNOWN / advisory-MAYBE)

Convention: the moving/computed thing on the left, the expectation on the right.
The classifier is **operand-type, not timer-specific** — it generalizes to any
calculated value, e.g. `CalculatedTag > Literal` (correct, no finding).

**`_dynamic(operand, written, acc_index)`** — an operand is *dynamic* (belongs on
the left) when it is any of:
- a **program-written tag** — `operand.name in written` (calc / copy / math / coil /
  counter / timer destinations);
- a **self-advancing register** — `operand.name in acc_index`;
- a **computed expression** — a `BinaryExpr`/`UnaryExpr` operand of an `ExprCompare`
  (inherently derived, so `(A + B)` reads as the subject).

An operand is *static* when it is none of those: a literal / `ImmediateRef`
constant, an `S.` enum constant, or **any never-written tag** — including an external
sensor input *and* an external HMI setpoint.

> **No `external` special-casing — grade by confidence instead.** An earlier attempt
> classified `external=True` as dynamic to dodge the `sensor < band` false positive.
> That over-corrected (it silently exempted a genuinely-backwards `Setpoint <
> RunningValue`) and papered over the real ambiguity: a live *measurement* and a
> *threshold* are structurally indistinguishable when neither is an accumulator. The
> honest design leaves classification coarse and lets **severity carry the
> confidence** (see Rule 3): if an accumulator anchors the comparison we can *prove*
> the mover → warning (KNOWN); if it's two ordinary tags we're *guessing* →
> advisory (MAYBE), out of the `errors()`/`warnings()` gate.

**Calculated-provenance signal (message/confidence only, not the verdict).** We can
prove an operand is specifically the *product of a calculation* — a stronger subset
of "dynamic":
- stored: `{i.dest.name for i in walk_instructions(program) if isinstance(i,
  CalcInstruction)}` (`instruction/calc.py:108` — `calc()`, arithmetic+wrap; distinct
  from `CopyInstruction`, a clamped move);
- inline: `ExprCompare` operand that is a `BinaryExpr`/`UnaryExpr`;
- (equivalently, a non-identity `Affine` from the calc crossing, `crossings/calc.py`).

Used to **sharpen the message** ("the right operand is a *calculated* value; put the
calculation on the left") and to **hold warning confidence** — but the dynamic-on-
left verdict stays driven by the broader written∪register∪computed set (a
`copy(0, x)` target is still "the machine" and belongs on the left).

Rule fires when **left is static and right is dynamic**, and per-finding severity
grades the confidence (findings carry their own `.severity`; the `RuleSpec` default
is `advisory`):
- **Exempt dynamic-vs-dynamic** — both computed/written → no canonical side
  (`CalcA > CalcB`, `Task1._CurStep == StateJumpTarget`).
- `==` / `!=` → **info** — cosmetic, the predicate is identical either way.
- Ordered operator, right side a **monotone register** → this is **KNOWN**: the
  accumulator is provably the mover. If it is *true at reset* (`Setpoint > Acc`,
  preset match) `_true_at_reset(...)` claims it as `CMP_TRUE_AT_RESET`; otherwise
  (`Setpoint < Acc` — false at reset, backwards "reached" check) it stays a
  `CMP_STATIC_ON_LEFT` **warning** ("the accumulator … belongs on the left; write
  `Acc > Setpoint`"). Either way an accumulator-anchored order issue is gated.
- Ordered operator, right side an **ordinary tag / computed expr** → this is a
  **MAYBE**: a live measurement and a threshold are indistinguishable to the
  analyzer, so it emits an **advisory** with hedged wording ("if `X` is the moving
  value, write `X > Y`"). Advisory is out of `errors()`/`warnings()` — it surfaces
  in the full report but never fails the gate.
- Correct-form no-finding cases (dynamic on left): `CalculatedTag > Literal`,
  `(A + B) > 5`, and the bench control `examples/packml_bench.py:764`
  `TaskTimer.Acc > Task1.Limit_Ts`.

Why grade rather than suppress: the sensor false positive (`S_DryerTemp_F <
S_ValLowBandTemp_F`) and the missed `Setpoint < RunningValue` are the same
unprovable ambiguity. Grading keeps both visible-but-honest instead of guessing —
KNOWN issues gate, MAYBE issues advise. No autofix (no fix framework; out of scope).

---

## Registry + report wiring (mechanical)

- `registry.py`: 3 `RuleSpec`s, category `"CMP"`, validator key `"cmp"`:
  - `CMP_EQ_ON_MONOTONE` / error / "Equality vs Self-Advancing Register"
  - `CMP_TRUE_AT_RESET` / warning / "Comparison True at Reset Value"
  - `CMP_STATIC_ON_LEFT` / advisory / "Static Operand on Left" (per-finding severity
    ranges info → advisory → warning; `advisory` is the display default)
- Append `"cmp"` to `VALIDATOR_ORDER` (after `"rung"`).
- `report._validator_dispatch`: `"cmp": lambda: validate_cmp_conditions(program).findings`.
- `__init__.py`: export codes + `CmpConditionFinding`/`Report`/`validate_cmp_conditions`.

## Tests (`tests/validators/`)

Mirror `test_rung_contradiction.py`: docstring cites spec §, `_*_program()` factory
with `Program(strict=False)`, `TestFixtureReproduces*` always-pass anchors +
`validate()`-facing assertions.

- `test_cmp_monotone.py` — specimen `Timer.Acc == Setpoint`; assert error + `>=` hint.
- `test_cmp_true_at_reset.py` — specimen `Setpoint > Timer.Acc` with preset match +
  reset condition; assert warning + behavioral message. Negative: `<= small-const`
  early-window stays quiet.
- `test_cmp_static_on_left.py`:
  - tier-1 `1 == Mode` → info
  - tier-2 `Setpoint <= CalcTag` → warning, flip hint
  - tier-2 computed-expr `5 < (A + B)` (`ExprCompare`) → warning, flip to `(A+B) > 5`
  - tier-3 `Setpoint > Timer.Acc` → escalates to CMP_TRUE_AT_RESET (true at reset)
  - tier-3 fallback `Setpoint < Timer.Acc` → not true at reset → tier-2 flip warning
  - tier-3 `Setpoint >= Counter.Acc` → counters keyed the same as timers
  - negative controls (dynamic on left, no finding): `CalculatedTag > Literal`,
    `(A + B) > 5`, bench:764 `TaskTimer.Acc > Task1.Limit_Ts`
  - exempt dynamic-vs-dynamic: `CalcA > CalcB` → no finding

---

## Deferred: `CMP_DIM_MISMATCH`

Needs a time-proof helper: operand is time-valued iff it's a timer/counter
accumulator (`profile.accumulator`, or `done_acc_pairs` value) — there is no unit
vocabulary to lean on. Flag when one side is time-proven and the other is a bare
`Int` with no such provenance. Lowest confidence in the set; spec + build after the
three core rules land.
