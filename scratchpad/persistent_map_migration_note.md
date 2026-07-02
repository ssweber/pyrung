# Deferred: pyrsistent → C-backed persistent map migration

2026-06-12. Parked after the scan-loop perf arc (72ae688, cf95fca) because the
remaining win is modest relative to the refactor: ~0.6–0.7ms off a 2.0ms
interpreted scan, plus smaller gains in commit/fork/history paths.

## Why we looked

This venv (CPython 3.13 on Windows) runs pyrsistent **pure Python** — 0.20.0
(Oct 2023) ships binary wheels only up to cp312, and PMap is pure Python in
every install regardless (the C extension `pvectorc` covers PVector only).
After the read-path caching landed, ~0.4ms/scan of pmap reads + ~0.3ms of
evolver/commit work remain.

## Measured (scan-shaped microbench: 2,800-key map, 466 reads, 90-write batch)

| op                | PMap    | immutables.Map | rpds-py | dict   |
|-------------------|---------|----------------|---------|--------|
| 466 reads         | 428µs   | 17.6µs         | 123µs   | 12.3µs |
| 90 writes + commit| 308µs   | 14.3µs         | 176µs   | —      |
| build from dict   | 1267µs  | 469µs          | —       | —      |

immutables reads at near-dict speed; the safe immutable-snapshot architecture
stops being expensive, which also obsoletes the riskier "plain-dict tag
mirror" idea.

## Maintenance / distribution facts (checked 2026-06-12 on PyPI)

- `immutables` 0.21 (2024-10-10): wheels cp38–cp313, **no cp314** 8 months
  after 3.14 shipped. MagicStack cadence has broken; modest dependent base.
- `pyrsistent` 0.20.0 (2023-10-25): wheels to cp312 + pure-py fallback —
  degrades silently on newer Pythons (how we got here).
- `rpds-py` 2026.5.1 (2026-05-28): wheels through cp315 incl. freethreaded;
  underpins jsonschema (which migrated off pyrsistent in 2023 for exactly
  this reason). Slower than immutables but still 3.5×/1.7× better than PMap.
- pyrung is a published library (`requires-python >=3.11,<4.0`) — a hard dep's
  wheel matrix becomes pyrung's install story. immutables on 3.14/Windows
  today means sdist build (MSVC) or silent fallback.

## Plan when revisited

1. One-file internal wrapper first (`Map`, `mutate` — ~30 lines) so the
   backend is a one-line switch; immutables vs rpds-py decided by a real
   scan-loop benchmark, not the microbench.
2. `state.py`: SystemState PRecord → frozen `__slots__` class (kills the
   per-field-access bucket walk everywhere, not just the hot paths already
   cached in context.py).
3. `context.py` evolvers → `Map.mutate()`; then `rung_firings.py` (34 pmap
   occurrences), `compiled_plc.py` materialization, runner remnants.
4. Audit: tests comparing `state.tags == {...}`, `isinstance(..., PMap)`
   checks, DAP capture serialization, prove paths hashing state.
5. Gate: full suite + soundness + parity.

## Revisit triggers

- Scan-loop speed becomes the bottleneck again (e.g. walker corridor scans
  dominate solve time after the planner levers are exhausted).
- Python 3.14 bump (pyrsistent goes pure-Python there too — the swap becomes
  a correctness-of-promise issue for pyrung's own wheel story).
- pyrsistent breaks outright on a new CPython.

## Stake-tested 2026-07-02 — PARKED (dep-*character*, not dep-*count*)

Re-measured on the REAL burner state (2672 tags, real per-scan read/write batch —
`scratchpad/burner/diag_backend_stake.py`, `diag_immutables_fallback.py`), settling
the mirror-vs-swap contradiction with `INTERP_SCAN_PROFILE.md` lever #1:

| op             | dict | immutables C | immutables PURE | rpds | PMap |
|----------------|-----:|-------------:|----------------:|-----:|-----:|
| read (ns)      |  ~30 |         ~35  |        **2393** | ~200 | ~800 |
| commit (µs/89) |  ~14 |         ~8.7 |            ~238 |  ~90 | ~150 |

The claim this note made ("immutables reads at near-dict speed") holds **only for
the C ext** — which is all it ever benched. immutables' pure-Python fallback
(`immutables/map.py`, what ships when there's no wheel, e.g. cp314 today) reads
**3× SLOWER than pyrsistent**. So the swap is NOT "trade one pure-py map for
another": it's **bimodal** — great with the C wheel, worse-than-today without, and
the bad mode triggers on new CPythons (exactly how we got here). A dep-*character*
swap, not a dep-*count* swap — that's the axis that decides it, not dep count.

rpds is **dominated**: 200 ns read / 90 µs commit → −5.7% end-to-end, worse than
the free dict mirror (−7%). Drop it despite its cp315 wheels.

**Decision: PARKED.** Neither the mirror nor the swap moves `how()` — it's
bottlenecked in the causal/incident layer (~76%), not the forward scan (~12%);
see `INTERP_SCAN_PROFILE.md` stake-test outcome (the mirror was implemented, passed
`make test`, measured +8.5% scan / ~1% `how()`, and reverted). Revisit the backend
swap ONLY for scan-bound workloads (twin / sim / `run_for`); if taken, gate on
`immutables._map` being importable so the 3×-worse pure fallback never ships.
