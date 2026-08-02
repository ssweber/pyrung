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

## Cache compiled replay kernels across overlay changes

Avoid recompiling the unchanged user program whenever PILOT swaps plant or hold
overlays. Cache compiled replay kernels by the executable identities that affect
the result, including the user program and plant/hold prefix.

Verify cache reuse across equivalent overlays, invalidation when executable
overlay identity changes, and identical interpreted/compiled replay behavior.
