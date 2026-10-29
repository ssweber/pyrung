"""Proposed edits to tests/conformance/test_rung_semantics.py (NOT applied).

Ladders unchanged (they already discriminate every candidate model); only the
expectations, sentences, confirmed flags and the module docstring change.

Model CLICK showed (2026-10-29, part B, two identical passes):
  A rung is solved row by row, top to bottom.  Each contact is read once, live,
  when the solve reaches it; the power at a shared node (a vertical wire) is
  computed once and held for every row below it; each output runs as soon as
  its row is solved.  (Same as a Koyo IL stack compile: STR/AND ... MPS / OUT /
  MRD / AND / OUT / MPP.)  A continued wire is just another row from the rail.
"""

# --- case list replacements -------------------------------------------------
case(
    "A branch reads its contacts live, when the solve reaches its row, after the "
    "rows above it ran; the parent's contacts are read once: the rung's first row "
    "sets the bit its parent contact needs off and the second row still runs "
    "(C1602 set); the branch that needs C1602 off does not run (Result1 = 0) and "
    "the one that needs it on does (Result2 = 1)",
    ladder=prove_branch_sees_rung_entry,
    init=init_branch_sees_rung_entry,
    expect={"Result1": 0, "Result2": 1},
    confirmed=True,
),
case(
    "Nested branches read live too: inner branches that need off the bits set "
    "above the outer branch (Result1 = 0) and inside it (Result2 = 0) do not run",
    ladder=prove_nested_branches_see_rung_entry,
    init=init_nested_branches_see_rung_entry,
    expect={"Result1": 0, "Result2": 0},
    confirmed=True,
),
case(
    "A continued wire reads live, like a rung of its own: the second wire needs "
    "off a bit the first sets and does not run (Result1 = 0), nor the fourth, "
    "which needs off a bit the third sets (Result2 = 0)",
    ladder=prove_continued_shares_snapshot,
    init=init_continued_shares_snapshot,
    expect={"Result1": 0, "Result2": 0},
    confirmed=True,
),
case(
    "Continued wires and normal rungs read alike: after a chain (Result1 = 0), a "
    "normal rung sets a bit; its continued wire that needs that bit off does not "
    "run (Result2 = 0), and one that needs on the bit set at the head of the "
    "earlier chain runs (Result3 = 1)",
    ladder=prove_continued_chain_breaks,
    init=init_continued_chain_breaks,
    expect={"Result1": 0, "Result2": 0, "Result3": 1},
    confirmed=True,
),
# memory_is_immediate, global_mutation, branch_and_with_parent,
# last_rung_wins_shared_coil passed on CLICK: add confirmed=True.

# --- module docstring, model section ------------------------------------------
DOC = """
- A rung is solved row by row.  Its own contacts are read once, on entry.  A
  branch's contacts are read live when the solve reaches the branch, after
  every instruction above it in the rung ran; the parent's power is held, not
  re-read.  Instructions run top to bottom against live memory.
- A ``.continued()`` rung is another row of the same CLICK rung; it reads live
  like any rung (the flag is layout only).

Models the cases tell apart (S = rung-entry snapshot, L = live row by row,
W = every row's whole path re-solved live, B = a branch snapshots where it
leaves its parent):
- branch_sees_rung_entry: S {1,0}; L {0,1}; W {0,0} (and C1602 stays off)
- nested_branches_see_rung_entry: S {1,1}; L {0,0}; W {0,0}; B {0,1}
- continued_shares_snapshot: S {1,1}; L {0,0}; first-wire-only {1,0}
- continued_chain_breaks: S {1,1,1}; L {0,0,1}; chain keeps first snapshot R3 = 0
"""
