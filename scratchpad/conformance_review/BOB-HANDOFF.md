# BOB handoff (BOB1 -> bob2, 2026-10-29)

## LIVE STATUS (2026-10-31 morning, supersedes everything below where they differ)
Policy (user 2026-10-30): prover bugs never block; strict xfail + ledger, fix only if straightforward.
Ship the conformance arc first; prover side-issues get a second sweep (list in the ledger).
- Landed + pushed: train7 = round2-expect + loop-call-liveness + probe-cases (5d9c0f0). Parts D/E exist.
- READY -> train8: blackbox (a15db8b/452c68f, fast5 green, re-review clean) + input-partition
  (745c562, re-review clean, input-partition-fast4 queued). They conflict in prove/classify.py +
  passes.py (classify kwarg goes on the single _pass_classify_dimensions call); CHANGELOG too.
- inf (bob/inf, 3 commits): review BLOCKING -- NaN REAL in bitwise/shift crashes the scan on all
  backends (calc.py:224-237, compile/_core.py:849-863 miss ValueError -> treat as 0+SC43). NB: **
  overflow 0 not Inf; NaN retentive dirty forever (render.py:1288); docs 64-bit intermediates.
  inf-fast queued. 9 Inf/NaN hardware questions -> round-3 probe cases (jobs/inf/HANDOFF.md).
- indirect-expr-compare (5f5b157): runtime fix for `Tbl[Q+0] == S`; needs fast gate + Opus review.
  Whichever of it / input-partition lands second removes the NB3 strict xfail marker.
- s3-edges: builder died at the usage limit; 3 commits (ecefd59, 6595cc3, 9915604) + 2
  uncommitted docs files. Checkpoint-commit, then resume/re-brief.
- Then: S4a/S4b after S3; SYS after round 3. Round 3 (user): repaste A + B, paste new D + E.
- Notes: ../pyrung-wt/buildbot/jobs/<id>/{HANDOFF,REVIEW}.md; arc ledger jobs/conformance-round2/LEDGER.md.

## LIVE STATUS (2026-10-30 21:10)
- Landed + pushed: train6 return-early-branch (62e4ad6).
- READY pending fast gate: round2-expect (expectations + 40 ms sim scan cap 92fb6a5; review clean).
- In review: input-partition (75a5d52, e94fa39); blackbox phases 3-5 (1046000.., absorb.py kept prior-only);
  loop-call-liveness fix round 2 (2db5adc; includes the return-early merge; a 3rd failure goes to the user).
- Building: probe-cases (ret_branch in new part D; main-level group home + call-in-For probes + CLK_NESTED_CALL).
- Merge note: input-partition's classify kwarg moves to the single _pass_classify_dimensions call after blackbox;
  r2_eq strict xfail on blackbox is removed by whichever lands second.
- Next after these: pilot-edges; fuzzer tag-to-tag compares + bounded subset fuzz in the train preset (proposed to user).

## LIVE STATUS (2026-10-30 20:40)
- Landed + pushed: train2 S2 (fb09791); train4 s2-pilot + classify-entry-read (061066f).
- train5 running: round2-expect (expectations + flasher case fix 3e7190a) + return-early-branch (fe7b05c, cc1addb).
- blackbox: phases 1-2 on bob/blackbox, fix round 2 running (R1 compared-tag values). User approved
  phase 3 once review is clean; a 3rd failed round goes to the user. VERDICTS.md in jobs/blackbox.
- loop-call-liveness: building (call in forloop body pruned by live_input_pruning, default-only false Proven).
- Next: pilot-edges branch (s2-pilot deferred items + cascade backstop false STOP, see jobs/s2-pilot/REVIEW.md).
- Round 3 (user): repaste A numeric, B system/out/text; hardware Qs: Int Math wide constant, float/RAND,
  SC43/SC44 clearing, hex Math SC40, REAL operand, Return on a branch (needs a probe case).
- Follow-ups: SimServer ~33 ms scans make wall-clock conf cases fragile; prove-gaps branch delete after blackbox.

## LIVE STATUS (2026-10-30 15:30)
Process: `/bob` skill. Ledger: `../pyrung-wt/buildbot/jobs/conformance-round2/LEDGER.md` (trust it).
- Landed: train1 = DRUM/OUT/TEXT/NOSTOP (`5def46e`); hardware round 2 ran (memory
  project_click_hardware_round2: 8 probe answers differ -> expectation updates still pending).
- Building: S2 fixer on `bob/s2-counters` (rebase onto dev, REVIEW B1 drums revert in
  prove/events.py, B2 CHANGELOG revert, non-blocking items). Then fast gate + resume reviewer.
- PARKED: branch `bob/prove-gaps` only, worktree removed (WIP eede4fc) pending the black-box prover decision (pyrung-a3); P1a/b/c + P2
  probe shapes are acceptance cases for whichever prover model wins.
- Notes: `../pyrung-wt/buildbot/jobs/s2-counters/{HANDOFF,REVIEW}.md`, probes in `probes/`.
- solve-order landed and removed.
- train2 = S2 alone on --stages train (running). Building in parallel: s2-pilot (review-2 N1/N2/N3),
  blackbox (scratchpad/prove-blackbox/PLAN.md phases 1-2; VERDICTS.md to the user before phase 3),
  round2-expect (8 differing hardware answers; repaste list in its handoff).

Arc: make pyrung match real CLICK behaviour (user decision: match CLICK, not documented
deviations). What is wrong: `FINDINGS.md`. How it lands: `DESIGN.md` (spike + S0 limitations at
the end). Hardware round 2 run sheet: `ROUND2.md`. All three in this folder (DESIGN/ROUND2 untracked).

## Landed on dev
- `ad608b0`, `70bb4df` conformance spec: CLICK expectations, `case(pending="<step>", probe="...")`
  (strict xfail on pyrung targets; flips to failure when pyrung matches -> fixer must remove marker),
  27 probes, `call_twice` group, stop_* groups -> `math_error/math_errors_do_not_stop` (still in
  `suite.STOP_GROUPS` until NOSTOP lands; then move to ALL_GROUPS). Steps: S2 S3 S4 DRUM OUT NOSTOP INF TEXT SYS.
- `942654b` S0 site identity table (`core/site_memory.py`): `i{N}` instruction keys unchanged, `e{N}`
  edge sites recorded (not evaluated), overlay namespaces `plant:`/`pilot:`, missing key raises,
  prover keys from the table (fixed a false Proven: one-shot in a subroutine not called on scan 0),
  per-key defaults in the state key, reload remaps by fingerprint+contacts (unambiguous only).
- `6ae9c82` S1 For loops: a false For rung skips its body (coils hold, timers/counters not scanned,
  body one-shots don't re-arm); count read as `int(n) & 0xFFFF`; `FOR_ZERO_COUNT_ITERATIONS = 0`
  switch in `core/instruction/control.py`. Fixed three prover soundness holes on the way (pdg: For-body
  out() no longer follows the rung, tag count joins the guard; passes.py `_writes_run_when_powered`
  for skippable writes: tag-count For, one-shot For, one-shot calc/copy, write in a branch; slice.py
  one-shot For key covers body writes). Loop group: F6/F18 pending S3, F8 pending S2, F19 probe.

## CLK_STEP_LIMIT export failure: FIXED `aa260ca` (2026-10-30)
`3a5f4c9` made export raise on CLK_STEP_LIMIT (even "near"), breaking the 3 whole-suite export tests
and parts A/B too. `aa260ca`: export now issues it as a UserWarning and still writes. No known
failures from it. Part A is at 7999 est. steps (B 7765): auto-split at a 7500 budget is due before
hardware round 2.

## Pipeline (use it): `C:/Users/Sam/Documents/GitHub/pyrung-wt/buildbot/README.md`
Builder (own hand-made worktree `../pyrung-wt/<name>`, branch `bob/<name>`, targeted tests only, no
heavy suites, no lock) -> BOB saves handoff to `buildbot/jobs/<id>/HANDOFF.md` -> `runner.py submit
--stages fast` + fresh reviewer (diff + FINDINGS/DESIGN, not the builder's reasoning) -> blocking
findings: resume builder if < ~50 min idle and < ~300k ctx, else fresh fixer -> resume reviewer to
verify -> heavy -> merge (auto on green; only if dev == job base_sha; tree must equal tested merge).
Heavy tiers: prover/fold/kernel/executor/rung eval/codegen -> heavy before merge; single-instruction,
tests-only, docs -> fast + review + its conformance group, heavy batched on dev (`--branch dev
--stages heavy`) when idle or after ~3 merges. `runner.py cancel --id X` cleans up. Run `next` with
`run_in_background` (one job per invocation -> you are notified). Gate times on dev+S0: make test
~13 min, soundness ~2, fold ~15, conf ~4, conf-cp ~2.5. Subagent cache TTL is 1h (user setting).
User prefs: milestone updates only; one question at a time; trunk on dev; pyrung lowercase.

## Next steps, in order
1. **S2 counters count edges** (core -> heavy before merge). FINDINGS item 1; DESIGN S2 row; memory
   keys `_cu:i{N}`/`_cd:i{N}` through S0's site table (`instruction_memory`). Implement the probes'
   ADOPTED answers (counters group docstrings): up+down same scan, edge on reset scan (memory updated
   during reset), input already on at first scan; down Done at `acc <= preset`. Remove fold counter
   sources + prover per-scan counter events (`events.py`, `_fixup_unfired_counters`). Pilot:
   count-to-preset level-hold branch becomes wrong -> oscillating hold for every counter driver
   (memory project_pilot_counter_preset). Prover: some proofs riding level-driven counters become
   named Intractable (allowed). Clears `pending="S2"` cases (counters group, loop F8).
2. **Leaf fixes in parallel with S2** (batched-heavy tier unless they touch prover/fold):
   TEXT (TXT 8-bit, pack_text clamps, NUL -> 0, search cursor rule, MDN), DRUM (FINDINGS 4), OUT
   (Out coils never set T<n>), INF (REAL overflow -> +/-Inf, SC43), NOSTOP. Decide first:
   - NOSTOP scope: remove only the math-error -> STOP trigger (core, SimServer, circuitpy). Check
     whether SC50 is CLICK's commanded-STOP bit before touching the 890bd4f SC50 hold.
   - INF vs fix A7 (enforced float32 range bounds on REAL): decide what bounds checks, prover
     domains and fold do with +/-Inf.
3. **Soundness follow-up** (prover/fold -> heavy): `pdg.unconditional_write_before_read` ignores
   one-shots; unguarded callers `fold.py:732`, `pdg.py:842`, `causal/why.py:125` may give wrong
   results for a one-shot For / one-shot copy on an unconditional rung (identical on dev; predates
   S1). Reproduce first.
4. **Solve-order group lands** (tests-only tier): another Claude owns it (worktree
   `../pyrung-wt/solve-order`, branch `bob/solve-order` -- not ours, don't touch);
   `tests/conformance/test_solve_order.py`, 15 cases in part B (ROUND2.md rows "solve_order/...").
5. **Hardware round 2** (user runs it; `ROUND2.md` + loop probes F6/F8/F14/F17/F18/F19/zero_count +
   solve_order). After S1 and the solve-order group are on dev. Check part A AND part B step counts
   on load (A 1673 -> 2154 cells, B 1702 -> 2215).
5b. **Order-dependence check** AFTER round 2: its ground truth is the measured solve_order readbacks
   (flag exactly the cases whose result depends on solve order); same table is the S4 spec.
6. **S3 per-contact edges** waits on round 2 (scanned vs powered; first-scan; per-iteration) AND the
   S0 precondition: keys stamped on shared rung objects must become per-PLC (DESIGN.md last section).
   Then S3b prover edge optimizations need their own design spike.
7. **S4 live row solve**: DESIGN.md; slice.py read hook must move in the same commit as the runtime.
8. SYS (SC3/SD9 parity, fault-flag model) after round 2.

## Known limitations recorded (not blocking)
- S0: combining rungs into a bare list renames keys in place (Program site table goes stale; another
  live PLC holding the rungs loses memory); Expr/Indirect contacts render as class names for reload.
- S1: the skipped-body timer/counter hold has no switch (user: CLICK won't honour reset in a skipped
  body; resurrect code if ever needed). Dead code: executor.py ~1334, debugger loop_body parent_enabled=False.
  Loop conformance group at slots=19 (manifest limit).
