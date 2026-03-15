"""Master coverage program — one rung per instruction variant.

Run this to generate master.csv via to_ladder(). Each rung exercises
exactly one instruction type so laddercodec can compare per-rung bytes
independently.

Rung IDs are embedded as comments so the progress report can name them.

Usage:
    python -m coverage_program

Adapt imports to your actual pyrung project layout.
"""

from pathlib import Path

from pyrung import (
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Rung,
    TagType,
    Tms,
    any_of,
    calc,
    copy,
    count_down,
    count_up,
    fall,
    fill,
    immediate,
    latch,
    off_delay,
    on_delay,
    out,
    reset,
    rise,
    search,
    shift,
)
from pyrung.click import TagMap, c, ct, ctd, ds, t, td, x, y

# ── Tag allocation ──────────────────────────────────────────────────
# Each rung gets its own addresses so there are no collisions.
# Naming: r{NN}_{purpose}

# We'll use x[1]..x[16] for condition inputs, x[21]..x[32] for more.
# y[1]..y[16] for coil outputs.
# t[1]..t[4], td[1]..td[4] for timers.
# ct[1]..ct[4], ctd[1]..ctd[4] for counters.
# ds[1]..ds[50] for integer scratch.
# c[1]..c[50] for bit scratch.
# dd[1]..dd[4] for double int scratch.
# df[1]..df[4] for float scratch.
# dh[1]..dh[4] for hex scratch.
# txt[1]..txt[10] for text scratch.


# ── Semantic tags ───────────────────────────────────────────────────
# Contacts / conditions
r01_in = Bool("r01_in")
r02_in = Bool("r02_in")
r03_in = Bool("r03_in")
r04_in = Bool("r04_in")
r05_in = Bool("r05_in")
r06_in = Bool("r06_in")
r07_in = Bool("r07_in")
r08_in = Bool("r08_in")
r09_in = Bool("r09_in")
r10_in = Bool("r10_in")
r11_in = Bool("r11_in")
r12_in = Bool("r12_in")
r13_in = Bool("r13_in")
r14_in = Bool("r14_in")
r15_in = Bool("r15_in")
r16_in = Bool("r16_in")
r17_in = Bool("r17_in")
r18_in = Bool("r18_in")
r19_in = Bool("r19_in")
r20_in = Bool("r20_in")
r21_in = Bool("r21_in")
r22_in = Bool("r22_in")
r23_in = Bool("r23_in")
r24_in = Bool("r24_in")
r25_in = Bool("r25_in")

r08_cmp = Int("r08_cmp")
r09_cmp = Int("r09_cmp")
r10_cmp = Int("r10_cmp")
r11_cmp = Int("r11_cmp")
r12_cmp = Int("r12_cmp")
r13_cmp = Int("r13_cmp")

# Coil outputs
r01_out = Bool("r01_out")
r02_out = Bool("r02_out")
r03_out = Bool("r03_out")
r04_out = Bool("r04_out")
r05_out = Bool("r05_out")
r06_out = Bool("r06_out")
r07_out = Bool("r07_out")
r08_out = Bool("r08_out")
r09_out = Bool("r09_out")
r10_out = Bool("r10_out")
r11_out = Bool("r11_out")
r12_out = Bool("r12_out")
r13_out = Bool("r13_out")

# Timer tags
r14_done = Bool("r14_done")
r14_acc = Int("r14_acc")
r15_done = Bool("r15_done")
r15_acc = Int("r15_acc")
r15_rst = Bool("r15_rst")
r16_done = Bool("r16_done")
r16_acc = Int("r16_acc")

# Counter tags
r17_done = Bool("r17_done")
r17_acc = Dint("r17_acc")
r17_rst = Bool("r17_rst")
r18_done = Bool("r18_done")
r18_acc = Dint("r18_acc")
r18_rst = Bool("r18_rst")

# Data transfer tags
r19_src = Int("r19_src")
r19_dst = Int("r19_dst")
r20_fill_dest = Block("r20_fill", TagType.INT, 1, 10)

# Calc
r21_a = Int("r21_a")
r21_b = Int("r21_b")
r21_result = Int("r21_result")

# Search
r22_val = Int("r22_val")
r22_result = Int("r22_result")
r22_found = Bool("r22_found")
r22_search_src = Block("r22_srch", TagType.INT, 1, 10)

# Shift
r23_clock = Bool("r23_clock")
r23_reset = Bool("r23_reset")
r23_shift_bits = Block("r23_shft", TagType.BOOL, 1, 8)

# OR branch
r24_a = Bool("r24_a")
r24_b = Bool("r24_b")
r24_out = Bool("r24_out")

# Multi-condition AND
r25_a = Bool("r25_a")
r25_b = Bool("r25_b")
r25_out = Bool("r25_out")


# ── Rung catalog ────────────────────────────────────────────────────
# Each rung comment encodes a stable ID for the progress report.

with Program() as coverage_program:
    # ── Condition × Coil matrix ─────────────────────────────────────

    # 01: NO contact → OUT coil
    with Rung(r01_in) as r:
        r.comment = "01_contact_no__out"
        out(r01_out)

    # 02: NC contact → OUT coil
    with Rung(~r02_in) as r:
        r.comment = "02_contact_nc__out"
        out(r02_out)

    # 03: NO contact → LATCH coil
    with Rung(r03_in) as r:
        r.comment = "03_contact_no__latch"
        latch(r03_out)

    # 04: NO contact → RESET coil
    with Rung(r04_in) as r:
        r.comment = "04_contact_no__reset"
        reset(r04_out)

    # 05: Rising edge → OUT coil
    with Rung(rise(r05_in)) as r:
        r.comment = "05_edge_rise__out"
        out(r05_out)

    # 06: Falling edge → OUT coil
    with Rung(fall(r06_in)) as r:
        r.comment = "06_edge_fall__out"
        out(r06_out)

    # 07: Immediate NO contact → Immediate OUT coil
    with Rung(immediate(r07_in)) as r:
        r.comment = "07_immediate__immediate_out"
        out(immediate(r07_out))

    # ── Compare contacts ────────────────────────────────────────────

    # 08: Compare == → OUT
    with Rung(r08_cmp == 5) as r:
        r.comment = "08_compare_eq__out"
        out(r08_out)

    # 09: Compare != → OUT
    with Rung(r09_cmp != 0) as r:
        r.comment = "09_compare_ne__out"
        out(r09_out)

    # 10: Compare < → OUT
    with Rung(r10_cmp < 100) as r:
        r.comment = "10_compare_lt__out"
        out(r10_out)

    # 11: Compare > → OUT
    with Rung(r11_cmp > 0) as r:
        r.comment = "11_compare_gt__out"
        out(r11_out)

    # 12: Compare <= → OUT
    with Rung(r12_cmp <= 50) as r:
        r.comment = "12_compare_le__out"
        out(r12_out)

    # 13: Compare >= → OUT
    with Rung(r13_cmp >= 10) as r:
        r.comment = "13_compare_ge__out"
        out(r13_out)

    # ── Timers ──────────────────────────────────────────────────────

    # 14: On-delay timer (standard, no reset → TON)
    with Rung(r14_in) as r:
        r.comment = "14_on_delay__ton"
        on_delay(r14_done, r14_acc, preset=3000, unit=Tms)

    # 15: On-delay timer + .reset() → RTON (retentive)
    with Rung(r15_in) as r:
        r.comment = "15_on_delay_reset__rton"
        on_delay(r15_done, r15_acc, preset=5000, unit=Tms).reset(r15_rst)

    # 16: Off-delay timer → TOF
    with Rung(r16_in) as r:
        r.comment = "16_off_delay__tof"
        off_delay(r16_done, r16_acc, preset=2000, unit=Tms)

    # ── Counters ────────────────────────────────────────────────────

    # 17: Count up + .reset()
    with Rung(rise(r17_in)) as r:
        r.comment = "17_count_up"
        count_up(r17_done, r17_acc, preset=100).reset(r17_rst)

    # 18: Count down + .reset()
    with Rung(rise(r18_in)) as r:
        r.comment = "18_count_down"
        count_down(r18_done, r18_acc, preset=50).reset(r18_rst)

    # ── Data transfer ───────────────────────────────────────────────

    # 19: Copy (scalar)
    with Rung(r19_in) as r:
        r.comment = "19_copy"
        copy(r19_src, r19_dst)

    # 20: Fill
    with Rung(r20_in) as r:
        r.comment = "20_fill"
        fill(0, r20_fill_dest.select(1, 10))

    # ── Calc ────────────────────────────────────────────────────────

    # 21: Calc expression
    with Rung(r21_in) as r:
        r.comment = "21_calc"
        calc(r21_a + r21_b, r21_result)

    # ── Search ──────────────────────────────────────────────────────

    # 22: Search
    with Rung(r22_in) as r:
        r.comment = "22_search"
        search("==", r22_val, r22_search_src.select(1, 10), r22_result, r22_found)

    # ── Shift register ──────────────────────────────────────────────

    # 23: Shift
    with Rung(r23_in) as r:
        r.comment = "23_shift"
        shift(r23_shift_bits.select(1, 8)).clock(r23_clock).reset(r23_reset)

    # ── Wiring / topology ───────────────────────────────────────────

    # 24: OR branch (any_of)
    with Rung(any_of(r24_a, r24_b)) as r:
        r.comment = "24_or_branch"
        out(r24_out)

    # 25: AND chain (multiple contacts)
    with Rung(r25_a, r25_b) as r:
        r.comment = "25_and_chain"
        out(r25_out)


# ── TagMap ──────────────────────────────────────────────────────────

mapping = TagMap(
    {
        # 01-07: contacts and coils
        r01_in: x[1],
        r01_out: y[1],
        r02_in: x[2],
        r02_out: y[2],
        r03_in: x[3],
        r03_out: y[3],
        r04_in: x[4],
        r04_out: y[4],
        r05_in: x[5],
        r05_out: y[5],
        r06_in: x[6],
        r06_out: y[6],
        r07_in: x[7],
        r07_out: y[7],
        # 08-13: compare contacts (Y008+ invalid, use C bits for outputs)
        r08_cmp: ds[1],
        r08_out: c[31],
        r09_cmp: ds[2],
        r09_out: c[32],
        r10_cmp: ds[3],
        r10_out: c[33],
        r11_cmp: ds[4],
        r11_out: c[34],
        r12_cmp: ds[5],
        r12_out: c[35],
        r13_cmp: ds[6],
        r13_out: c[36],
        # 14-16: timers
        r14_in: x[8],
        r14_done: t[1],
        r14_acc: td[1],
        r15_in: x[9],
        r15_done: t[2],
        r15_acc: td[2],
        r15_rst: x[10],
        r16_in: x[11],
        r16_done: t[3],
        r16_acc: td[3],
        # 17-18: counters
        r17_in: x[12],
        r17_done: ct[1],
        r17_acc: ctd[1],
        r17_rst: c[1],
        r18_in: x[13],
        r18_done: ct[2],
        r18_acc: ctd[2],
        r18_rst: c[2],
        # 19-20: data transfer
        r19_in: x[14],
        r19_src: ds[7],
        r19_dst: ds[8],
        r20_in: x[15],
        r20_fill_dest: ds.select(41, 50),
        # 21: calc
        r21_in: x[16],
        r21_a: ds[9],
        r21_b: ds[10],
        r21_result: ds[11],
        # 22: search (X021+ invalid, use C bits)
        r22_in: c[21],
        r22_val: ds[12],
        r22_result: ds[13],
        r22_found: c[3],
        r22_search_src: ds.select(31, 40),
        # 23: shift
        r23_in: c[4],
        r23_clock: c[5],
        r23_reset: c[6],
        r23_shift_bits: c.select(11, 18),
        # 24-25: wiring (X/Y overflow → C bits)
        r24_a: c[22],
        r24_b: c[23],
        r24_out: c[37],
        r25_a: c[24],
        r25_b: c[25],
        r25_out: c[38],
    }
)


# ── Export ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    out_dir = Path("fixtures/coverage")
    bundle = mapping.to_ladder(coverage_program)
    bundle.write(out_dir)
    print(f"Wrote {len(coverage_program.rungs)} rungs to {out_dir}/main.csv")
