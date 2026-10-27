# CLICK v4 — what's left

All six features have landed. This is the remaining work, most important first.

## Bugs

1. **Real preset tags disagree between backends.** Interpreted compares `acc >= 2.5`, compiled
   `acc >= int(2.5)` (`on_delay`, `timed`, `debounce`).
2. **Fold lands ahead of stepping with two `on_delay`s on one Timer.** One rung false and resetting it, one
   ticking, plus any other moving timer: Acc folds to 350 where stepping gives 70. A duplicate-out program, but
   it breaks "equal stepping or decline".
3. **`cause("T")` on a stepped `calc(T + 3, T)` raises RecursionError.**
4. **Export doesn't enforce CLICK's 24-character nickname limit** (`pyclickplc.validation.NICKNAME_MAX_LENGTH`).
   Add a validation finding.
5. **The importer drops the comment on an un-nicknamed TD row.**
6. **A callable property that reads a tag no rung touches** (`lambda s: s["M0"]`) raises KeyError from
   `guarded_state`. The condition form of the same property works.

## Hardware

- Real-PLC conformance runs for the `out` (O1-O6), `drum` (D1, D3), `math` (M1-M4, `rand_range`) and `loop`
  (F1-F14) groups: `make test-conformance PLC=host[:port]`, then flip `confirmed=True` on what passes.
- Depending on O6: if CLICK does not write T<n> for an Out type, add `CLK_OUT_TIMER_DONE_UNSET`.
- F15 (inner one-shot: does CLICK reject or ignore it?) is a question for the CLICK editor.
- Subroutine-call cases (F16, "call into subroutine", "subroutines: same rules") need a home called from main.

## Gaps, accepted for now

- Prover has no exact model for `time_event_drum`, `debounce`/`flasher`/`timed`, or conditioned forloop body
  rungs; each is a named Intractable (BFS only, so `how()` keeps its prover context).
- Fold: a display copied into an unread block element goes stale inside a window when read back through an
  unbounded index (strict xfails `TestIndexedDisplays`).
- Prover: CTUD (`count_up(c, 4).down(rise(Y))`) classification; timer-race stop-point gaps (`cycles.py`
  "What is left", sweep `tools/race_sweep.py`); PackML lock 91 s.
- PILOT declines a counter fed by a debounced bit's edges (strict xfail `test_pilot_counts_debounced_edges`), and
  an Acc target past a drum's preset.
- Lints: `CMP_PRESET_STAYS_ZERO` misses drum duration registers and Out presets; `stranded_bits` doesn't count a
  toggle target as persistent; `physical_realism` ignores the Out types; D2 (which banks may hold a duration
  register) has no Click check.
- Fuzz grammar has no T&E drum or Out kinds.
