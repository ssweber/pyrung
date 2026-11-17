# BOB profile: pyrung

Repo-specific rules for the bob plugin (`/bob:bob`). The plugin reads this file and the
`config.toml` beside it.

## Paths

- Main checkout `C:/Users/Sam/Documents/GitHub/pyrung`, base branch `main` (since 2026-11-15;
  there is no dev trunk). Each branch goes into main through a PR. The session squash-merges its
  own PR once CI is green on the head commit and a fresh reviewer has passed that commit. A PR
  that waits on Sam (his judgment, CLICK software, a real PLC) stays open (user, 2026-11-15).
  Never push main. Only Sam tags, releases and publishes.
- Worktrees are `../pyrung-wt/<name>` on `bob/<name>`. The mergeq home is `../pyrung-wt/buildbot`
  (queue, logs, results).
- `<jobs>` is `jobs_dir` in config.toml (`../pyrung-wt/buildbot/jobs`). It holds
  `<jobs>/<id>/{HANDOFF,REVIEW,LEDGER}.md`, and the read-only agents may write there.
- Plan: `scratchpad/roadmap.md`. An arc doc lives in `<jobs>/<arc>/` (out of git, so give
  builders the absolute path). One arc doc per arc: fold rulings and leftovers into it, not side notes. When the
  arc closes, its open items move into the roadmap.
- Builder's local gate: `make lint` before the final commit, plus targeted tests. The queue runs
  the rest.

## Trains into main

The queue's local `main` must equal `origin/main` when a train is submitted. Nothing else moves
local `main`, so after each merge on GitHub run, in the main checkout on `main`:

```
git fetch origin && git merge --ff-only origin/main
```

A green train goes to GitHub as one PR of the exact tested merge, merged with a merge commit:

```
git push origin refs/mergeq/train<N>:refs/heads/bob/train<N>
gh pr create --base main --head bob/train<N> --title "..." --body-file <jobs>/train<N>/PR.md
gh pr merge <PR> --merge --delete-branch --match-head-commit <merged_sha from done/train<N>.json>
git update-ref -d refs/mergeq/train<N>
```

Merge only when CI is green on that head and a fresh reviewer has passed the train. If `main`
moved after the submit, GitHub asks for an update first, and that is a new tree. Gate it again.

## Correctness contract (put in every builder and reviewer brief)

- Match real CLICK behaviour; hardware-recorded expectations in `tests/conformance/` are
  authoritative. `pending="<STEP>"` cases are strict xfails; the fixer removes the marker in
  the commit that makes the case pass (`tests/conformance/AGENTS.md`).
- Fold must equal stepping or decline. Correctness before features, with perf measured and
  kept.
- The prover's exhaustive search is gone (user, 2026-11-15). Its static facts stay, because
  how() and validation read them. A change to them must leave what pilot and validation see
  the same, or say why it differs.
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
R --timeout 2000 -- uv run python devtools/how_compare.py --base main --tree <worktree or merged tree>     [--full] [--only SUBSTRING] [--expected FILE]
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
  parallel. `test` and `fold` take 6 of the 8 cores (`PYTEST_WORKERS`, peak 2.9 GB). Pilot-heavy suites
  (`tumbler`, `prove`, `pilot`) stay in one process: user, 2026-11-03, pilot is intensive.
- Stage commands, timeouts and presets are in `.claude/bob/config.toml`; measured
  durations are in each job's `done/<id>.json` (a full train should now take ~39 min, was 96).
- Only a jit change runs the `fold` stage (user, 2026-11-07, for iteration speed). A jit change
  touches `scan_trace.py`, `compiled_run.py`, `coast_run.py`, `kernel.py`, `read_trace.py`,
  `tests/fold_check.py`, or how timers, counters or clocks compute. A branch that only uses
  timers or adds rungs is not one.
  - A jit branch gates on `lint,tumbler,pilot,fold`, not `fast`. `fold` includes the prove and
    pilot tests, except the slow half of `test_pilot_long_wait.py`. `pilot` runs that file whole.
    `fast` has no tumbler. A change to the run's fast path broke a pilot golden past READY once.
  - A train holding a jit branch gates on `train-jit`. Every other train gates on `train`, which
    has no `fold` but runs `fold-new` on the files the train adds.
- A branch landing outside a train still gates on the `test` stage if it changes anything under
  `src/`: a codegen fix gated on lint, prove, pilot, tumbler and conf-cp turned two `tests/core`
  tests red on dev once, and it stayed red until the next `test` run (2026-11-02).
- Never run `make test-fuzz` in an orchestration session.
- Reviewers run scratch scripts and `node --test` only as
  `R --timeout N -- <command>` (the plugin's `run.py`); the hook refuses the bare form
  (no memory bound) and allows ruff/ty read-only forms. For an investigation that must not run
  tests, spawn `bob:bob-investigator` (read-only: no tests, no scripts) instead of a reviewer.
- A branch that adds a test file gets it checked under `--fold-check` by the `fold-new` stage in
  `fast`, `default`, `heavy` and `train` (before `test`). The file then meets lockstep without a
  full `fold` stage. Builders run `pytest --fold-check --fold-check-allow-vacuous <new file>` themselves (the flag
  judges mismatches only, as the stage does).
- Any run that exercises pilot or the prover, scratch scripts included, has a memory and time bound
  (user, 2026-11-02: an unbounded one can OOM the machine). Run it through
  `R --timeout N`, whose Job Object applies `job_memory_mb`. pytest also has its own
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

When a reading asks pyrung for an odd rule, suspect the paste before you build the rule. An odd rule
is one that treats a register differently from a constant, or makes a value nobody would type
behave strangely. First check that laddercodec writes that instruction the way CLICK's editor does.
If you can't settle it, tell the user before building the rule. From 2026-11-05 to 2026-11-11,
pyrung's drums held a DS step preset per step, and a DS preset of 0 never ended its step. Both rules
came from drums that laddercodec had mis-encoded.

## Gotcha

Agent `isolation: worktree` fails in this repo (cwd `documents` vs git `Documents` case
mismatch): always use `R worktree new` and brief the builder with the path.

Before submitting a train, run `uv run python devtools/conf_status.py` in its worktree and compare
it with main's. A part that is done on main and "changed since it was confirmed" on the train means a
branch carries an old `tests/conformance/confirmed.toml`.
