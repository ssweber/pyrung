# Phase 6 — incident/replay from receipts (implementation record, 2026-07-16)

Design source: DESIGN.md Part 5 + Part 8 phase 6; audit items I2/I3/I4/C2.

## Pens — the missing recorder capability

The receipt timeline today only records watched-tag transitions at bump firings.
For the timeline to *be* the incident evidence, mid-coast transitions must land on
it: **pens** — nonterminal, re-arming, change-from-baseline recorders owned by the
session (`CoastSession.pens`).

- In `seek()`: pens ride as one internal nonterminal bump (watched = pen tags →
  fold-protected → exact landings). Each firing appends one `BumpEvent` per scan
  grouping all changed pens, refreshes baselines, steps once, keeps coasting.
  Bit-exactness: the fold would have produced the same trajectory; a pen landing
  converts a virtual scan to a real one, never changes tag evolution or the
  terminal landing scan.
- In `dwell()` / `settle()` (step-mode): `note_pens()` after every scan.
- Raw `plc.step()` in `_apply_pulse` / `_apply_actions`: `note_pens()` after each.

Pen universe (per trial): profile Done bits ∪ `state.watch_tags` ∪ pipeline role
channels, **minus accumulator registers** (a change-pen on a per-scan-churny tag
would collapse the fold to step-mode; acc membership in `changed_tags` is served
by endpoint diff — see below) and clock/scan-derived names.

## What replaces what

| Deleted | Replaced by |
|---|---|
| `_changed_tags_in_window` (investigate.py) | incident timeline (Done transitions) ∪ acc endpoint diff (`before_snap`/`after_snap`) |
| `_first_departure_scan` | first timeline transition off the bearing value |
| `_DEPARTURE_MARGIN` | audit I3: replay coast seeks first-of {target, eject, timeout}; budget = the recorded step's own span; judgment reads `stop_reason`, not a snapshot at margin+N |
| positional `is_eject_coast` inference | recorded per-step spec (`ReplayStepSpec`: inputs, scans, kind from `MotionKind`, channel_tag, channel_target) built in progress.py from `step_contexts` |
| `letrun_tried[key] >= len(rungs)` | `letrun_memo[world_key] = stop_reason`, recorded only when trusted (departed commit, or stalled with **no pending effects** — audit C2); an untrusted (pending) stall is deliberately NOT memoized and may re-coast, bounded by the skiff key budget |
| `_new_cause`'s history diff on the probe | the replay session's own timeline (pens = all Done bits) |

## Deliberate deviations from DESIGN.md wording

- `causal._changed_in_window` **stays**: its only caller is
  `empirical_program_writes` (the empirical steerable veto), which is recorded-run
  *testimony* — skiff consults it over the whole run (scan 0..now), a window no
  receipt can cover. The duplication dies (investigate's copy deleted); the
  testimony reader survives, same shape as the design's own trace_back ruling.
- `_last_transition_scan` (ranking proximity) stays history-based: hypothesis
  tags are levers outside the pen universe.

## Threading

`CoastSession.events` → `_PulseState.timeline` → `_TrialResult.timeline` →
`_StepContext.timeline` (stamped at `_record_step_context`) → incident timeline
gathered in `_investigate_and_revert` over `[anchor_scan, end_scan]` →
`DeviationIncident.timeline` (new field, evidence for ranking/corrections).

Replay watch-set scoping (audit I2) becomes an explicit `build_replay_fn`
parameter (`replay_watch_roles`), computed at the progress.py build site:
channel-only for a channel incident, full role set otherwise.

## Golden risk

`unresolved` (= `incident.changed_tags`) is skeleton-kept. Parity strategy: Done
membership from timeline; acc membership from endpoint diff (misses only a
mid-window A→B→A acc excursion with equal endpoints and no Done event). If
goldens diff, review + regen is the explicit fallback.
