# Concrete Phase Batch Efficiency — Implementation Plan

## Goal
Reduce concrete phase wall time by implementing 3 low-effort optimizations + 1 medium one.

## Current Architecture (concrete.py)

### `_can_elide(candidate, retained)` — the hot loop
```
1. _reachable_stateful_frontier → find observed tags
2. _scoped_dependencies → retained_names, input_names, hidden_stateful
3. Build vary_domains = hidden_stateful + (candidate,)
4. Budget check: group_product * vary_product > 200k → bail
5. Triple-nested loop:
   for retained_values:        # outer: retained tag combos
     for input_assignments:    # middle: ND input combos  
       expected = None
       for vary_values:        # inner: hidden + candidate combos
         outcome = _scan(full_entry, observed)
         if outcome != expected → return False (FAIL)
   return True (PASS)
```

### `_scan(entry_values, observed)` — the kernel call
- Creates fresh kernel, sets entry, steps, reads observed
- If warm_memory: runs a second kernel with warm memory, checks agreement
- Returns tuple of observed tag exit values, or None on memory disagreement

### `_pass_concrete_batch(ctx)` — pipeline entry
- Creates `_ConcreteStateElider` (expensive: pilot scans, warm memory discovery)
- Loops candidates until fixed point
- Calls `_can_elide` per candidate, removes proven ones from retained

### `elide()` — the other entry (used standalone, not via pipeline)
- Same loop, but with batch removal and round tracking

---

## Optimization 1: Fast-Path Coverage Skip (§3.3)

**What:** If a candidate was never written across pilot scans (`_coverage.written_tags`), 
its exit is always its default value — trivially elidable without full proof.

**Where it already half-exists:** `_is_concrete_candidate` already checks 
`if name not in self._written_tags: return False` — but this REJECTS unwritten tags 
as not-a-candidate, leaving them RETAINED. That's backwards for our purposes.

Wait — re-reading: `_written_tags = static_writers | dynamic_writers`. Static writers 
come from `graph.writers_of` (the PDG). So a tag that appears in NO rung's write set 
AND was never written in pilot scans. These are truly never-written tags.

Actually, the current logic: `_is_concrete_candidate` returns False for unwritten tags, 
meaning they're skipped by the candidate loop, meaning they stay in `retained`. These 
tags SHOULD be elidable (their exit is always default), but the current code doesn't 
elide them — it just doesn't try to prove them.

**Fix:** Before the main candidate loop, pre-filter: any tag in state_basis where 
`name not in self._written_tags` AND not in `_continued_source_tags` AND no PENDING 
→ auto-elide it. It's never written, so exit == entry default always.

Wait, that's not quite right either. "Never written" means exit == entry (unchanged). 
If entry can vary, exit varies too. So it's NOT elidable — it's identity.

Hmm, re-think. The coverage skip says: if a candidate was never written across pilot 
scans, its exit is always DEFAULT. But pilot scans use `force_rung_enable=True`, so 
all rungs fire. If even with all rungs firing the tag was never written, it genuinely 
is never modified by any instruction. Its exit == its entry. So varying entry DOES 
change exit. That means it's NOT elidable... unless no retained tag reads it.

Actually wait — the frontier check handles this. If nothing in retained reads the 
candidate, then `observed` is empty and `_can_elide` returns True at line 442 
(after the _hidden_entry_matters check). The issue is we still compute the frontier 
and scoped deps for never-written candidates.

Actually no — `_is_concrete_candidate` already filters out unwritten tags. They never 
enter the candidate loop. They stay retained. This is correct but conservative: a 
never-written tag whose value nobody in retained reads should be elidable.

**Revised understanding:** The fast-path isn't about never-written tags (those are 
already filtered). It's about tags that ARE written by some rung (so they pass 
`_is_concrete_candidate`) but whose writes always produce the default value across 
all pilot scan combos. That is: `before[name] != after[name]` never triggered, 
but the tag IS in `graph.writers_of`.

So: tag is in `static_writers` (PDG says some rung writes it) but NOT in 
`_coverage.written_tags` (pilot scans never saw it change). This means the write 
is dead code or the guard is never satisfied under forced-true compilation... 
wait, forced_true forces all rungs to fire. So if a tag has a static writer and 
forced-true didn't change it, that means the instruction wrote the same value as 
entry (e.g., `copy(tag.default, tag)` or `latch(tag)` when tag is already True).

Hmm, this is more subtle than I thought. The tag's exit depends on its entry: 
`latch(T)` with forced rung → exit is True regardless of entry. But if entry was 
True, before==after so it's not in written_tags. If entry was False, 
before!=after so it IS in written_tags.

OK but `_collect_forced_true_coverage` uses BOTH default and alternate seeds. 
So for a bool tag, it tries entry=False AND entry=True. If `latch(T)` fires, 
the False→True case catches it. So written_tags should include it.

**Real fast-path:** Tags in `static_writers` but NOT in `coverage.written_tags` 
(with both seeds tested) are tags where the write instruction provably produces 
the same value as entry for all tested combos. These are either:
- Dead writes (guard never satisfiable — but forced_true overrides guards)
- Identity writes (copy X to X, or latch when already latched)

For the forced-true case, guards are forced on, so dead writes aren't the issue. 
It's identity writes. If the tag's exit always equals its entry across all tested 
combos, it's not that the tag is elidable — it's that it's the identity function. 
Identity means exit==entry always, which means it MUST be in the key (it carries 
information forward).

Hmm, I think I'm overcomplicating this. Let me re-read the checklist:

> If a candidate was never written across pilot scans 
> (`_collect_forced_true_coverage`), its exit is always default. 
> Trivially elidable without full proof. Pre-filter before cone computation.

"exit is always default" — this claim is ONLY true if the tag was never written 
AND its entry is always default (i.e., no other rung copies a non-default into it). 
If entry can be non-default and nothing writes it, exit==entry==non-default.

I think the checklist item is slightly wrong as stated, or it's assuming that 
"never written" tags start at default and stay at default. In BFS they start at 
default (initial state). But another tag could copy a value into them... wait, 
if they're "never written" that can't happen.

OK so: never written by ANY instruction, ever. Entry is always default (since 
nothing can change it). Exit is always default (since nothing writes it). 
Therefore trivially elidable. BUT — these tags already fail 
`_is_concrete_candidate` (not in `_written_tags`) and stay retained. They should 
instead be auto-elided.

**The fix is in `_is_concrete_candidate` or the loop around it:**

Currently: unwritten → not a candidate → stays retained.
Should be: unwritten → auto-elide (remove from retained).

But is this sound? If a tag is never written, its value in every reachable state 
is its default. So it adds no information to the state key. Removing it is safe.

UNLESS: the tag starts with a non-default value via force/patch, or it's an 
external input. But external inputs are in nondeterministic_dims, not 
stateful_dims. And forces are not part of BFS.

**Conclusion:** Tags in stateful_dims that are never written (not in _written_tags) 
AND pass the other _is_concrete_candidate checks (no PENDING, not continued-source) 
should be auto-elided without proof. Currently they're silently retained.

Where to implement: In `_pass_concrete_batch` or `elide()`, before the main loop, 
scan for tags where `_is_concrete_candidate` would fail only because of the 
written_tags check, and elide them.

Actually simpler: add a new pre-filter in `_pass_concrete_batch` right after 
creating the elider:

```python
# Fast-path: never-written tags are always at default → elidable without proof
for tag_name in sorted(abstract_retained):
    if tag_name not in concrete_elider._written_tags:
        if PENDING not in ctx.stateful_dims.get(tag_name, ()):
            if tag_name not in concrete_elider._continued_source_tags:
                retained.discard(tag_name)
                ctx.elided[tag_name] = "concrete_never_written"
```

---

## Optimization 2: Cone Pruning from Abstract Results (§3.2)

**What:** Tags the abstract phase already resolved (elided) shouldn't appear in 
`vary_domains` during concrete proofs. They're known to be functions of 
retained + inputs, so varying them independently adds no information.

**Where:** `_scoped_dependencies` builds `hidden_stateful` by walking the write 
graph backward from candidate + observed. When it encounters a tag in `state_basis` 
that's NOT in `retained`, it adds it to `hidden_stateful`.

But `state_basis` is set from `ctx.stateful_dims` — which has ALREADY been pruned 
by the abstract phase. So abstract-elided tags are NOT in state_basis and should 
NOT appear in hidden_stateful.

Wait — look at `_pass_concrete_batch`:
```python
concrete_elider = _ConcreteStateElider(
    ...
    ctx._original_stateful_dims,     # ← FULL dims, not pruned
    ...
    state_basis=frozenset(ctx.stateful_dims),  # ← pruned basis
)
```

And in `_scoped_dependencies`:
```python
if src in self._state_basis and src not in retained_set and src != candidate:
    hidden_stateful.add(src)
```

`_state_basis` is `frozenset(ctx.stateful_dims)` which IS the pruned set. So 
abstract-elided tags are already excluded from hidden_stateful.

But wait — `self._stateful_dims` is `_original_stateful_dims` (line 228/657). 
And `vary_domains` uses `self._stateful_dims[name]`. So the domains come from 
the original full set. That's fine — the names are filtered by `_state_basis`.

**Conclusion:** Cone pruning is ALREADY IMPLEMENTED via the `state_basis` parameter. 
Abstract-elided tags don't enter hidden_stateful because they're not in state_basis.

Double-check: in `_ConcreteStateElider.__init__`:
```python
self._state_basis = (
    frozenset(self._stateful_dims)
    if state_basis is None
    else frozenset(state_basis) & frozenset(self._stateful_dims)
)
```

When called from `_pass_concrete_batch`, `state_basis=frozenset(ctx.stateful_dims)` 
which is the post-abstract pruned set. ✓

So this optimization is already done. We can skip it.

---

## Optimization 3: Early Termination Propagation (§3.4)

**What:** When a candidate fails proof, mark it immediately and exclude from 
further perturbation in the current round.

**Current behavior in `_pass_concrete_batch`:**
```python
for tag_name in sorted(snapshot):
    if not concrete_elider._is_concrete_candidate(tag_name):
        continue
    if tag_name not in abstract_retained:
        continue
    compare_retained = frozenset(snapshot - {tag_name})
    if concrete_elider._can_elide(tag_name, compare_retained):
        retained.discard(tag_name)
        ctx.elided[tag_name] = "concrete_batch"
        changed = True
```

When a candidate fails, it stays in `snapshot` (used for all remaining candidates 
in this round). The next candidate's `compare_retained` still includes failed 
candidates. This is correct — failed candidates ARE retained.

The optimization is about the `elide()` method's batch mode. When `_ELISION_BATCH_REMOVE` 
is True, removable candidates accumulate and are batch-removed at round end. Failed 
candidates don't affect anything — they just stay in retained.

Actually, re-reading the checklist:
> When a candidate fails, mark immediately and exclude from further perturbation 
> in current round. Baseline scans still run but per-candidate budget shrinks.

This is about the INNER loop of `_can_elide` for other candidates. If candidate A 
fails, A stays retained. When proving candidate B, A is in the retained set and its 
domain is enumerated in `retained_domains`. That's correct and necessary.

The optimization would be relevant in a shared-baseline architecture where multiple 
candidates are proven in the same enumeration pass. In the current per-candidate 
architecture, there's nothing to propagate — each `_can_elide` call is independent.

**Conclusion:** This optimization is only meaningful WITH shared baseline batch proofs 
(§3.1). It doesn't apply to the current per-candidate architecture. Skip for now, 
implement with §3.1 later.

---

## Optimization 4: Shared Baseline Batch Proofs (§3.1) — MEDIUM EFFORT

**What:** Instead of K independent `_can_elide` calls, union dependency cones and 
run one baseline per (retained × input) combo, then perturb each candidate against it.

**Current cost:** For K bool candidates with shared retained/input space of M combos:
- Per candidate: M × 2 scans (vary candidate False and True)
- Total: K × M × 2 scans

**Proposed cost:** 
- M baseline scans (all candidates at default)
- K × M × 1 perturb scans (each candidate at alternate value)
- Total: M × (1 + K) scans ≈ half

**Complications:**
1. Each candidate has different `observed` tags and different `hidden_stateful` 
   (vary_domains). The "shared baseline" only works if all candidates share the 
   same observation and enumeration space.

2. `_reachable_stateful_frontier` and `_scoped_dependencies` are per-candidate. 
   Different candidates have different dependency cones.

3. The baseline outcome must be comparable across candidates. If candidate A 
   observes tags {X, Y} and candidate B observes {Y, Z}, their baselines aren't 
   directly comparable.

**Approach:** Group candidates by compatible observation sets. Within a group, 
union the dependency cones. Run shared baseline over the unioned space. Then 
perturb each candidate individually.

This is genuinely medium effort and touches the core proof architecture. Let's 
do the easy wins first.

---

## Revised Implementation Order

1. **Fast-path coverage skip** — Pre-filter never-written tags as auto-elidable 
   before the main candidate loop. ~15 lines of code.

2. ~~Cone pruning~~ — Already implemented via state_basis. No work needed.

3. ~~Early termination~~ — Only meaningful with shared baseline. Skip.

4. **Shared baseline batch proofs** — Medium effort. Rewrite proof loop to 
   share baseline scans across candidates. Biggest payoff.

So really it's: one easy win (#1) and one medium refactor (#4).

Let me look for other quick wins I might have missed...

### Additional quick win: Budget/domain check caching

In `_can_elide`, every call recomputes `_reachable_stateful_frontier` and 
`_scoped_dependencies`. These involve BFS over the program graph. For round-based 
iteration where `retained` changes between rounds, this is necessary. But WITHIN 
a round (batch mode), `retained` is the same `snapshot` for all candidates. We 
could cache the frontier/dependency results per candidate for the round.

Actually, `compare_retained = frozenset(snapshot - {tag_name})` — this differs per 
candidate (each removes itself). So the frontier IS candidate-specific. No caching 
opportunity here.

### Additional quick win: Skip proof for domain-1 candidates

If a candidate has a domain of size 1 (only one possible value), it's trivially 
elidable — there's nothing to vary. Currently this goes through the full proof 
machinery where the inner loop runs exactly once and returns True.

Check: does this happen in practice? Unlikely for stateful dims, which must have 
≥2 values to matter. But worth a one-line check.

---

## Final Plan

### Phase 1: Fast-path never-written elision
- In `_pass_concrete_batch`, before the main loop, auto-elide tags that are in 
  `abstract_retained` but not in `concrete_elider._written_tags` (and pass the 
  other eligibility checks)
- Add progress message for these
- Also do the same in `elide()` for the standalone path

### Phase 2: Shared baseline batch proofs
- This is the big one. Needs careful design. See §3.1 in the techniques doc.
- Group candidates by observation compatibility
- Union dependency cones within groups  
- Run shared baseline, perturb individually
- Maintain soundness: each proof must still vary all other candidates

Let me look at the actual numbers to understand the payoff. How many candidates 
typically go through concrete proof? How much time is spent in `_scan`?
