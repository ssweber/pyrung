# Walker fixes + holds as first-class plan output

## Context

A review of `src/pyrung/core/analysis/prove/walk.py` found four concrete bugs and one structural insight. The bugs: a dropped `nogoods` store on the most clobber-prone path, the `unlink=` fault model not applied during plan verification, a dead nogood-hint query, and minor duplication. The insight: the walker's dominant failure mode (serial clobber) is self-inflicted — `_steer_prefix`'s pulse branch releases **all** held external inputs (walk.py:1476), so later sub-walks break conditions earlier sub-walks established, and `_try_independent_walks` + `_recover_via_oracle` exist largely to work around that. Representing the walker's own commitments — a **hold** = (external input, value, the goal it protects) — converts recovery into prevention, lets `_try_independent_walks`' hold-mining be reused, and surfaces "hold X while…" in `how()` output. Holds describe the walker's own hand (sticky external inputs fully under its control), so this does not violate the "static analysis is a prior, never correctness-bearing" principle.

Verified safe foundations (Explore agent): all `Path`/`ReachabilityStep` constructions use keyword args; DAP consumes only `str(path)` and `to_commands()`, so new defaulted fields are safe. Sensitive tests: exact step/scan counts in `tests/core/analysis/test_prove_walk.py:85,88,156,159` and `test_prove_walk_feedback.py:101,135,176` (all single-goal — protected by empty-store bit-identity), and `test_prove_walk_nogood.py:304` (`store.recovery_iters <= 2`).

**Out of scope** (explicitly deferred): `_WalkContext` parameter bundling, plan-as-tree, monitor unification.

---

## Phase A — Bug fixes (independent, commit separately)

**A1. Thread `nogoods` into the missed `_check_residuals` call.**
walk.py:2422-2438 (end of the serial-prerequisite path in `_walk_to_goal`): add `nogoods=nogoods`. The other three call sites (2227, 2293, 2466) already pass it; this is the clobber-prone path where losing learned nogoods hurts most.

**A2. Apply `unlink` to the verify and annotate forks.**
walk.py:2685-2689 installs `Harness(verify)` without unlinking; walk.py:2704-2729's annotate fork installs no harness at all. Add a small helper in walk.py:

```python
def _install_replay_harness(plc: PLC, unlink: list[str] | None) -> None:
    """Mirror the work fork's physical model on a replay fork."""
    from pyrung.core.harness import Harness
    h = Harness(plc)
    h.install()
    if unlink:
        h.unlink(unlink)
```

Use it for both forks (gated on `work._harness is not None`, as today). Harness API: `install()` at harness.py:145, `unlink()` at harness.py:189.

**A3. Fix the dead nogood-hint transition.**
walk.py:2394-2401 passes `transition=(gov_value, gov_value)` and walk.py:2530-2536 passes `(target_value, target_value)`, but nogoods are keyed `(current_from_value, target_value)` (walk.py:1914,1936) — `all_orderings_blocked` can never match. Two changes:
- Pass the real from-value: `transition=(work.state.tags.get(governing), gov_value)` / `(work.state.tags.get(target_tag), target_value)`.
- Relax `NoGoodStore.all_orderings_blocked` (walk.py:157-168) to match **any** recorded nogood with the same `(from_value, to_value)` regardless of blocking set (iterate `self._nogoods`). It feeds an observability hint only; exact-set matching is uselessly strict because the hint's `prereqs` come from the static SP-tree while nogood keys come from `cause()`. Keep `is_blocked` exact (it gates recovery bail-out). Update both docstrings.

**A4. Dedup minor code.**
Hoist the `_OPS` comparison dict (duplicated at walk.py:527-532 and 591-596) to one module-level `_CMP_OPS` constant. Collapse the identical `isinstance(operand, str)` if/elif branches at walk.py:583-585 into one.

**Gate:** `make test-prove` green after each fix (no count changes expected — A1/A3 affect only paths that previously failed or logged; A2 affects only harness-coupled programs, where existing feedback tests at test_prove_walk_feedback.py must stay green).

---

## Phase B — HoldStore + registration (inert: nothing consumes holds yet)

**B1. `_Hold` / `HoldStore`** in walk.py next to `NoGoodStore` (~line 170):

```python
@dataclass(frozen=True)
class _Hold:
    name: str          # external input tag
    value: Any         # value that must persist
    goal: tuple[str, Any]  # the (tag, value) goal this hold protects

class HoldStore:
    # dict[str, _Hold]; methods:
    # protect(name, value, goal)   — register; same-name same-value updates goal; conflicting
    #                                value for an existing hold: keep the existing hold, log a
    #                                cross-goal conflict (do not overwrite)
    # release(name)                — divest
    # protected() -> dict[str, Any]        — name -> value
    # protected_names() -> frozenset[str]
    # __iter__ / __len__           — for output assembly
```

Docstring states the safety contract (mirrors NoGoodStore's): holds only *restrict releases* and are re-validated by `plan_walk`'s replay verification; an empty store is bit-identical to today.

**B2. Factor `_extract_holds`.**
Lift the mining loop from `_try_independent_walks` (walk.py:2092-2098) into

```python
def _extract_holds(actions: list[_Action], cone: frozenset[str], ext_set: set[str]) -> dict[str, Any] | None
```

(last-writer-wins per input; return `None` on intra-call value conflict, preserving the current bail semantics). Replace the inline loop in `_try_independent_walks` with calls to it.

**B3. Registration at commit points.**
Rename the body of `_walk_to_goal` to `_walk_to_goal_inner`; make `_walk_to_goal` a thin wrapper that, on a non-`None` result with the goal satisfied on `work`, mines holds from the returned actions (`cone = pdg.upstream_slice(target_tag)`, `ext_set = set(ext_inputs) | edge_ext`) and registers them protecting `(target_tag, target_value)`. Recursion through the wrapper means every committed sub-goal registers its own holds — that's the point. Also register `required_holds` in `_try_independent_walks` on success (protecting the governing goal).

**B4. Threading.**
`holds: HoldStore | None = None` keyword-only (None→fresh empty, same pattern as `nogoods`) through: `_walk_to_goal`, `_explore`, `_apply_steer`, `_apply_steer_compound`, `_recover_via_oracle`, `_check_residuals`, `_try_independent_walks`, `_blocker_clearing_move`. `plan_walk` constructs one store per walk alongside `nogoods` (walk.py:2616). `_steer_prefix` gains `protected: frozenset[str] = frozenset()` (names only — it doesn't need the store). `_probe_steps` stays hold-blind (local governance detection, never commits).

**Gate:** `make test-prove` — must be green and behaviorally identical (consumption not wired; registration is bookkeeping only).

---

## Phase C — Selective release + divest check (the behavior change)

**C1. Selective release in `_steer_prefix`** (walk.py:1437-1487). In the pulse branch:
- `release = {c: False for c in ext_inputs if work_tags.get(c) and c not in protected}`
- the edge_ext release loop and the `pulse[e] = True` edge-blast loop both skip protected names (a hold at False must not be driven True). The steered input `inp` itself is exempt from the skip — conflicts on `inp` are resolved in `_explore` before the prefix runs (C2).
- The multi branch: skip protected names the same way when building its release dict.

**C2. Divest check in `_explore`'s steer loop** (walk.py:1621-1674). Add a helper `_steer_conflicts(steer, holds, edge_ext) -> frozenset[str]`: protected names this steer would write to a different value (pulse → `inp` if protected at False; low → `inp` if protected at True; set/multi → patch entries differing from held values). Before `_apply_steer`:
- No conflicts → proceed (prefix gets `protected = effective protected names`).
- Conflicts → **divest probe**: fork `node.plc`, apply the steer prefix unfiltered for the conflicting names, settle `_PULSE_REACT_CAP` scans, check each conflicting hold's `goal` is still satisfied (`_values_match`). All satisfied → the hold is releasable (seal-in case): proceed with the steer, and record the names in the child `_Node`'s released-overlay. Any broken → `continue` (skip the steer).

**Per-branch overlay, commit-time reconciliation.** `_explore` trials are speculative, so do **not** mutate the store mid-explore. Add `released: frozenset[str] = frozenset()` to `_Node`; effective protected for a node = `holds.protected_names() - node.released`; a successful divest grows the child's `released`. After `_explore` returns a winning path and `_walk_to_goal` commits it via `_advance_work`, reconcile: any action in the committed steps that writes a protected name to a different value → `holds.release(name)` + `logger.info("walk: divest point — released %s (was protecting %s)")`. Cost bound: one extra fork + ≤`_PULSE_REACT_CAP`+2 scans per conflicting steer per node, bounded by `_MAX_NODES × |alphabet|`.

**C3. Validation against the tripwires.** Expected outcomes, to confirm by running:
- Single-goal tests (test_prove_walk.py, test_prove_walk_feedback.py exact counts): no holds ever register → bit-identical.
- Nogood tripwire (test_prove_walk_nogood.py:304): its clobbers stem from **failed** sub-walks (no hold registered → release still happens → recovery still exercised); `recovery_iters <= 2` should still hold. If prevention now solves it with 0 recovery iters, keep `assert path.reachable`, relax the telemetry assertion, and add a direct `NoGoodStore`/`_explore`-level unit test so the recovery loop stays covered.
- Decomposition/rendezvous tests: serial walking may now succeed where independent forks were required (prevention preserves the earlier hold) — these assert reachability, not step counts, so they should pass either way; note in the walk-plan doc if the solve route changed.

**Gate:** `make test-prove` green (726 pass, 4 xfail baseline); inspect walker log lines for divest/prevention behavior on the tripwires.

---

## Phase D — Holds in plan output

**D1. graph.py** (`Path` at graph.py:430): add `holds: tuple[tuple[str, Any, str], ...] | None = None` — entries `(input, value, protecting_goal_tag)`. In `Path.__str__` (graph.py:438-457), append after the step lines, only when non-empty:

```
  Holds: Enable_A=True (for InitA), Enable_B=True (for InitB)
```

Per-step annotation: **not** added (keep minimal; `constraints` already carries per-step semantics). `to_commands()` unchanged — forces already persist, so holds need no extra commands. DAP picks the section up automatically via `str(path)`.

**D2. walk.py `plan_walk`**: after verification succeeds, build `holds_out = tuple(sorted((h.name, h.value, h.goal[0]) for h in hold_store))` and pass `holds=holds_out or None` to the final `Path(...)` (walk.py:2740). Already-satisfied early return (walk.py:2599) passes nothing.

---

## New tests — `tests/core/analysis/test_prove_walk_holds.py`

Follow the program-builder style of test_prove_walk_decomposition.py.

1. **Prevention**: two prerequisites where prereq A's enable must stay held while walking prereq B serially (the pattern `_try_independent_walks` exists for). Assert `path.reachable`, `store`-level telemetry shows `recovery_iters == 0` (drive `plan_walk` directly to inspect, as test_prove_walk_nogood.py does), and `path.holds` names A's enable.
2. **Divest point**: input seals a latch (seal-in via OR feedback), hold registered; a later corridor requires that input low. Assert reachable; assert the divest (hold absent from `path.holds`, or caplog contains the divest log line).
3. **Conflict skip (no false positives)**: non-latching goal genuinely requires its input held; a steer that would break it must be skipped. Assert the unreachable/alternate-route outcome is honest (no plan that fails replay — replay verification is the backstop, assert `path is None` or reachable-with-hold-intact).
4. **Rendering**: `Path.__str__` includes the `Holds:` section when populated and omits it when `holds is None` (pure graph.py unit test).

For Phase A, extend `test_prove_walk_feedback.py` with a `unlink=` case where the enable rises during the plan so a live coupling would schedule a conflicting feedback patch during verification — asserts the A2 fix (plan still verifies). If constructing the diverging case proves fiddly, unit-test `_install_replay_harness` directly (couplings exclude unlinked names).

## Verification

- After each phase: `make test-prove` (suite gate; baseline 726 pass / 4 xfail).
- After Phase C: re-run the tripwires with `-o log_cli=true` (or caplog asserts) to confirm prevention vs. recovery routes; update `scratchpad/corridor_walker_plan.md` status block (serial-clobber prevention landed; recovery retained as backstop).
- Final: `make lint` + `make test`.

## Risks & rollback

- Each phase is an independent commit; Phase C is the only behavior change. If tripwires destabilize, A+B+D still land (B is inert bookkeeping, D renders whatever registered).
- Over-holding risk (a registered hold blocks a release some corridor needs): mitigated by the divest probe; worst case the walker returns `None` where it used to clobber-then-recover — if a tripwire regresses this way, the conflict-skip can fall back to "allow the steer and let recovery handle it" (one-line change at the C2 skip).
- `_walk_to_goal` wrapper rename must preserve the recursive call sites (`_recover_via_oracle`, `_try_independent_walks` call `_walk_to_goal` — they should call the wrapper so sub-goals register holds too).
