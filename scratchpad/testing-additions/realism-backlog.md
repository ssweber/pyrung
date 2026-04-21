# Realism model — open items

Layer 1 static checks and codegen are done. Items below don't have a
clean answer yet; they're parked here so the planning docs stay stable.

## Runtime range/choices enforcement (beartype-style)

The static validator catches literal writes outside `min`/`max` and
`choices`. Dynamic writes (`copy(other_tag, bounded_tag)`) go
unchecked. The spec mentions a "runtime bounds-check fallback."

One option: intercept tag writes in the scan engine and check
`min`/`max`/`choices` on every committed value. This is a beartype-like
approach — declarative metadata becomes runtime enforcement. Applies to
both `choices` (existing) and `min`/`max` (new).

Open questions:

- **Performance.** Every tag write gets a bounds check. Most writes
  are internal, most tags don't have constraints. Need a fast path
  (e.g., only check tags that declare constraints, precomputed set).
- **What happens on violation?** Options: clamp (like `copy()` already
  does for out-of-range), fault flag (like division-by-zero), raise,
  or finding/warning. Clamping silently hides bugs; raising breaks the
  scan. A fault flag + finding is probably right.
- **Choices at runtime.** `choices` today is purely a metadata/display
  concern. Making it a runtime constraint changes the contract — a tag
  with `choices={0: "Off", 1: "On"}` would reject a write of `2`.
  Is that desirable? Probably, but needs explicit opt-in or a separate
  `strict_choices` flag.
- **Interaction with `copy()` clamping.** `copy()` already clamps
  out-of-range values for type limits. If the tag also has `min`/`max`,
  should `copy()` clamp to the tag range too, or should the runtime
  check fire after `copy()` writes the clamped-to-type-range value?

## `physical=` on software-only tags

The spec says `physical=` on a "pivot" (software-only) tag should warn.
The concept of "software-only" doesn't have a formal marker today.
Heuristics (no hardware mapping, no `external` flag) could work but
feel fragile. An explicit `physical=NONE` sentinel for "looks physical
but isn't" is mentioned in the spec but not implemented.

This is low priority — misapplying `physical=` to a software tag is
mostly a documentation smell, not a correctness bug.

## Profile registry validation

`Physical(..., profile="first_order")` names a profile function, but
nothing validates that the named function is actually registered. The
static validator emits `CORE_MISSING_PROFILE` for linked analog
feedback without *any* profile, but doesn't check whether a named
profile resolves to a real function. That check requires a profile
registry (Layer 2).

## `physical=NONE` sentinel

The spec mentions `[physical=NONE]` for tags that look physical but
aren't (e.g., a tag named `Fb_Pressure` that's actually a calculated
value). This would suppress warnings about missing `physical=` on
tags that match physical-looking naming patterns — but those warnings
don't exist yet either. Park both together.
