# Hardware round 2 run sheet (from bob/probes-round2, 70bb4df)

Branch `bob/probes-round2` (ad608b0 markers + call_twice groups; 70bb4df CLICK expectations +
probes). Green: make lint, make test 7634, test-conformance sim + circuitpy (120 pass / 35
pending / 0 fail), conformance-export A and B. Loop group NOT included here (S1 owns it), so
run round 2 after S1 merges if the loop probes should ride along.

## Pending / probe markers
- `case(pending="S2")`, tag in S2 S3 S4 DRUM OUT NOSTOP INF TEXT SYS (`PENDING_STEPS` in
  `_case.py`): strict xfail on pyrung's targets; fails with "remove pending=" once pyrung
  matches; judged normally against a real CLICK. Not exported, so no group-hash change.
  36 pending: TEXT 8, DRUM 6, SYS 5, S4 5, S2 5, S3 3, OUT 2, INF 1, NOSTOP 1.
- `case(probe="...")`: names the open question; docstring lists each candidate's readback. 27 probes.
- `Group(calls=2)` (call_twice group). `memory.OWNED` accepts tuples of ranges.
- stop_* groups -> one sequenced case `math_error/math_errors_do_not_stop`, still in
  `suite.STOP_GROUPS` until NOSTOP lands (then move to ALL_GROUPS). DS2901..3040 stay reserved.

## Builder caveats
- No part A run log existed, so in part A only `rise_fall_fire_one_scan` is confirmed.
- coils/proposed.py held pyrung's values; drum reset/completion drafts were placeholders.
  CLICK values derived from the per-contact model and drum/model_a.py (hypotheses, not measured).
- count_down_done_at_preset widened to 16 scans so <= preset / <= 0 / <= -preset read 4/10/16.
- Fault flags: adopted "follows the last instruction" (expect 0,0,0).
- Reversed-range search can't be probed (exporter writes an address list).
- Part A grew ~29% in cells (1673 -> 2154), B 1702 -> 1812 -> 2215 with solve_order (402 cells): check each part's step count on load against the estimate.

## Load
1. Three parts since 2026-10-30 (`suite.PARTS`, by topic; the A/B column in the table below is
   the OLD split): A = block, counters, drum, math, math_error, numeric (layout 1782, est. ~5860);
   B = out, search, shift, system, text, timers (layout 25452, ~5970); C = call_twice, coils,
   loop, rungs, solve_order (layout 1757, ~3970). `make conformance-export PART=A|B|C` writes
   `build/conformance-<part>`; load each fresh: new project, ClickNick Guided Paste, link
   `tests/conformance/workspace/`, save, write, RUN.
2. Note step count per part, firmware version, whether C bits are non-retentive (default).
3. Note any rung Guided Paste refuses; `rungs/box_leg_reads_rows_above` (counter box below a
   latch) refused = "a box cannot sit below other outputs".
4. SD9 wrap probe: nothing to do; the wire test waits for SD9 to pass 32767 when the system group (part B) runs.
5. `make test-conformance PLC=192.168.1.238` (reads loaded part from DS1); then the other part.
6. First-scan probes read the first scan after the latest STOP->RUN (loading provides it).
7. Part A wall-clock cases get the rerun FINDINGS asked for.

## Retired before the run (2026-10-30)
CLICK does not compile a box below another output on one wire (C000A: all inputs (pins) of a box
must start from the same branch). Retired: rungs/box_leg_reads_rows_above, solve_order/rton_reset_leg,
count_down_leg, shift_clock_leg, shift_reset_leg, drum_reset_leg, drum_jump_leg, drum_jog_leg,
for_body_box_leg_reads_row_above (3c252e0). Export refuses the shape (50cb2ee). Ignore their rows below.

## Probes and spec checks (adopted answer first; R1..R4 = Result1..4)

| Case | Question | Readback meaning |
|---|---|---|
| A coils/edge_behind_open_contact | Edge memory updated when scanned or only when powered | R1,R2 = 0,0 scanned; 5,8 powered (R3,R4 must read 3,7) |
| A coils/edge_in_false_rung_branch | Same, branch of a false rung | 0,0 scanned; 5,8 powered |
| A coils/edge_on_first_scan | Edge memory at STOP->RUN | R1 = 1 starts off and fires; 0 seeded from bit or carried (R2 must be 0) |
| A coils/edge_sees_previous_scan_end | Per-contact edge memory | 3,3,3,3 per-contact; 345 x4 end-of-scan (pyrung); 3,0,3,0 one memory per bit |
| A coils/edge_before_its_writer | Same | 4,7,3,6 per-contact; 0,0,3,6 end-of-scan |
| A call_twice/rise_once_per_scan | rise() in a sub called twice | 6,5,8,7 per-site; 6,56,8,78 pyrung; 0,5,0,7 shared memory updated per read |
| A counters/up_down_same_scan | Up+down edges one scan | 2,2,6 cancel; 3,4,6 up wins; 1,0,2 down wins |
| A counters/count_down_done_at_preset | Down Done threshold | R2 = 4 Acc <= preset; 10 <= 0; 16 <= -preset |
| A counters/edge_on_reset_scan | Edge on reset scan | 0,1 memory updated during reset; 1,2 frozen; 2,4 counts levels |
| A counters/first_scan_input_already_on | Input on at first scan | R1 = 1 counts; 0 not (R2 must be 0) |
| A counters/level_vs_edge, held_input_not_recounted, count_down_negative | Spec | 2,2; 1,0; -3,1,-1 |
| A drum/paused_drum_jumps | Jump while paused | 3,3,5,6 taken; 1,1,8,0 ignored |
| A drum/reset_swallows_held_jog | Reset vs held jog edge | 1,0 spent; R2 = 5 kept for after; R2 = 4 applied with reset (pyrung) |
| A drum/completion_turns_outputs_off | Outputs after completion, then jump/jog | R2 = 2 off at completion (5 stay on); R3 = 6 + ErrorCode = 8 back after jump/jog (0 stay off) |
| A drum/reset_no_stale_outputs | Reset turns outputs off | R2 = 7 all off during reset; 5 step 1 written at once |
| A drum/reset_jump_jog_order, paused_drum_jogs_and_resets | Spec | 3,1,1; 1,3,4,6 |
| A math/mdn_even_count | MDN even count | 5,50,50,-5 truncated mean; 2,20,20,-2 nearer zero; 2,20,20,-9 lower middle; 5,50,50,-6 floor; 5,55,55,-5 exact (pyrung) |
| A math/mdn_real_range | MDN on DF | 55,15 exact; 50,10 integer |
| A math_error/math_errors_do_not_stop | Math error vs STOP | R1 = R2 = 127, SC11 = 1, SC50 = 0, SC46 = 0 keeps running; never fires + SC11 = 0 stops; missing bit names fn (0 SQRT, 1 LN0, 2 LN-1, 3 LOG0, 4 LOG-1, 5 ASIN, 6 ACOS) |
| A numeric/dint_quotient_into_int | -2147483648/65536 into Int | -32768,-32768,-32767 exact; 0,-32768,0 constant cut to 16 bits; R1 = 99 or -32767 refused/clamped |
| A numeric/real_overflow_stays_finite | REAL overflow | 1,1,1 Inf; 0,0,1 saturates |
| A numeric/dint_calc_wraps | Compare readback | 1,1 |
| B rungs/box_leg_reads_rows_above | Box leg read at its row | R1 = 0 at its row; 1 at rung entry; paste refused = no box below outputs |
| B rungs/branch_sees_rung_entry, nested, continued x2 | Spec, live row solve | 0,1; 0,0; 0,0; 0,0,1 |
| B system/division, out_of_range, address flags | What clears a fault flag | 0,0,0 any instruction; 346,0,0 any Math; 346,346,0 own kind; 346,346,346 end of scan (pyrung) |
| B system/first_scan_counter_and_clock | First scan SD9/SC3 | 1,1,0 SD9 starts at 1; 0,1,1 SD9 0 with SC3 on |
| B system/scan_counter_wraps_to_zero | SD9 wrap | R1 = 0 wraps to 0; 1 negative (only after 32768 scans) |
| B system/scan_clock_follows_counter | Spec | 8,8 |
| B text/to_value_rejects | Char Value of NUL | R3 = 0; R4 = 0 no SC43, 1 SC43 |
| B text/to_binary | Copy Binary 200 | R3 = 200; R4 = SC43 off/on |
| B text/pack_text_int, pack_text_dint | Clamp sets SC43? | ErrorCode (int) / R4 (dint): 0 no, 1 yes |
| B text/pack_text_int_fraction | "12.9", "12x" | R1: 12 trunc, 13 round, 99 refused; R2: 12 prefix, 99 refused; R3,R4 = SC43 after each |
| B text/pack_text_whitespace | Refusal sets SC43? | R2 = 1 yes; 0 no |
| B search/continuous_cursor_outside_range | Cursor at 410, 417, -5 | -1,-1,-1,413 rule; 411,-1,411,413 restarts from below |
| B out/flasher_restarts_with_on_phase | Flasher rung false | R1 on-side TD 1 cleared/2 kept; R3 off-side TD same; ErrorCode 1 frozen-or-cleared/2 counting; R2/R4 1/1 fresh on, 0/1 resumes off (adopted), 0/0 fresh off |
| B out/out_coil_sets_timer_done | Spec | 0,1 |
| B solve_order/row_series_reads_rows_above, sibling_branch_reads_sibling_above, outer_row_reads_inner_branch_write, branch_or_reads_row_above | Live row solve: series rows, sibling/nested branches, OR on a branch | 1,0 / 1,0,1,1 / 1,0,1 / 1,0 live (L); 0,1 / 0,1,1,1 / 0,1,1 / 0,1 rung-entry snapshot (S, pyrung) |
| B solve_order/row_reads_row_below, continued_reads_later_wire | Control: solved once, top down | 0,1 each; 1,1 re-solved to a fixpoint |
| B solve_order/input_or_legs_read_before_outputs | Control: rail OR solved once, before the outputs | R1 = 1; 0 every row's path re-solved |
| B solve_order/bus_operand_sees_coil, bus_coils_last_write_wins | Control: output bus in order | 1; 0,1. 0 coil writes land at rung end; 1,1 OR/set wins; 0,0 reset wins |
| B solve_order/rton_reset_leg | RTON reset leg read at its row | 1,4,4 at its row; 0,0,0 rung entry; never fires = paste refused a box below outputs |
| B solve_order/count_down_leg | Counter down leg | R1 = 1 at its row; 2 rung entry; never fires = refused |
| B solve_order/shift_clock_leg, shift_reset_leg | Shift clock / reset legs | 3; 1 at its row. 1; 3 rung entry; never fires = refused |
| B solve_order/drum_reset_leg, drum_jump_leg, drum_jog_leg | Drum reset / jump / jog legs | R1,R2 = 1,2 / 3,2 / 3,2 at its row; 2,2 rung entry; never fires = refused |
| B solve_order/box_leg_reads_row_below | Control: leg not read after the continued wire below the box | R1 = 1; 0 legs read at rung end or fixpoint; never fires = refused |
| B solve_order/for_body_box_leg_reads_row_above | Box leg in a For body rung | 0,1 at its row; 1,1 rung entry; never fires = refused (or no counter in a For body) |
| B solve_order/for_body_rung_reads_body_rung_above | Body rung sees the body rung above it, same pass | 1,0; 0,1 body rungs share the For rung's snapshot |

When a probe reads another candidate: set the expectation to CLICK's reading and note the answer; confirmed.toml is written by the run itself.
