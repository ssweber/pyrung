# Retire `choice=` → `how()` reports the route it took, engineer redirects with `avoid=`/`via=`

Design direction agreed in conversation (2026-06-30). Supersedes the
"collapse vs surface" framing in `DESIGN.md`. Scratchpad only — other agents in
the tree; nothing built.

## The reframe

The surface-decision was keyed on *structure* ("internal coil / multi-writer")
but justified by *materiality* ("a distinct machine configuration"). Those
diverge — the Burner stays surfaced for a proxy reason. The right axis is **what
the engineer already knows**: they know their machine. They don't need PILOT to
hand them opaque positional options *before* answering. They need PILOT to:

1. **always reach the goal** via a deterministic default route,
2. **tell them where it went**, in terms they recognize,
3. **accept a semantic redirect** — `avoid=ProdMode`, `via=MaintMode` — and re-plan.

Outcome space collapses `{reachable-via-X, ambiguous, unreachable}` →
`{reached-via-X, unreachable}`. "Ambiguous" disappears: when the route is
genuinely opaque to the *engineer* too (a computed jump table), PILOT can't
enumerate meaningful options anyway, so surfacing was empty.

```
# today
how(Burner)            -> ambiguous, choices=[ProdMode, MaintMode]   # BLOCKS
how(Burner, choice=1)  -> reached

# proposed
how(Burner)               -> reached via ProdMode  (alt: MaintMode)  # never blocks
how(Burner, avoid=ProdMode)  -> reached via MaintMode
how(Burner, via=MaintMode)   -> reached via MaintMode
```

Already half-built: the OR-arm fix (`57a1b60`) *auto-resolves* and only
name-drops `choice=` as an escape hatch. DiverterCmd already does "go, then
you'd redirect." This reframe just drops the remaining Burner-style hard-surface.

## What stays, what moves

- **Default selection** keeps the work from `DESIGN.md`: among viable routes pick
  the cheapest by `_trace_score`; the retentive + input-gated gates decide *which*
  route is the sensible default (and whether a route is even a safe establish).
  It is no longer a *collapse-vs-surface* test — it is *default-vs-pivot*.
- **The route labels** (`_choice_label`, `_writer_label`, `TraceChoice.label`)
  are **repurposed from "describe alternatives" to "describe the taken route +
  its roads not taken."** No label work is thrown away; its consumer changes.
- **`enumerate_trace_choices`** still enumerates the forks — but the caller
  records the *unchosen* arms as `RoutePivot.alternatives` instead of returning
  an ambiguous `Path`. Every fork that had ≥2 viable options becomes a pivot.
- **`avoid=`** already exists (`c8d971b`, `_route_forces`). **`via=`** is its
  documented complement — built next, same primitive, opposite filter.
- **`choice=`** is **removed outright** — no deprecation release. The
  `choice=` param on `runner.how()` and the `choice`/`trace_choice` threading
  through `pilot_how`/`pilot_drive`/`pilot_events`/`_prepare_trace_choice` all
  go. `avoid=`/`via=` are the only route controls. (Internal `TraceChoice` /
  `enumerate_trace_choices` stay — they now feed `route.pivots`, not a user
  param.)

## The one new load-bearing piece: a legible taken-route on the `Path`

Redirect only works if "here's where I went" names something the engineer can
target. The bet is **transparency replaces enumeration** — so the `Path` must
carry the route, not just the input steps.

### Sketch — new types (in `core/analysis/graph.py`, beside `Path`)

```python
@dataclass(frozen=True)
class RoutePivot:
    """A redirectable decision the route committed to.

    PILOT picked `(tag, value)` where ≥1 other viable option existed.  The
    engineer steers away with `avoid=<via_hint>` or toward an alternative with
    `via=<that alt's via_hint>`.  `via_hint` is the bridge from the human label
    to the `avoid=`/`via=` predicate: a concrete condition `_route_forces` can
    test (a single steerable tag in the arm, or the committed coil tag)."""
    tag: str                  # e.g. "ProdMode"   (multi-writer coil, or arm rep)
    value: Any                # e.g. True
    label: str                # "ProdMode" / "manual-jog branch"  (reuse _choice_label)
    kind: str                 # "writer" | "or-arm"
    via_hint: tuple[str, Any] # (tag, value) the engineer names: avoid=/via= target
    alternatives: tuple[RouteAlt, ...] = ()   # the roads not taken
    salient: bool = True      # False for trivial cost-0 forks (Or(Auto, Manual))


@dataclass(frozen=True)
class RouteAlt:
    """A road not taken at a pivot — what `via=` would switch to."""
    label: str                # "MaintMode"
    via_hint: tuple[str, Any] # how to ask for it: via=(MaintMode, True)


@dataclass(frozen=True)
class RouteTaken:
    """How PILOT reached the goal — the legible 'here's where I went'."""
    label: str                          # one-line: "via ProdMode"
    pivots: tuple[RoutePivot, ...]       # redirect points, trace order
    dominant: bool                       # True = unique cheapest, no real fork
```

### `Path` delta

```python
@dataclass(frozen=True)
class Path:
    reachable: bool
    steps: tuple[ReachabilityStep, ...]
    ...
    route: RouteTaken | None = None      # NEW: populated on reachable Bool plans
    # choices:  REMOVED   (its content -> route.pivots[*].alternatives)
    # ambiguous property: REMOVED  (no terminal ambiguous state)
```

`__str__` on a reachable path with a non-dominant pivot renders, e.g.:

```
Path (1 step): ProdCmd=True
  Route: via ProdMode
    redirect: avoid=ProdMode  (or via=MaintMode) -> reached via MaintMode
```

### What computes it, and where

At the seam that is `_prepare_trace_choice` today (`pilot.py`). Instead of
`collapse → ()` or `surface → _ambiguous_path`, every entry point:

1. `enumerate_trace_choices(...)` → the fork set (as now).
2. Apply `avoid_pred`/`via_pred` pruning (avoid exists; via is the dual).
3. **Pick the default**: cheapest surviving route by `_trace_score`
   (retentive + input-gated gates decide eligibility / break ties; rung order
   is the final deterministic tiebreak).
4. **Build `RouteTaken`**: the default's label; each fork it passed becomes a
   `RoutePivot` whose `alternatives` are the pruned/unchosen routes, with
   `via_hint`s lifted from each route's forcing leaves.
5. Trace with the default locked; attach `route` to the returned `Path`.

`dominant` = the default was the *unique* cheapest (no sibling at min cost).
`salient` per pivot = the fork had cost > 0 alternatives (Burner: salient;
`Or(Auto, Manual)`: not — both cost 0, redirect still possible but not headlined).

**DECIDED**: trivial cost-0 forks ARE recorded, as non-salient pivots — uniform
redirect (`avoid=Auto` works everywhere) beats a special-case drop. The renderer
hides non-salient pivots from the headline but they remain `avoid=`/`via=`-able.

### `via_hint` — the human-label ↔ predicate bridge

The pivot's job is to make redirect expressible. Two cases:

- **multi-writer** (`ProdMode` vs `MaintMode`): `via_hint = (coil_tag, True)`.
  `avoid=ProdMode` / `via=MaintMode` map straight to `_route_forces` predicates.
- **OR-arm** (manual-jog vs auto-sort): the arm is a *condition*, not one tag.
  Pick a representative steerable leaf as the hint — `via=(DiverterBtn, True)`
  for the manual arm — and let `_route_forces` confirm the route forces it.

This is the only genuinely new logic; everything else is relocation + the
already-planned `via=`.

## Migration / tests

- `test_bool_output_ambiguous_requires_choice` → rewrite: assert
  `how(Burner).reachable` with `route.label` naming ProdMode, then
  `how(Burner, via=MaintMode)` (or `avoid=ProdMode`) reaches via MaintMode.
- OR-arm tests (`test_or_arm_over_inputs_collapses`,
  `test_or_mixed_steerable_conjunction_arm_collapses`) stay green — they already
  assert reachable; add a `route.label` assertion if we want to lock legibility.
- `Path.ambiguous` consumers (DAP `dap/console.py` 697/751, CLI `how` renderer,
  any `.choices` reader) must move to `.route`. Grep `\.ambiguous|\.choices`
  before ripping it out (most `.choices` hits are unrelated — tag `choices=`,
  the `choices_violation` validator, named-arrays).
- `choice=` removed outright — also delete the param from `runner.how()` and the
  threading through every `pilot_*` entry point.

## Resolved

1. **Trivial cost-0 forks** (`Or(Auto, Manual)`) → recorded as **non-salient
   pivots**. Uniform redirect; renderer hides them from the headline.
2. **`choice=`** → **removed outright**, no deprecation shim.

## Deferred (revisit when `pilot_drive` is designed)

`pilot_drive` (steering real hardware) vs `how` (planning): `how` is
review-then-redirect, low risk — adopt report-and-redirect now. Whether *drive*
wants a confirmation gate before committing to a non-dominant default is left
open **because `pilot()`/`pilot_drive` itself is not yet thought through** — fold
this question into that design when it happens, don't pre-decide it here.
