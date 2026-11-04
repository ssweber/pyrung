# BOB profile: pyrung

Repo-specific rules for the /bob skill. Another repo replaces this file.

## Paths

- Main checkout `C:/Users/Sam/Documents/GitHub/pyrung`, base branch `dev` (trunk; the user
  squash-merges dev into main themselves; never touch main). Push dev to origin after each
  landing (user, 2026-10-30); never push bob/* branches or main.
- Worktrees `../pyrung-wt/<name>` on `bob/<name>`; mergeq home `../pyrung-wt/buildbot`
  (queue, logs, results, `jobs/<id>/{HANDOFF,REVIEW,LEDGER}.md`).
- Current arc doc: `scratchpad/win-back/HANDOFF.md` (untracked, so give builders the absolute
  main-checkout path).

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
- CHANGELOG (Unreleased) moves with behaviour changes, one or two sentences per bullet. Docs
  move only where a program author's understanding changes: state the rule with no history,
  and leave corners to the conformance suite, the source of truth (`docs/AGENTS.md`).

## Docs pass

At the end of a train (user, 2026-11-01), one docs-only `bob-builder` edits the docs diff as a
whole against `docs/AGENTS.md`: cut history and edge cases, lead with code, keep gotchas in a
reference area. A fresh `bob-reviewer` reads the result against the same rules.

## Debugger follow-along review

A branch that changes how rungs evaluate (rung/condition evaluation, live row solving, executor,
scan order, edge or site memory) gets a second, focused `bob-reviewer` alongside the general one
(user, 2026-10-31). What an instruction writes or flags does not need one (user, 2026-11-01):
the debugger reads those values from the engine. Scope only the debugger, DAP
server (`src/pyrung/dap/`) and VS Code extension (`editors/vscode/pyrung-debug/`) still
following evaluation (stepping, condition trace, state panes, time travel, reload, recorded
sessions). Brief it with `editors/vscode/pyrung-debug/AGENTS.md` and
`docs/internal/debug-spec.md`.

## Compare gate: `how()` and history changes

A branch that moves `how()` (pilot planning, `core/analysis/pilot/`) or history (seek, scan log,
checkpoints, `core/history.py`, `core/scan_log.py`) gets an old-vs-new answers compare, and the
compare's program set must include programs with long waits (timers and dwells of minutes to
hours, a drum step, a `Pulse` of 60 s), not only the corpus goldens. Two debugger blockers got
past tumbler, the 215-answer corpus compare and lockstep, because none of them held a long wait
(2026-11-03). Put it in the builder brief (build the long-wait programs, run the compare on the
base and the branch) and the reviewer brief (check that the set has them), and in the gate list
for the branch: `lint,tumbler,fold` at least. Bound every compare run (machine budget below).

## Models

- Builders: Opus at `effort: high` for prover / fold / kernel / executor / rung evaluation /
  multi-layer changes (user, 2026-11-01; set it on the spawn, the agent file does not);
  Sonnet for a single instruction, tests-only or docs-only.
- Reviewers: Opus for prover/fold/kernel diffs; Sonnet for leaf instructions, tests, docs.

## Machine budget

- One heavy job machine-wide (the queue's lock); up to ~5 builders iterating on targeted tests in
  parallel. `test` and `fold` take 6 of the 8 cores (`PYTEST_WORKERS`, peak 2.4 GB). Pilot-heavy suites
  (`tumbler`, `prove`, `pilot`, `soundness`) stay in one process: user, 2026-11-03, pilot is intensive.
- Stage commands, timeouts and presets are in `devtools/mergeq/mergeq.toml`; measured
  durations are in each job's `done/<id>.json` (a full train should now take ~39 min, was 96).
- A branch touching `scan_trace.py`, `compiled_run.py` or `coast_run.py` gates on
  `lint,tumbler,fold`, not `fast` (`fold` includes the prove and pilot tests): `fast` has no tumbler, and a change to the run's
  fast path broke a pilot golden past READY once.
- A branch landing outside a train still gates on the `test` stage if it changes anything under
  `src/`: a codegen fix gated on lint, prove, pilot, tumbler and conf-cp turned two `tests/core`
  tests red on dev once, and it stayed red until the next `test` run (2026-11-02).
- Never run `make test-fuzz` or `make bench` in an orchestration session.
- Reviewers run scratch scripts and `node --test` only as
  `python -I devtools/mergeq/mergeq.py run --timeout N -- <command>`; the hook refuses the bare form
  (no memory bound) and allows ruff/ty read-only forms. For an investigation that must not run
  tests, spawn `bob-investigator` (read-only: no tests, no scripts) instead of a reviewer.
- A branch that adds a test file gets it checked under `--fold-check` by the `fold-new` stage in
  `fast`, `default` and `heavy` (before `test`), so the file does not first meet lockstep in the
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

## Ground truth

The user runs hardware rounds on a CLICK. Collect questions only hardware can answer (probe
cases, firmware version) and list them in milestone reports; changed conformance subroutines
must be re-pasted before a round, so batch case-layout changes.

## Gotcha

Agent `isolation: worktree` fails in this repo (cwd `documents` vs git `Documents` case
mismatch): always use `mergeq worktree new` and brief the builder with the path.
