# BOB profile: pyrung

Repo-specific rules for the /bob skill. Another repo replaces this file.

## Paths

- Main checkout `C:/Users/Sam/Documents/GitHub/pyrung`, base branch `dev` (trunk; the user
  squash-merges dev into main themselves; never touch main).
- Worktrees `../pyrung-wt/<name>` on `bob/<name>`; mergeq home `../pyrung-wt/buildbot`
  (queue, logs, results, `jobs/<id>/{HANDOFF,REVIEW,LEDGER}.md`).
- Current arc doc: `scratchpad/conformance_review/BOB-HANDOFF.md` (FINDINGS.md, DESIGN.md,
  ROUND2.md beside it; some untracked, so give builders absolute main-checkout paths).

## Correctness contract (put in every builder and reviewer brief)

- Match real CLICK behaviour; hardware-recorded expectations in `tests/conformance/` are
  authoritative. `pending="<STEP>"` cases are strict xfails; the fixer removes the marker in
  the commit that makes the case pass (`tests/conformance/AGENTS.md`).
- Fold must equal stepping or decline. The prover is exact on common idioms or a named
  Intractable, never a false Proven; any prover change needs a standalone justification.
  Correctness before features, with perf measured and kept.
- Core runtime, compiled kernel and CircuitPython codegen must agree.
- pyrung-authored terminal/log/exception text is ASCII only; "pyrung" is always lowercase.
- Docs + CHANGELOG (Unreleased) move with behaviour changes.

## Models

- Builders: Opus for prover / fold / kernel / executor / rung evaluation / multi-layer
  changes; Sonnet for a single instruction, tests-only or docs-only.
- Reviewers: Opus for prover/fold/kernel diffs; Sonnet for leaf instructions, tests, docs.

## Machine budget

- Memory is the constraint (prover/fold/pilot suites are heavy). One heavy job machine-wide
  (the queue's lock); up to ~5 builders iterating on targeted tests in parallel.
- Stage commands, timeouts and presets are in `devtools/mergeq/mergeq.toml`; measured
  durations are in each job's `done/<id>.json` (a full train has run ~45 min).
- Never run `make test-fuzz` or `make bench` in an orchestration session.
- The conformance stages use the local soft PLC; no agent runs `-m conformance` while a job's
  conf stage runs (the hook refuses it for agents anyway).

## Ground truth

The user runs hardware rounds on a CLICK. Collect questions only hardware can answer (probe
cases, firmware version) and list them in milestone reports; changed conformance subroutines
must be re-pasted before a round, so batch case-layout changes.

## Gotcha

Agent `isolation: worktree` fails in this repo (cwd `documents` vs git `Documents` case
mismatch): always use `mergeq worktree new` and brief the builder with the path.
