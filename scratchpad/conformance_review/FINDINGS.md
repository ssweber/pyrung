# Conformance part A on a real CLICK: findings (2026-10-29)

Hardware: CLICK at 192.168.1.238, part A loaded (layout 28574), watchdog raised to 10000.
21 failures in part A, all explained. Per-group detail, scripts and proposed ladders are
in the subfolders (`coils/`, `counters/`, `drum/`, `loop/`, `numeric/`).

## Real CLICK semantics pyrung gets wrong (core changes)

1. **Counters count rising edges** of the up/down inputs (per-instruction memory, updated
   on every scan including reset scans; a held input does not recount after reset).
   Down-counter Done is `acc <= preset`, not `<= -preset`.
   -> counters.py, kernel, fold advance_profile, prover events/counter model,
   crossings/accumulating.py, circuitpy `_instructions_basic.py`, docs + AGENTS.md gotcha.
2. **Edge contacts keep per-contact memory** (the bit as that contact last saw it), not
   the bit at end of previous scan. `rise(X)` above X's writer fires one scan late on
   CLICK, never in pyrung. Replaces `_prev:` per-tag keys (~11 files: condition.py,
   runner, kernel, compiled_plc, circuitpy `_core.py`, prover kernel/elision,
   causal/projected, pilot tide_tables, fold). Open: memory updated when scanned, or
   only when powered? Needs a probe (edge contact behind an open contact / false For).
3. **False For rung skips its body** (no unpowered pass; children's one-shots do not
   rearm). Loop count is read as **unsigned 16-bit** (-1 -> ~65535 iterations; 0 is 0
   or 65536, open). -> control.py ForLoopInstruction, executor (2 paths),
   debug_handlers, circuitpy `_compile_for_loop_instruction`, fold, prover.
4. **Event drum ordering**: jump/jog act on their edges even while paused; reset is a
   level that wins (step 1, flag off, ALL outputs off, drops jump/jog that scan);
   jump beats jog on the same scan; paused drum does not write outputs; at completion
   all outputs go off (also time drum, per CLICK charts). -> drums.py (all three),
   fold, crossings/drums.py, circuitpy `_instructions_block.py`, drum docs.
5. **REAL overflow stores +/-Inf** with SC43 (not saturation); div-by-zero still 0+SC40.
   Decision needed: adopt Inf or document deviation.
6. **MDN of an integer range with even count** is an integer: truncated mean of the
   middle two, or the middle value nearer zero (new data {9,2,10,1} separates: 5 vs 2).
7. Math intermediates are wider than 32 bits (`intermediate_exceeds_32_bits` passed);
   AGENTS.md checklist line saying 32-bit is wrong.

## pyrung bugs found (not CLICK semantics)

- `click_real_literal` (62b5664) silently changes values whose 7-digit spelling is a
  different float32: 16777216.0 exported as 16777220.0. Should raise or report.

## Test-case changes (proposed, not applied)

| group | case | change |
|---|---|---|
| coils | edge_sees_previous_scan_end, edge_before_its_writer | record firing scans as digits; side-by-side contacts (coils/proposed.py) |
| coils | rise_fall_fire_one_scan | confirmed=True |
| counters | 6 cases | one-scan pulse inputs so each asks its own question (counters/proposed.py) |
| counters | count_down_negative + new count_down_done_at_preset, held_input_not_recounted | mark the real differences |
| drum | reset_no_stale_outputs | never complete, reset held 2 scans |
| drum | reset_jump_jog_order | expect {3,1,1}, "reset wins; jump beats jog" |
| drum | pause_holds_and_drops_jog | retire; re-add as paused_drum_jogs_and_resets {1,3,4,6} |
| drum | new completion_turns_outputs_off | needs C801..C850 in memory.OWNED |
| loop | F2 | retire; new false_for_rung_skips_body |
| loop | F3 | split; zero-count probe; -1 off the shared run (1.1 s scan) |
| loop | F8, F14, F6 | For always true, gated body rung, on/off by iteration; new F17 |
| numeric | dint_calc_wraps | compare readback; new dint_quotient_into_int probe |
| numeric | real_is_64_bit_in_pyrung | stage via DD; in_process_only |
| numeric | real_overflow_stays_finite | readbacks that separate Inf from saturation; preset R4 |
| math | mdn_even_count | new data separating candidate rules |

## Open hardware questions (next round)

- Edge memory: updated when scanned vs when powered.
- Loop count 0: 0 or 65536 iterations.
- Drum: jump while paused? outputs back after completion jump/jog? reset swallowing a held edge?
- Counter: input already on at first scan after STOP->RUN counts?
- dint quotient: constant 65536 truncated, or -32768 rejected?
- MDN on a DF range: exact or integer?
- Rerun part A wall-clock cases without F3's 1.1 s scan.

---

# Part B (layout 13935): 16 failures, both passes identical

Detail in `rungs/`, `system/`, `text/`, `out_search/`; run log `partB/run1.log`.

## Real CLICK semantics pyrung gets wrong (core changes)

8. **Rungs solve row by row, live** (CLICK manual ch1 "Program Execution": left to right,
   rung by rung). Each contact read live when the solve reaches it; output runs as soon as
   its row is solved; power at a shared node held for rows below (parent not re-read);
   continued wires are ordinary rows. pyrung's "atomic rung" snapshot is wrong.
   -> rung.py Rung.execute (_compute_branch_enable_map), executor (2 paths),
   debug_handlers, circuitpy `_core.py` snapshot bindings, prover classify
   `_uses_prior_snapshot`, fold continued guards, simplified/pdg/pilot overlays.
   ~300-500 lines / ~15 files; 115 `.continued()` call sites. Docs: learn/branches.md
   "atomic rungs" tip, docs/AGENTS.md "Scan evaluation", instructions/rungs.md. Breaking.
   Cases need only new expectations (ladders already discriminate).
9. **Out coils never set T<n>** (O6 answered): Debounce/Flasher/Timed derive phase from TDs.
   -> out_variants.py, fold Out-coil sources, prover classify, circuitpy `_instructions_out.py`;
   Click finding: contact on an Out-claimed T<n> is always False.
10. **Continuous search cursor**: 0 restarts; old result inside range resumes after it;
    anything else misses (-1). -> advanced.py `_resolve_cursor_index`, circuitpy search.
11. **TXT is 8-bit** (Copy Binary 200 stored 200, despite manual "7-bit"); **pack_text into
    INT/DINT clamps** (123456 -> 32767, 2147483648 -> 2147483647); "12.5" -> 12;
    **Character Value of NUL writes 0**. -> conversions.py, copy_converters.py, packing.py,
    circuitpy `_helpers.py`; pyclickplc decodes byte 200 as U+FFFD (upstream fix).
12. **SC3 is on when SD9 is even** (one of them is offset from pyrung's). -> system_points.py,
    circuitpy render.py. First-scan case decides which.
13. **SC40/43/44 likely follow the last instruction** (successful op clears) — confounded as
    written; rewrite discriminates any-instruction / any-calc / same-kind / end-of-scan.

## pyrung bugs found (not CLICK semantics)

- CLICK importer (`click/codegen/analyzer.py` `_split_continued`, prefix factoring;
  `shaping.py` logic check) merges rail rows with a common prefix into branches; unsound
  under live row solving when an earlier row writes a prefix contact.
- test_text.py docstring says TXT can't be read over Modbus; it can.

## Test-case changes (proposed, not applied)

| group | case | change |
|---|---|---|
| rungs | 4 failing | CLICK expectations, confirmed=True (proposed.py); passing 4 confirmed=True |
| system | 3 fault-flag cases | probe-coil rewrite (system/proto_faults2.py); expectations after rerun |
| system | scan_clock_follows_counter | swap parity; new first_scan_counter_and_clock (slot 9) |
| text | to_value_rejects, to_binary, pack_text_whitespace | instruction under test last before flag rung; CLICK values |
| text | pack_text_int/dint | CLICK clamped values + flag reading; new pack_text_int_fraction ("12.9", "12x") |
| out | out_coil_sets_timer_done | expect 0 + phase-finished R2, confirmed=True |
| out | flasher_restarts_with_on_phase | rewrite to record clear/keep per TD; rerun |
| search | continuous_cursor_is_address | R4 = -1; new continuous_cursor_outside_range (13) |

## Open hardware questions (part B)

- Fault flags: which model (rewritten cases). Do clamps (pack_text, NUL, Copy Binary 200) set SC43?
- First scan after STOP->RUN: SD9 starts at 1, or SC3 starts on? SD9 wrap value?
- Flasher with rung false: TDs cleared, kept, frozen; restart phase.
- Search edges 410/417/-5; reversed ranges.
- "12.9" -> 12 or 13; "12x" -> 12 or refused.
- Can a box (timer/counter reset leg, drum jump) sit below other outputs in one CLICK rung?

---

# Plan (decided 2026-10-29)

1. **Match CLICK.** Every semantic finding above becomes pyrung's behaviour (not a
   documented deviation): live row-by-row rung solve, per-contact edge memory, counters on
   edges (+ down Done at acc <= preset), false For skips body + unsigned 16-bit count,
   drum ordering, Out coils never set T<n>, search cursor rule, TXT 8-bit, pack_text clamps,
   NUL -> 0, MDN rule, REAL overflow -> Inf, SC3/SD9 phase, fault-flag model (after rerun).
   Kernel, fold (`make test-fold`), prover (`make test-soundness`), pilot, circuitpy codegen,
   docs ("atomic rungs" lesson, docs/AGENTS.md, gotchas) move together. Breaking CHANGELOG.
2. **One static check: intra-rung order dependence.** Within one rung, a write followed later
   in solve order by a read of the same tag (another row/branch/continued wire). Lives in
   `core/validation/`; correct under the new semantics but fragile, so it is a finding.
   - **Import** (`ladder_to_pyrung` / ClickNick): warn on such rungs, and stop merging rail
     rows into branches of a shared prefix when an earlier row writes a prefix contact
     (split rows to keep CLICK's behaviour).
   - **pyrung programs and export**: same finding from `check` and the Click export.
   - **ClickNick Static Analysis** surfaces it.
3. **Test rewrites** per the tables above; then a hardware round answering the open questions.
4. **Fallback for the rung solve (8).** If live row solving proves too hard in kernel/fold/
   prover, keep pyrung's atomic rung and make the step-2 finding say "pyrung differs from
   CLICK here". The check flags exactly the rungs where atomic and live solving disagree
   (a write earlier in the rung read later in it), so the warning covers the whole gap.
   Ship the check first: it is needed under either choice.

# STOP groups (all 7, part B)

None stopped the PLC. All seven read identically: R1 = 0 (destination written 0),
R2 = 1 (rest of the scan sees SC46), SC11 = 1 (still RUN), SC46 = 0 and SC50 = 0 when read
afterwards (SC46 clears each scan). The CLICK help text quoted in test_math_stop.py says
SC50 turns on and the PLC goes to STOP; this PLC did not. Open: a project/system setting?
If STOP never happens, the STOP groups can rejoin the normal run, and pyrung's
"math error -> STOP" model (PLC.start() after reset, SimServer STOP handling, circuitpy
890bd4f "hold STOP on the SC50 command") is wrong for this configuration.
User (2026-10-29): no CLICK setting controls this to their knowledge. Treat as finding 14:
**a Math error does not stop the PLC** -- it writes 0, sets SC46 for the rest of the scan,
and the PLC keeps running (help text is wrong or applies to other firmware; record the
firmware version with the next round). Plan: drop pyrung's math-error -> STOP (core
system_points/runner STOP path, SimServer, circuitpy SC50 hold from 890bd4f), and fold the
seven stop_* groups back into a normal group (math) that runs with the rest.
