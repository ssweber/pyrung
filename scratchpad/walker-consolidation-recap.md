# Walker Consolidation — Recap

Direction settled across this discussion, briefest form. One sentence: static
advice in through a registry, one agenda loop in the middle, verified plans and
a triangle table out.

Supersedes `walker-fable-feedback.md` (deleted): its four bugs and holds landed
(fb15b87…8e73c56); its surviving items are folded into §§2–5 below, marked
*(from feedback)*.

## 1. Bugs and holds (landed)

Four concrete fixes in `walk.py` (dropped nogoods, unlink on replay forks, dead
nogood-hint transition, dedup). Holds registered as first-class commitments —
(input, value, goal protected) — with selective release converting
clobber-recovery into prevention.

`cause()` stays program-pure. It explains the program; holds are the walker's
own hand. Self-conflicts (a cause()-named blocker that is a held input) are
classified one layer up — `_classify_blockers(goals, holds)` — and routed to
the divest probe or a reorder. They never enter the NoGoodStore: nogoods record
program facts only.

## 2. One loop instead of four

The four solve loops (`plan_walk` compound loop, `_walk_to_goal` prereq tail,
`_recover_via_oracle`, `_check_residuals`) differ only in where goals come
from, not in what they do.

- **From POCL, the vocabulary.** Every work item is an *open condition*
  (unachieved goal) or a *threat* (a steer that would break a hold). Every
  response is one resolver: establish, reorder, divest (white knight), reject.
  Holds are the causal links; threats are detected by construction.
- **From PDR, the mechanics.** One deepest-first queue of items
  (goal, depth, provenance), one global fork/scan budget, stale items skipped,
  honest "budget exhausted" as the Phase-5 NotFound trigger.
- **Not borrowed:** POCL's least-commitment ordering (would force replays,
  surrendering the forward-fork oracle) and PDR's frame/induction machinery
  (assumes a symbolic transition relation).

Resolver preference order (e.g. divest-first for latch-heavy programs) is a
tuning knob, not structure — programs differ in which resolver wins, not in
loop shape.

Three representation choices belong *inside* this consolidation — they are the
loop's data structures, not add-ons *(from feedback)*:

- **Plan as tree, not flat list.** Each solved goal returns a node
  `(goal, actions, holds, children)`; flatten once at `Path`-build time.
  Backjump drops the subtree for the goal that diverged; Phase-5
  `NotFound(best partial plan, first failing edge)` prints the partial tree;
  the triangle table (§4) derives from the flattened tree. The agenda's
  `(goal, depth, provenance)` covers the search side; this is the output side.
- **One fold monitor.** `_apply_steer` (watch one governing value) and
  `_apply_steer_compound` (sequential goal-list iteration) are the same
  function parameterized by a `done(state)` predicate. Phase 3's three items —
  path-sequence divergence, must-stay violation, deadline race — become
  monitors plugged into this one point, not three code paths.
- **`Diagnosis` spec'd as a consumer, not a mechanism.** The return type reads
  the plan tree + holds + nogoods + pass journal; the global budget supplies
  the honest "budget exhausted" trigger. Spec the type during consolidation so
  the tree representation has to carry what diagnosis needs.

## 3. The needle-mover: nogood generalization

Today's nogoods are exact cause()-named assignments; they rarely recur, so
`is_blocked` starves and seen-keys fragment. PDR's lesson: after learning a
failure, drop assignments and re-test — the simpler version that still fails is
the real nogood. PDR needs a SAT solver for the re-test; the walker forks and
runs. Broader nogoods prune more on deep interlock chains. This is the one
borrowed idea that extends reach on harder programs rather than cleaning code.

Residual risk *(from feedback)*: the store stays shared per `plan_walk` and
add-only, so accumulated blocking names fragment `seen`-keys for *unrelated*
goals. Generalization shrinks each blocking set but doesn't scope the store.
If fragmentation still bites: project per-goal — only nogoods whose
`(from, to)` involves the current governing tag.

## 4. Triangle table (output, orthogonal)

Derived once from holds + steps at Path-build time. `kernel(i)` = conditions
that must still hold for steps i..n to remain valid. One structure gives:

- Phase-3 must-stay monitoring ("is the highest true kernel still true"),
- window characterization (the rows a hold spans; the divest is the row it
  leaves),
- divergence recovery (resume from the highest true kernel),
- operator-legible `how()` output (Fikes–Hart–Nilsson 1972 / PLANEX).

Free: the data already exists; it is a table not yet built.

## 5. Pass pipeline for the walker

Mirror `prove/`'s idiom — registered passes run once per `plan_walk`, freeze a
walk context (the deferred `_WalkContext` lands here), journal their decisions
(à la `_JournalBuilder`) so diagnosis can report which advice applied.

Build-once goes with freeze-once *(from feedback)*: `_build_jump_context` does
a whole-program SP-tree scan and is currently rebuilt at every recursion level
× recovery iteration × independent walk; everything in it except
`normal_dt`/`profile_fb_names` is static per walk. Same for `_governing`'s
`_probe_steps` results — memoize per tag. Both land in the frozen context.

Each pass declares its **kind**, and the kind is its proof obligation:

| Kind | Examples | Ablation property |
|---|---|---|
| Ordering | edge/level sort (steady enablers before triggers), destructive-writer scan (`~A → reset(B)` forces A-then-B), window suspects, flaw selection | Disable freely: same verdicts, more recovery iters/forks |
| Narrowing | steer alphabet, cone filters | Must be conservative (over-approximate); disabling only widens |

The completeness matrix writes itself: one test parametrized over the registry,
disable each pass, assert by kind ("same verdict, or budget-exhausted" — under
finite budgets, slower can mean None, so ablation runs raise budgets or accept
exhaustion). Every new pass gets a matrix row by construction.

Structural guarantees:

- Passes get `(program, pdg)`, run before the walk, never again; no handle to
  the agenda, work fork, or stores. A heuristic physically cannot become a
  parallel path — the only door into the loop is advice.
- Runtime learning (nogoods, holds) stays out of the registry. Everything
  load-bearing lives on the loop side of the line; that is what keeps the
  ablation property true.

Soundness is untouched throughout: replay verification carries it, so no pass
can break it. Passes touch completeness only.

## Guiding tests

- Anything readable from the SP-tree or PDG → ordering advice via the registry.
- Anything knowable only by running → learned nogood in the loop.
- Every new mechanism: a resolver, a flaw source, or a pass — never a new loop.
