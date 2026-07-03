# Validator subsystem refactor plan (Phase 3, pre-new-rules)

Prepared before implementing the Part 2 rules. Goal: get the validator scaffolding
into a shape that absorbs 7 new rules + category-prefixed codes + severity **without**
making the existing hand-maintained dispatch worse. Three tasks, done in this order:
**#8 severity → #7 registry → #9 rename**. Rationale for the order at the end.

All file:line references verified against the current tree (branch `dev`).

---

## 0. Current state (grounded)

**Ten codes across eight validator modules**, each module defining its own
`XxxFinding` + `XxxReport(summary())` pair:

| Module | Code(s) emitted | Notes |
|---|---|---|
| `readonly_write.py` | `CORE_READONLY_WRITE` | |
| `choices_violation.py` | `CORE_CHOICES_VIOLATION` | |
| `final_writers.py` | `CORE_FINAL_MULTIPLE_WRITERS` | |
| `duplicate_out.py` | `CORE_CONFLICTING_OUTPUT` | the exclusivity prover |
| `stuck_bits.py` | `CORE_STUCK_HIGH`, `CORE_STUCK_LOW` | **multi-code** |
| `pointer_default.py` | `CORE_POINTER_DEFAULT_BEFORE_BLOCK_START` | |
| `physical_realism.py` | `CORE_RANGE_VIOLATION`, `CORE_MISSING_PROFILE`, `CORE_ANTITOGGLE` | **multi-code**, takes `dt` |

**Pain points the new batch will amplify:**

1. `validate()` (`report.py:104-127`) is a hand-maintained if-ladder. Multi-code
   validators (`stuck_bits`, `physical_realism`) need a manual `for f in ...: if f.code
   in active` filter loop — **two** such loops today, one per multi-code validator.
   Seven new rules (five of them `CMP_*` from one comparison-analysis pass) make this
   strictly worse.
2. `ALL_RULES` is duplicated: the authoritative frozenset in `report.py:21-34`, plus the
   individual `CORE_*` constants re-exported through `__init__.py`, plus a hardcoded copy
   in `tests/validators/test_validate_report.py:167` (`test_all_rules_constant_complete`).
3. No severity. `Finding` protocol (`report.py:13-18`) carries only `code`,
   `target_name`, `message`. The advisory rules (`CMP_HAND_ROLLED_DONE`) will break the
   documented `assert not report` idiom the moment they land — one style note fails CI.
4. `Program.validate()` facade (`_program.py:209-240`) accepts `mode="warn"` but **ignores
   it for the core path** (only forwards `select`/`ignore`/`dt`). `mode` currently only
   affects dialect validation. The spec's `strict=True` (RUNG_CONTRADICTION as error) has
   no wiring yet.
5. `select`/`ignore` only accept exact codes; no category selection. `_resolve_rules`
   (`report.py:62-72`) raises `ValueError` on anything not in `ALL_RULES`.

---

## 1. Task #8 — Severity (do first; additive, lowest risk)

### 1.1 Severity vocabulary

```python
# pyrung/core/validation/severity.py  (new, tiny)
from typing import Literal
Severity = Literal["error", "warning", "info", "advisory"]
_ORDER = {"error": 3, "warning": 2, "info": 1, "advisory": 0}
```

Meaning (from the spec's table): **error** = provably wrong, no reasonable intent fits;
**warning** = high-confidence bug pattern; **info** = convention, auto-fixable where
semantics-preserving; **advisory** = off-by-default / heuristic too weak for warning.

### 1.2 Protocol + finding dataclasses

Add `severity: Severity` to the `Finding` protocol (`report.py:13`). Then add a
`severity` field to each of the 8 `XxxFinding` dataclasses, defaulted to the rule's
intrinsic level so **no call site changes**:

```python
@dataclass(frozen=True)
class ConflictingOutputFinding:
    code: str
    target_name: str
    sites: tuple[OutputSite, ...]
    message: str
    severity: Severity = "error"          # <-- added, defaulted
```

Defaults per current rule (proposed): CONFLICTING_OUTPUT=error, STUCK_HIGH/LOW=warning,
READONLY_WRITE=error, CHOICES/RANGE=error, FINAL_MULTIPLE_WRITERS=warning,
POINTER_DEFAULT=warning, MISSING_PROFILE=info, ANTITOGGLE=warning. (These become the
registry's default severities in #7; for #8 they live as field defaults.)

Rules that vary severity per finding (future RUNG_CONTRADICTION: error when UNSAT,
warning when only unreachable-given-init) pass `severity=` explicitly at construction.

### 1.3 ValidationReport filters

```python
def errors(self)   -> tuple[Finding, ...]: return tuple(f for f in self.findings if f.severity == "error")
def warnings(self) -> tuple[Finding, ...]: ...
def infos(self)    -> tuple[Finding, ...]: ...
def advisories(self)-> tuple[Finding, ...]: ...
def has_errors(self)-> bool: return any(f.severity == "error" for f in self.findings)
```

Keep `__bool__ == bool(findings)` for backward compat. New recommended idiom, documented
and migrated in the test-heavy files: **`assert not report.errors(), report.summary()`**.
Extend `summary()` to break down by severity as well as code.

### 1.4 Test impact
- No existing test breaks (severity is additive, `__bool__` unchanged).
- Add targeted tests: severity present on each finding; `errors()`/`warnings()` partition.

---

## 2. Task #7 — Rule registry (replace the if-ladder)

### 2.1 Data model

A validator may emit several codes, so keep **two** maps: per-code metadata, and a
callable per validator. `validate()` becomes a planner, not an if-ladder.

```python
# pyrung/core/validation/registry.py  (new)
@dataclass(frozen=True)
class RuleSpec:
    code: str
    category: str            # "TAG" | "COIL" | "PTR" | "PHYS" | "RUNG" | "CMP"
    severity: Severity       # default severity for this code
    validator: str           # key into VALIDATORS
    default_on: bool = True  # advisory rules default_on=False
    aliases: tuple[str, ...] = ()   # deprecated old codes (filled in #9)

RULES: dict[str, RuleSpec] = { ... }          # code -> spec
VALIDATORS: dict[str, Callable[..., Sequence[Finding]]] = { ... }  # key -> callable

CATEGORIES = frozenset(spec.category for spec in RULES.values())
ALL_RULES = frozenset(RULES)                  # single source of truth
```

`validators` values wrap the existing `validate_*` functions (unchanged signatures;
`physical_realism` marked as needing `dt` — pass via a small run-context or `functools.partial`).

### 2.2 The planner

```python
def validate(program, *, select=None, ignore=None, dt=0.010) -> ValidationReport:
    active = _resolve_rules(select, ignore)            # frozenset[str]
    if not active: return ValidationReport(())
    needed = {RULES[c].validator for c in active}       # dedup validators
    findings: list[Finding] = []
    for key in _VALIDATOR_ORDER:                        # stable, deterministic order
        if key not in needed: continue
        for f in VALIDATORS[key](program, dt=dt):       # dt ignored by validators that don't take it
            if f.code in active:
                findings.append(f)
    return ValidationReport(tuple(findings))
```

The two ad-hoc `if f.code in active` loops collapse into the one filter in the planner —
uniform for single- and multi-code validators. Adding a rule = one `RULES` entry (+ a
`VALIDATORS` entry only if it's a new pass). The `CMP_*` family is one validator emitting
five codes: one `VALIDATORS` entry, five `RULES` entries.

### 2.3 Category selection (enabled by the registry)

`_resolve_rules` gains category + alias expansion:

```python
def _expand(token: str) -> set[str]:
    if token in RULES:        return {token}
    if token in CATEGORIES:   return {c for c, s in RULES.items() if s.category == token}
    if token in _ALIASES:     warn_deprecated(token); return {_ALIASES[token]}   # #9
    raise ValueError(f"Unknown rule code or category: {token!r}")
```

So `select={"CMP"}` runs the whole comparison-semantics bucket; `ignore={"PHYS"}` drops the
dt-aware realism rules. `default_on=False` rules (advisories) are excluded from the implicit
"all" set (when `select is None`) but selectable explicitly by code or category.

### 2.4 Test impact
- `test_all_rules_constant_complete` moves to asserting `ALL_RULES == set(RULES)` (or is
  regenerated from the registry). No more hardcoded literal drift.
- Behavior-preserving for existing codes/tests **until** #9 renames them.

---

## 3. Task #9 — Rename with deprecation aliases

### 3.1 Mapping (from the spec's Part 0 table)

```
CORE_READONLY_WRITE                    -> TAG_READONLY_WRITE
CORE_CHOICES_VIOLATION                 -> TAG_CHOICES_VIOLATION
CORE_RANGE_VIOLATION                   -> TAG_RANGE_VIOLATION      (TAG, not PHYS; dt-forward is impl detail)
CORE_FINAL_MULTIPLE_WRITERS            -> TAG_FINAL_MULTIPLE_WRITERS
CORE_CONFLICTING_OUTPUT                -> COIL_CONFLICTING_OUTPUT
CORE_STUCK_HIGH                        -> COIL_STUCK_HIGH
CORE_STUCK_LOW                         -> COIL_STUCK_LOW
CORE_POINTER_DEFAULT_BEFORE_BLOCK_START-> PTR_DEFAULT_BEFORE_BLOCK_START
CORE_MISSING_PROFILE                   -> PHYS_MISSING_PROFILE
CORE_ANTITOGGLE                        -> PHYS_ANTITOGGLE
```

New (registered now, emitted when Part 2 lands): `RUNG_CONTRADICTION`, `RUNG_TAUTOLOGY`,
`CMP_EQ_ON_MONOTONE`, `CMP_DIM_MISMATCH`, `CMP_HAND_ROLLED_DONE`, `CMP_TRUE_AT_RESET`,
`CMP_STATIC_ON_LEFT`.

### 3.2 Alias policy

Each renamed `RuleSpec` lists its old code in `aliases`. Build `_ALIASES = {old: new}` from
the registry. `select`/`ignore` accept old codes for **one release** (through v0.11.0),
emitting a `DeprecationWarning` and mapping to the new code; removed the release after.
The `code` field on emitted findings is **always the new code** (no dual emission).

The module-level constants (`CORE_STUCK_HIGH = "CORE_STUCK_HIGH"` etc.) become the new
strings; keep the old names as assignments to the new value for import compatibility for one
release (`CORE_STUCK_HIGH = COIL_STUCK_HIGH`), or drop them if no external importers — decide
per grep of downstream usage.

### 3.3 Test / doc impact (the behavior-changing step)
- `test_all_rules_constant_complete` → new code set.
- Any test doing `select={"CORE_..."}` keeps working via aliases (add an explicit
  alias-still-works test + a `pytest.warns(DeprecationWarning)` test).
- `tests/validators/test_rung_contradiction.py` xfails start referencing real registered
  codes (they already name `RUNG_CONTRADICTION`/`RUNG_TAUTOLOGY`).
- Docs: `CHANGELOG.md` (rename table + alias policy), the analysis/verification guides, and
  the `assert not report.errors()` idiom. `make lint` runs codespell — keep the table tidy.

---

## 4. Task #10 — Satisfiability: NO new solver work (both RUNG rules are wiring)

Both claims below are verified empirically against the existing machinery.

**`RUNG_CONTRADICTION`** — `_conjunction_satisfiable(guard_rung._conditions)` already returns
`False`. Pure detection-wiring: walk rungs, call the existing solver, emit + name the pair.

**`RUNG_TAUTOLOGY`** — my earlier "needs disjunction reasoning" scoping was **wrong**. The
De Morgan identity dissolves it:

> `Or(t₁, …, tₙ) ≡ True`  ⟺  `¬Or(…) ≡ False`  ⟺  `And(¬t₁, …, ¬tₙ)` is UNSAT.

The existing conjunction solver already proves the UNSAT side — `And(x==4, x==2, x==9)` trips
`_tag_domain_feasible`'s "two different equality pins" guard (`_common.py:444-446`). Verified:

```
buggy  Or(x!=4, x!=2, x!=9)  ->  negate -> And(x==4,x==2,x==9) UNSAT  ->  TAUTOLOGY  ✓
real   Or(x!=4, x<2)         ->  not unsat                          ->  not taut    ✓ (no FP)
bool   Or(B, ~B)             ->  And(B, ~B) UNSAT                    ->  TAUTOLOGY   ✓
```

So `_tag_domain_feasible` needs **no** disjunction path. The only genuinely new code is a
~10-line condition-level `negate_leaf` (Eq↔Ne, Lt↔Ge, Le↔Gt, Bit↔NormallyClosed) — a direct
mirror of the existing `simplified._negate` (`simplified.py:610-639`), which does the same
flips one representation over (on `Expr`/`Atom` instead of `Condition`).

Soundness refinement for opaque terms (edges, indirect, arith — `negate_leaf` returns `None`):
**drop them and test the remainder**. `Or(A, B)` proven tautological ⟹ `Or(A, B, C)` is too
(extra disjuncts only add truth). An empty remainder (all terms opaque) ⇒ not provably a
tautology. Conservative in the safe direction: zero false positives.

**Action:** promote `_conjunction_satisfiable` / `_tag_domain_feasible` from private to a
documented internal API (e.g. `pyrung/core/validation/sat.py`) and add `negate_leaf` beside
them. The RUNG_* and CMP_* rules all consume it. The spec's "detection wiring, not solver
work" is now correct for **both** RUNG rules — and the De Morgan repair-hint ("did you mean
`Or(And(...), <1, >3)`") reuses the exact same negate + sat-check to test the dual.

### 4.1 Concrete helpers (`pyrung/core/validation/sat.py`)

Move `_conjunction_satisfiable`, `_tag_domain_feasible`, `_conditions_contradict`,
`_flatten_and_conditions`, `_tag_name` out of `_common.py` into `sat.py` (re-export from
`_common` for one release so `simplified.reset_dominance` and `duplicate_out` keep importing
the old path). Add the two new primitives:

```python
# pyrung/core/validation/sat.py
from pyrung.core.condition import (
    AnyCondition, BitCondition, NormallyClosedCondition,
    CompareEq, CompareNe, CompareLt, CompareLe, CompareGt, CompareGe,
)

# Complementary comparison classes — the Condition-level twin of simplified._negate's
# atom-form flips.  Symmetric, so one dict covers both directions.
_COMPARE_COMPLEMENT = {
    CompareEq: CompareNe, CompareNe: CompareEq,
    CompareLt: CompareGe, CompareGe: CompareLt,
    CompareLe: CompareGt, CompareGt: CompareLe,
}


def negate_leaf(cond):
    """The logical complement of a leaf Condition, or None if opaque.

    None means "can't negate statically" (edges, indirect/arith compares,
    AnyCondition/AllCondition) — callers treat that conservatively.
    """
    cls = type(cond)
    comp = _COMPARE_COMPLEMENT.get(cls)
    if comp is not None:
        return comp(cond.tag, cond.value)
    if isinstance(cond, BitCondition):
        return NormallyClosedCondition(cond.tag)
    if isinstance(cond, NormallyClosedCondition):
        return BitCondition(cond.tag)
    return None


def disjunction_tautological(terms) -> bool:
    """True when Or(*terms) is provably always-true.

    Or(t1..tn) ≡ True  ⟺  And(¬t1..¬tn) is UNSAT.  Opaque terms (negate_leaf
    → None) are DROPPED, not failed: proving Or over a subset tautological
    proves it for the whole (extra disjuncts only add truth).  An empty
    surviving set ⇒ not provably tautological (return False).
    """
    negated = [n for t in terms if (n := negate_leaf(t)) is not None]
    if not negated:
        return False
    return not conjunction_satisfiable(negated)   # renamed public alias
```

`conjunction_satisfiable` is the promoted (de-underscored) `_conjunction_satisfiable`.

### 4.2 Rule detection sketches (Part 2, shown here to prove the wiring)

```python
# RUNG_CONTRADICTION — walk every rung; flag when its condition conjunction is UNSAT.
def _rung_contradiction(program, dt=0.010):
    findings = []
    for loc, rung in _iter_rungs(program):          # (already have walkers in _common)
        conds = tuple(rung._conditions)
        if not conds:                                # bare rung() is the intentional always-on
            continue
        if not conjunction_satisfiable(conds):
            pair = _blocking_pair(conds)             # first UNSAT pair, for the message
            findings.append(RungFinding(
                code="RUNG_CONTRADICTION", target_name=loc,
                severity="error",                    # UNSAT anywhere → error (spec §2)
                message=_contradiction_message(loc, conds, pair),
                repair=_demorgan_hint(conds),        # §4.3
            ))
    return findings

# RUNG_TAUTOLOGY — flag Or-subterms that contribute nothing; report the residual.
def _rung_tautology(program, dt=0.010):
    findings = []
    for loc, rung in _iter_rungs(program):
        conds = tuple(rung._conditions)
        taut = [c for c in conds if isinstance(c, AnyCondition)
                and disjunction_tautological(c.conditions)]
        if not taut:
            continue
        residual = tuple(c for c in conds if c not in taut)
        findings.append(RungFinding(
            code="RUNG_TAUTOLOGY", target_name=loc, severity="warning",
            message=_tautology_message(loc, taut, residual),  # "reduces to <residual>"
        ))
    return findings
```

### 4.3 De Morgan repair hint (same primitives)

For a degenerate rung (contradiction or tautology) over one variable with flipped operators,
compute the And↔Or dual and test *it* for satisfiability; emit `did you mean:` only when the
original is degenerate **and** the dual is informative (neither always-true nor always-false):

```python
def _demorgan_hint(conds):
    dual = _and_or_dual(conds)                       # And(a,b,c) <-> Or(a,b,c) at top level
    if conjunction_satisfiable(_as_conjunction(dual)) and not disjunction_tautological(dual):
        return DemorganHint(dual=dual, fires_on=_describe(dual))
    return None
```

This is entirely negate + `conjunction_satisfiable` + `disjunction_tautological` — no new
solver. `_blocking_pair`, `_and_or_dual`, `_describe` are pure structural helpers.

### 4.4 Proof-of-concept before Part 2 (optional, cheap)

`tests/validators/test_rung_contradiction.py` already fixtures the buggy guard rung with four
`xfail(strict=True)` assertions. A ~30-line `sat.py` (just `negate_leaf` +
`disjunction_tautological` beside the promoted `conjunction_satisfiable`) is enough to flip
the two `RUNG_TAUTOLOGY` xfails green via a direct helper call, proving the reduction on the
real specimen before any registry/rename churn. Keep the `validate()`-facing xfails red until
the rule is registered (task #7/#9).

---

## 5. Order, risk, coordination

**Order: #8 → #7 → #9.**
- #8 severity is additive and unblocks the advisory rules; nothing else depends on the
  registry existing first. Smallest, safest, lands independently.
- #7 registry is behavior-preserving (same codes, same findings) — a pure internal
  restructure. Doing it after severity means the `RuleSpec.severity` field has a home.
- #9 rename is the only behavior-changing step (codes are load-bearing). Do it last, behind
  the registry (one place to edit), with aliases + CHANGELOG + doc sweep.

**Coordination:** this touches shared code across 8 validator modules + `report.py` +
`__init__.py` + the `_program.py` facade + `tests/validators/*`. Per the "others share the
working tree" constraint: stage by path, keep `make test-prove` / `make` green after each of
the three steps, and land them as three separate commits so a bisect can isolate the rename.

**Explicitly out of scope here:** implementing the 7 rules, `pyrung lint` CLI, and the
`strict=`/`mode=` escalation wiring — those are the "next batch" this review clears the way
for. Note only: once severity exists, `strict` is just "do warnings also fail?", a thin
`report.has_errors()` vs `bool(report)` choice at the call site.
```
