# PILOT naming cleanup

A grounded naming backlog, audited against `14f18c9`.

The earlier version treated every proposal as a mechanical rename that could
land in any order. That was too strong. A name is ready only when it describes
the owner's current responsibility without narrowing it, broadening it, or
claiming evidence the object does not carry.

The ownership work in `scratchpad/pilot-docs/PLAN.md` remains the primary plan.
That plan links here as its final naming phase. If an approved rename is pulled
forward, it still lands independently and must not smuggle in structural work.

## Rules

- Use one agent and one conventional commit per listed item.
- Run focused tests while editing, then `make test-pilot` and `make lint` before
  each commit.
- Update imports, tests, module maps, and current documentation in the same
  commit.
- Treat exported fields, event payload keys, and rendered vocabulary as public
  changes: document them in the changelog and test their exact shape.
- Rename a whole concept coherently. Do not leave old and new terms describing
  different layers of the same object flow.
- Re-ground the named owner before implementation. This file records the
  present conclusion, not permission to preserve a stale premise.

## Approved exact renames

These names describe the code as it exists today and do not depend on an
ownership redesign.

| Current | Rename to | Why this is exact |
|---|---|---|
| `navigation.py` | `navigation_types.py` | The module contains immutable navigation contracts, not navigation algorithms. |
| `charts.py` | `pipeline_graph.py` | It builds and searches static transition graphs and detects opaque pipeline structure. |
| `navigation_evidence.py` | `reachability.py` | It owns constrained current-world path admission, continuation, and reachability status. |
| `CurrentReading` | `AwaitedAction` | The value is exactly one operator action a recognized program transition is awaiting. |
| `Bump` / `BumpEvent` | `CoastTrigger` / `CoastTriggerEvent` | They are a predicate armed for one coast and the exact firing it records; the prefix avoids colliding with causal triggers. |
| settle parameter `cone` | `watched_tags` | The set is formed from trace-tree and opaque-loop tags whose motion matters; it is not necessarily a PDG upstream scope. |

Recommended order for the no-design tranche:

1. `navigation.py` -> `navigation_types.py`
2. `charts.py` -> `pipeline_graph.py`
3. `navigation_evidence.py` -> `reachability.py`
4. `CurrentReading` -> `AwaitedAction`
5. `Bump` / `BumpEvent` -> `CoastTrigger` / `CoastTriggerEvent`
6. settle parameter `cone` -> `watched_tags`

Stop after this tranche and re-ground the remaining owners.

## Rename after the owning PLAN item

These concepts are being reshaped by the main plan. Naming them first would
either bless the current mixed responsibility or create another rename later.
The B6 departure-receipt prerequisite is now met; its three rows remain here
for the post-ownership naming audit rather than authorizing a rename in the B6
commit.

| Current proposal | Wait for | Grounded direction |
|---|---|---|
| `options.py` -> `candidate_builder.py` | D1 | Use `candidate_builder.py` if the extracted phases still form one candidate-building owner. |
| `detour.py` -> `departure_classifier.py` | post-B6 re-audit | Prefer the stable owner name `departure.py`; observation and typed classification now share this owner while policy stays in `progress.py`. |
| `gauge.py` and selected `Gauge*` types -> earned-work names | post-B6 re-audit | Decide the complete vocabulary together: module, model, component, reading, receipt, movement, builder, state field, diagnostics, and tests. A partial rename would create two concepts. |
| `_PulseState` -> `_TrialState` | post-B6 re-audit | Rename the whole execution vocabulary together, including `.pulse` fields on attempt/trial receipts. `_TrialState` alone is not a coherent change. |
| `_ops.py` -> `_execution.py` | D4 / E2 | Do not rename the current mixed module. It also owns state/world keys, avoid and hold predicates, rung compilation, and coast wrappers. Name the cohesive owners after replay advancement is extracted. |

## Proposals that need a better name

The motivation is sound, but the proposed replacement currently describes only
part of the module or asserts more than the object proves.

| Current | Do not use yet | Why / candidate direction |
|---|---|---|
| `evidence.py` | `pipeline_roles.py` | The module also expands transition routes and builds `TransitionEvidence`. Revisit `pipeline_structure.py` or `pipeline_analysis.py` after its owner is stable. |
| `tide_tables.py` | `guard_solver.py` | It also models and inverts table reads and calculation preimages. `finite_solver.py` is a closer candidate, but needs a responsibility audit first. |
| `currents.py` | `program_awaits.py` | It also folds program constants and classifies producer families. `program_transitions.py` or `live_transitions.py` is closer; the `AwaitedAction` type rename can land independently. |
| `skiff.py` | `frontier_probe.py` | `run_pinned_scan` is a general isolated probe used by investigation. Rename the module and all `skiff` symbols/events only after choosing between a frontier-specific and isolated-probe owner. |
| `Pulse` / `BatchPulse` | `ActionEvent` / `BatchActionEvent` | They are declared `NavigationAct`s awaiting execution, not observed events. Keep them, or later consider `PulseAct` / `BatchPulseAct`. |
| `ChannelHeading` | `TargetBoundary` | The replacement loses the important channel distinction and collides with `TargetSpec`; the object is a declared channel destination/boundary. |
| `ChannelMotion` | `ObservedCrossing` | The object may exist before a reached/departed result and does not carry the landing value, so “observed crossing” overclaims its evidence. |
| `lever_notes` | `action_hints` | The data are explanatory notes, not necessarily hints, and the name is exported on `Plan` and in DAP payloads. Decide compatibility explicitly; `action_notes` is the plainer candidate. |

## Kept names

`pilot`, `trace`, `compass`, `bearing`, `coast`, `steer`, `verify`, `outcome`,
`progress`, `investigate`, `corrections`, `recording`, `availability`,
`advance`, `program_step`, `physical`, `multitarget`, `cyclefold`, `causal`,
`orientation`, `static_expressions`, `types`, `pen`/`pens`, `nogood`,
`tombstone`, `dwell`, `frontier`, `ActPolicy`, `ActSource`, `CoastReceipt`,
`StaticEdgeAdmission`, `ContinuationSafety`, `BearingObjective`,
`PendingDeparture`, `CandidateRead`, `WaitRead`, and `RouteRead`.
