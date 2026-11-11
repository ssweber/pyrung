# BOB profile: pyrung

Repo-specific rules for the /bob skill. Another repo replaces this file.

## Paths

- Main checkout `C:/Users/Sam/Documents/GitHub/pyrung`, base branch `dev` (trunk; the user
  squash-merges dev into main themselves; never touch main). Push dev to origin after each
  landing (user, 2026-10-30); never push bob/* branches or main.
- Worktrees `../pyrung-wt/<name>` on `bob/<name>`; mergeq home `../pyrung-wt/buildbot`
  (queue, logs, results, `jobs/<id>/{HANDOFF,REVIEW,LEDGER}.md`).
- Current arc doc: `scratchpad/conform-readings/arc.md` (untracked, so give builders the absolute
  main-checkout path). One arc doc: fold rulings and leftovers into it, not side notes.

## Correctness contract (put in every builder and reviewer brief)

- Match real CLICK behaviour; hardware-recorded expectations in `tests/conformance/` are
  authoritative. `pending="<STEP>"` cases are strict xfails; the fixer removes the marker in
  the commit that makes the case pass (`tests/conformance/AGENTS.md`).
- Fold must equal stepping or decline. The prover aims for exact on common idioms or a named
  Intractable; any prover change needs a standalone justification. Correctness before
  features, with perf measured and kept.
- Prover bugs never block a branch or train (user, 2026-10-30). A false Proven or other
  prover hole found in review or while building: fix it in the branch only if the fix is
  straightforward; otherwise add a strict xfail test reproducing it, log it in the ledger
  as a follow-up, and let the branch proceed. Reviewers report them as NON-BLOCKING unless
  the branch itself introduced the hole. Arc priority (user, 2026-10-30): ship the
  conformance arc (match CLICK) first; prover side-issues get a second sweep afterwards.
- Core runtime, compiled kernel and CircuitPython codegen must agree.
- pyrung-authored terminal/log/exception text is ASCII only; "pyrung" is always lowercase.
- CHANGELOG (Unreleased) moves with behaviour changes, in the changelog shape the root AGENTS.md
  Writing section sets. Docs
  move only where a program author's understanding changes: state the rule with no history,
  and leave corners to the conformance suite, the source of truth (`docs/AGENTS.md`).

## Docs pass

At the end of a train (user, 2026-11-01), one docs-only `bob-builder` edits the docs diff as a
whole against `docs/AGENTS.md` and the root AGENTS.md Writing section: cut history and edge
cases, lead with code, keep gotchas in a reference area. A fresh `bob-reviewer` reads the result
against the same rules.

## Debugger follow-along review

A branch that changes how rungs evaluate (rung/condition evaluation, live row solving, executor,
scan order, edge or site memory) gets a second, focused `bob-reviewer` alongside the general one
(user, 2026-10-31). What an instruction writes or flags does not need one (user, 2026-11-01):
the debugger reads those values from the engine. Nor does an instruction's new internal memory
registered through `site_memory.instruction_memory()` (user, 2026-11-05): the DAP panes, reload
and history handle it generically (checked once, on timed-coils and drum-click). Edge or site
memory means a change to how that machinery works, not a new slot in it. Scope only the debugger, DAP
server (`src/pyrung/dap/`) and VS Code extension (`editors/vscode/pyrung-debug/`) still
following evaluation (stepping, condition trace, state panes, time travel, reload, recorded
sessions). Brief it with `editors/vscode/pyrung-debug/AGENTS.md` and
`docs/internal/debug-spec.md`.

## Compare gate: `how()` and history changes

A branch that moves `how()` (pilot planning, `core/analysis/pilot/`), history (seek, scan log,
checkpoints, `core/history.py`, `core/scan_log.py`), the harness or the state record
(`core/state.py`, `core/runner.py` forks) gets an old-vs-new answers compare. The compare is
one tool, `devtools/how_compare.py`, with one program set (`devtools/how_compare_programs.py`).
Do not write a per-branch compare: two branches regressed `how()` on 2026-11-04 because the
compares of the day had no entry-requirement program (avoid-fix) and never ran a timer
mid-count under a held enable (triage), and the 215-answer corpus and tumbler hold neither.

```
python -I devtools/mergeq/mergeq.py run --timeout 2000 --     uv run python devtools/how_compare.py --base dev --tree <worktree or merged tree>     [--full] [--only SUBSTRING] [--expected FILE]
```

(`--full`: outer `--timeout 11500`; the per-side default is 900 s fast, 5400 s full. The reviewer
hook runs a script only from a write root, so a reviewer cannot run this tool from `devtools/`;
the orchestrator or builder runs it and the reviewer reads the output.)

- `--base` is a git ref (exported with `git archive`) or a directory; `--tree` is a directory
  with `src/` or a ref. Each side runs in its own process at below-normal priority with a memory
  limit and a per-row time limit, and prints `pyrung.__file__`, which the tool checks.
- Fast set (default): 122 cases, 3.5 to 4 minutes a side. `--full`: 378 cases, see the timing
  in the ledger.
- The groups: a timer mid-count under a held enable (with and without a harness, bench link
  changed), entry-requirement programs warmed 0..2 scans, programs that really reach, long
  waits (minutes, past 32.767 s), holds, two routes, every avoid form (condition, callable index,
  `.get`, raising on a real scan), drums, goal already true, composite targets, and truth rows
  (held enable 120 s, held chain 3+2 min: declines today; must stay declined or become a correct
  REACHED).
- It prints: rows that differ (case, field, base, tree); how many rows REACHED on each side and
  which reach on neither (agreement there is no evidence); which reached plans were not checked
  against stepping and why; FLAGS by kind per row. PLAN_VS_STEP: a plan with no inputs or holds
  ends at a scan other than where stepping the session itself reaches the goal. Also
  TOTAL_VS_TIP, GOAL_FALSE_AT_TIP, REPLAY_DIFFERS, STEPPED_DIFFERS, NO_FORK, MISSED.
- Exit 1 holds the train: a selected case without a row on both sides, a difference not covered
  by an expected line, a flag kind new to the tree on a row, an ABORT/SETUP_ERROR row without a
  `run` line. A flag of the same kind already on the same base row does not hold; it is listed
  as "already on base" every run so it stays visible. Exit 2: setup problem, stale expected line.
- Expected differences: `<glob>[:field,...]  # reason` per line in
  `<home>/jobs/train<N>/how-compare-expected.txt`, passed with `--expected`. No field list covers
  every field except a REACHED -> error/declined change, which needs a line naming `status`.
  The reasons go to the user at landing.
- Run `devtools/how_compare.py` from the merged tree's worktree, so it uses that tree's
  `devtools/how_compare_programs.py`. A branch that renames public API breaks the base's copy
  (train10: `ImportError: Pulse`).
- `tests/test_fold_check_stop.py` also runs in plain `make test`. If a Python version needs a fix
  to the fold-check tool, include that fix in every train that runs the `test` stage.
- Add a program when a review finds a `how()` regression the set missed (same case name never
  reused; mark it `fast=False` unless it is cheap), and say so in the ledger.

The builder brief for a `how()`/history branch says: build the programs that show the change
(long waits, a mid-count, an entry requirement) into `how_compare_programs.py`, run the tool on
the base and the branch. The reviewer checks that the set has them. The gate list for the
branch is `lint,tumbler` at least, plus `fold` if it is a jit change (below). Bound every compare run (machine budget below).

## Models

- Builders: Opus at `effort: high` for prover / fold / kernel / executor / rung evaluation /
  multi-layer changes (user, 2026-11-01; set it on the spawn, the agent file does not);
  Sonnet for a single instruction, tests-only or docs-only.
- Reviewers: Opus for prover/fold/kernel diffs; Sonnet for leaf instructions, tests, docs.

## Machine budget

- Time base and branch on the same interpreter. The main checkout's venv is Python 3.13 and the
  worktrees' are 3.14, and a base timed in one against a branch timed in the other is skewed
  (2026-11-08: "dev 95 s" was 75 s on 3.14).
- Read a gating pytest's exit code directly. Piping it through `tail` without `pipefail` hides a
  fold-check session failure behind "N passed" (train14, 2026-11-08: an undeclared mismatch from a
  strict xfail). A strict xfail whose run fold-check compares wraps that run in
  `fold_check.deliberate()` when fold-check is installed.
- One heavy job machine-wide (the queue's lock); up to ~5 builders iterating on targeted tests in
  parallel. `test` and `fold` take 6 of the 8 cores (`PYTEST_WORKERS`, peak 2.4 GB). Pilot-heavy suites
  (`tumbler`, `prove`, `pilot`, `soundness`) stay in one process: user, 2026-11-03, pilot is intensive.
- Stage commands, timeouts and presets are in `devtools/mergeq/mergeq.toml`; measured
  durations are in each job's `done/<id>.json` (a full train should now take ~39 min, was 96).
- Only a jit change runs the `fold` stage (user, 2026-11-07, for iteration speed). A jit change
  touches `scan_trace.py`, `compiled_run.py`, `coast_run.py`, `kernel.py`, `read_trace.py`,
  `tests/fold_check.py`, or how timers, counters or clocks compute. A branch that only uses
  timers or adds rungs is not one.
  - A jit branch gates on `lint,tumbler,fold`, not `fast`. `fold` includes the prove and pilot
    tests, and `fast` has no tumbler. A change to the run's fast path broke a pilot golden past
    READY once.
  - A train holding a jit branch gates on `train-jit`. Every other train gates on `train`, which
    has no `fold` but runs `fold-new` on the files the train adds.
- A branch landing outside a train still gates on the `test` stage if it changes anything under
  `src/`: a codegen fix gated on lint, prove, pilot, tumbler and conf-cp turned two `tests/core`
  tests red on dev once, and it stayed red until the next `test` run (2026-11-02).
- Never run `make test-fuzz` or `make bench` in an orchestration session.
- Reviewers run scratch scripts and `node --test` only as
  `python -I devtools/mergeq/mergeq.py run --timeout N -- <command>`; the hook refuses the bare form
  (no memory bound) and allows ruff/ty read-only forms. For an investigation that must not run
  tests, spawn `bob-investigator` (read-only: no tests, no scripts) instead of a reviewer.
- A branch that adds a test file gets it checked under `--fold-check` by the `fold-new` stage in
  `fast`, `default`, `heavy` and `train` (before `test`). The file then meets lockstep without a
  full `fold` stage. Builders run `pytest --fold-check --fold-check-allow-vacuous <new file>` themselves (the flag
  judges mismatches only, as the stage does).
- Any run that exercises pilot or the prover, scratch scripts included, has a memory and time bound
  (user, 2026-11-02: an unbounded one can OOM the machine). Run it through
  `mergeq.py run --timeout N`, whose Job Object applies `job_memory_mb`. pytest also has its own
  `PYTEST_MEMORY_CAP_MB` cap, but a plain `python script.py` or `timeout N` has no memory bound. A
  change that adds anything accumulating per scan or per search (records, traces, kernels) gives it
  a cap in the code.
- The conformance stages use the local soft PLC; no agent runs `-m conformance` while a job's
  conf stage runs (the hook refuses it for agents anyway).
- A branch that adds or changes a conformance case, or changes what an instruction computes, adds
  `conf` to its own gate (`--stages lint,docs,fold-new,prove,pilot,conf`). In-process cases run on
  fixed 10 ms scans. The soft PLC runs wall-clock scans of 10-40 ms and builds the apart groups
  into one program. Train 23 (2026-11-10) found three failures only there, after five branches
  were READY on `fast`: a confirmed flasher case broken by longer scans, a case whose one-scan tick
  equalled its preset, and a case needing its part alone.
- A train or branch that changes only conformance cases (`tests/conformance/`, plus comment or
  docstring text under `src/`) gates on `lint,docs,fold-new,conf,conf-cp`. Add `test` when it
  changes `src/pyrung/conformance/`. Soundness, tumbler and hypothesis never read those files, and
  the `train` preset spends about an hour on them (user, 2026-11-10).

## Ground truth

The user runs hardware rounds on a CLICK. Collect questions only hardware can answer (probe
cases, firmware version) and list them in milestone reports; changed conformance subroutines
must be re-pasted before a round, so batch case-layout changes.

## Gotcha

Agent `isolation: worktree` fails in this repo (cwd `documents` vs git `Documents` case
mismatch): always use `mergeq worktree new` and brief the builder with the path.
