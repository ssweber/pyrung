# PILOT cleanup

## Make continuation replay explicit

Replace the dynamically attached `replay.with_continuation` closure attribute
with a typed, explicit API. Production and tests must exercise the same
continuation-proof path for relational corrections.

Prefer either:

- a small dataclass or named tuple containing `replay` and
  `replay_with_continuation`; or
- an explicit `prove_continuation` argument at the call site.

Remove the `getattr(..., None)` fallback and add direct coverage for relational
corrections using continuation replay.
