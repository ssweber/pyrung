# PILOT generalization — handoff (2026-07-05)

State of the "genuinely generalized pilot" effort. Completed work is committed
on `dev`; this file covers what remains, with enough context to resume cold.

## Where things stand (committed)

Two waves, all on `dev`, full suite green (4771 passed / 34 skipped / 7 xfailed
at `e1b532b`), `make lint` clean:

- `e49b669`..`019fd27` — first sweep: `_TT` via accumulating profile,
  blast-radius drop → ordering tier, semantic journal labels, Bool-domain
  closure in `guard_satisfiable`, sandbox split, calc-source unification,
  fire-time-pin oracle trigger, skiff boundary gates, CLAUDE.md.
- `8a60302` — crossings inverts affine expression-source copies (exact in the
  clamp interior, rail punts; shared with the prover — test-prove green).
- `ebda5ca` — set-valued guard-lattice coil aliases; `guard_satisfiable`
  rejection arm wired into `_trace_back` writer admission (three-valued
  `guard_verdict`; DEAD only over complete domains; `TraceNode.live_guard`
  punt signal).
- `814511e` — corrections handle multi-read guards: prime-implicant
  enumeration over read domains, coordinated multi-lever holds, honest
  declines.
- `e1b532b` — **the skiff is wired**: `probe_live_guard_frontiers` at both
  stuck exits; singles→pairs isolated probes on condition-read levers;
  composite causes → `prescribed_batch` batch trials; `Compass.contradict`
  falsifies stale seeded edges. The command-selected-mask boundary gate
  (`tests/core/analysis/test_pilot_sandbox_gate.py`) passes end to end.

Invariants that must survive any further work:
- Skiff results only ever feed `Compass.record` — bearings, never plan steps;
  the verify pipeline is the sole source of confirmed edges.
- Enumeration rejects/pins only over COMPLETE finite domains (nd_domains,
  declared choices, Bool); plausible-value fallbacks never reject.
- Punt on anything unreadable; never fabricate an edge or an alias.

## Task 5 — non-affine calc-decode inversion  [IN FLIGHT]

An Opus agent was launched and may still be running (or died with the
session). Brief: extend `_transition_fire_pins` (trace.py) to non-affine calc
decodes by enumerate-and-evaluate on the writer side — solve
`calc_expr(srcs…) == value` over the sources' finite domains, reusing
`table_oracle`'s domain resolution (`_guard_operand_domain`) and guardrails
(`_MAX_FREE_INDICES`/`_MAX_COMBOS`). Pin semantics: only FORCED values — the
per-source projection shared by ALL satisfying assignments (or punt if the
agent chose the simpler unique-assignment-only rule; check its report).
Solver likely belongs in table_oracle.py. Tests: a non-affine decode variant
in test_table_oracle.py's `_mask_gate_program` family + solver unit tests.
Acceptance: existing shapes byte-identical; skiff gates stay 7 passed /
1 xfailed. If the agent's work is on disk and green, commit as
`feat(pilot): derive fire-time pins for non-affine calc decodes`.

## Task 6 — route machinery for word-valued and Bool==False targets

`_prepare_route` (pilot.py) gates on `_target_is_bool_true`, so word targets
and `Bool==False` targets get no route enumeration and no `avoid=`/`via=`
redirect. Extend `enumerate_trace_choices` (trace.py) to a general
`tag == value` target contract, give `RouteTaken` pivots sensible labels for
value routes, and lift the multi-target `avoid`/`via` ValueError in
`_pilot_how_multi` while there. Largest user-facing surface of the remaining
tasks — no new theory, but wants its own careful pass and doc updates
(Plan.route semantics in docs/). Sequenced after task 5 (both edit trace.py).

## Task 8 — open the corrections menu (design done, not implemented)

Goal: a regression whose fix isn't FLIP/OSCILLATE/FREEZE-shaped currently
yields `unresolved`. Design (worked out this session):

1. **Generalize the antagonist family.** `investigate_excursion` only
   recognizes `ResetInstruction` antagonists. Replace with: any writer rung of
   the deviated register that is *causally implicated* in the deviation
   (via `chase_cause_roots` / `cause()` on the deviation scan window).
2. **Suppression by guard-force enumeration.** For each implicated writer,
   propose the minimal drivable lever set that forces its guard FALSE —
   this is exactly task 7's machinery (`_minimal_forcing_sets` /
   `_best_forcing_holds` in corrections.py) with the polarity inverted
   (satisfies=False). Menu vocabulary stays FLIP/FREEZE/OSCILLATE; what opens
   is the *dispatch* (any implicated writer, not an instruction-class list).
3. **Skiff escalation.** When the guard enumeration punts (live-word guard on
   the antagonist), run bounded isolated probes (`run_pinned_scan`) over
   condition-read steerable levers in the antagonist guard's upstream cone:
   hold a lever, replay the deviation window in the pinned fork, keep levers
   under which the antagonist does not fire. Nominations only.
4. **Replay confirmation stays the gate.** All nominated holds flow through
   investigate's existing replay-testing; nothing is applied unverified.

Out of scope by design: pulse *sequences* (multi-step corrections) — that is
search, and should wait until the above shrinks the space.

Files: investigate.py, corrections.py (+ sandbox.py import), tests in
test_pilot_investigate.py. Test cases: a non-Reset clobbering copy with a
compound guard (currently unresolved → corrected); a live-word-gated
antagonist needing the probe pass; existing menu behavior unchanged.

## Task 9 — long tail (fold in opportunistically)

- **`compute_reference_constants` misses indirectly-read table rows** — a
  never-written ds/dh slot read only via `ds[computed]` classifies steerable.
  The skiff filters these by condition-read; the proper fix is in
  ref_consts' read-set walk. (Found live: the skiff was probing
  `cfg_prod_row=-1`.)
- Drums: return an `accumulating_profile()`; pass `fork=`/`harness` at
  corrections.py's `scans_to_eject`/`iter_profiles` call sites so Tier-2
  empirical `measure_scans` is reachable.
- `_resolve_inequality_target` strict-inequality `±1` step is wrong for Real
  tags / non-unit domains — make it domain/epsilon-aware.
- `_counter_driver_leaf` enumerates only Bool advance candidates — solve int
  advance conditions over declared domains.
- `_scan_transient_rest` multi-scope producer variants fall through.
- `_rewrite_internal_compare` requires exactly one Cmp per branch.
- `table_from_indirect_src` 3-hop pointer chase proceeds on a partial
  `eval_addr` at hop 4 instead of punting cleanly.
- Free-word skiff tier (strict xfail in test_pilot_sandbox_gate.py): needs
  value synthesis (e.g. bitwise-complement proposals for `&==0` guards) and/or
  establish-staged sequences (set word → pulse load → command). This is the
  next instrument to earn its gate.

## Working agreements

- Never `git stash`; stage by explicit path (other agents may share the tree).
- `make test-pilot` (~7s) for the loop; `uv run pytest tests/core/analysis/`
  for the subsystem; full `make test` + `make lint` before committing anything
  touching core/instruction or crossings (crossings is shared with the
  prover — run `make test-prove` too).
- Conventional Commits; end with `Co-Authored-By: Claude Fable 5
  <noreply@anthropic.com>`.
- The boundary gates are the acceptance discipline: every new instrument or
  tier gets a hand-driveable, statically-punting, honestly-failing gate
  program *before* the wiring, and a strict xfail as the tripwire.
