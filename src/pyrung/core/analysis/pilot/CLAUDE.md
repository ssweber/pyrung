# `pilot/` — steer a PLC fork toward a target

PILOT answers `PLC.how(...)`: from the PLC's current state, what safe,
evidence-backed input changes, waits, and temporary guarded holds can make the
requested condition true?

PILOT is an online controller, not a stored-plan executor. It repeatedly reads
the current world, chooses one immediate action, tries it on a disposable fork,
and learns from the result. Static analysis proposes; executed PLC scans are the
oracle.

Use WWTD — *what would the tech do?* — as the first-principles check: read the
ladder and trend first, make the smallest reversible intervention, then observe
what the program actually did.

Module docstrings are the source of truth for local responsibilities. This file
only records the cross-cutting contracts that are difficult to recover from one
module.

## Public contract

- PILOT never drives the caller's PLC. It works on forks.
- `PlanStatus.REACHED` carries the reached fork. Its scan log and synthesis
  holds are the replayable witness; replay does not re-plan.
- `PlanStatus.CANNOT_REACH` requires a proof of incompatibility or
  unreachability.
- `PlanStatus.STOPPED` means PILOT exhausted its bounded supply of safe,
  evidence-backed actions. It is not a proof that no path exists.
- `Plan.journal`, `journey`, and the event stream explain the drive. They do not
  determine the verdict.
- A multi-target `how(A, B, ...)` succeeds only when all targets hold in one
  final committed state. The targets are driven sequentially on one fork and
  then checked together.
- `pilot_events()` is currently a single-target diagnostic stream;
  `pilot_how()` also owns the multi-target composition policy.
- `avoid=` constrains route selection, applied actions, transient execution
  states, and retained landing states. `unlink=` deliberately makes named
  harness feedback steerable for fault injection.

The public `how()` docstring calls the result a minimum input-change sequence.
Internally, PILOT uses deterministic preference and bounded search rather than
an evident global shortest-path proof. Do not strengthen or rely on global
optimality without making that contract and proof explicit.

## Authority chain

```text
current World + durable Knowledge
              |
              v
 trace/read candidates --proposal--> Orientation --one result--> Bearing
                                                               or typed request
              ^                                                   |
              |                                                   v
      apply observations <--- verify <--- ExecutionReceipt <--- forked execution
                                    |              |
                                    | accepted     | rejected
                                    v              v
                            atomic World adopt   discard fork
                                    |
                                    v
                         progress / recovery policy
                         | retain       | restore
                         v              v
                    fresh read     checkpoint World
```

There are three separate decisions:

1. **What can be tried next?** Orientation reads the current world and returns
   one `Bearing`, a typed evidence/research request, or `Stuck`.
2. **Did the attempt produce trustworthy local evidence?** Execution is frozen
   into an `ExecutionReceipt`; verification applies the ordered acceptance
   gates.
3. **Did the accepted result become durable progress?** Post-commit progress
   policy may retain it, hold a departure pending, investigate it, or restore a
   checkpoint.

Do not merge these judgments. In particular, verification acceptance is
eligibility for atomic adoption, not a promise that the adopted world will be
retained.

## Fresh reads, never executable suffixes

`Compass.orient()` is an observational current-world read. Candidate building
creates one immutable `CandidateRead`/`OrientationRead` for that world, and
ordinary and WorkingTheory lowering consume that same read.

A `Bearing` authorizes exactly one immediate act. Its world key, objective,
policy, prerequisites, and effect expectation belong to that read. Execution
must reject a stale Bearing. After any observation, execution, correction, or
restore, return to Compass and read again.

Never retain a route suffix, candidate cursor, future Bearing, executable
callable, fork, or World as recovery/theory state. Reporting provenance may
describe an earlier route, but it must not constrain a later read.

Static uncertainty should usually leave a frontier or request evidence. It
must not manufacture action authority. Conversely, an action proposed by
static or learned evidence still needs physical execution and the same
verification gates.

Trace is the common happy-path instrument, not the fallback owner. One
`CandidateRead` collects the applicable cheap current-world readings; ordinary
act proposers are considered in one declared precedence and every nominated
act crosses the same activation, requirement, and nogood gate. WorkingTheory
may instead declare an exact causal question, and fork-consuming instruments
remain typed, bounded requests.

An actionless read authorizes program motion only through a positive
`ContinuationRead`, currently an exact prerequisite, self-advancing frontier,
ready writer, or satisfied trace awaiting its writer. Absence of a diagnosis
does not authorize Coast. The remaining terminal Coast/Dwell execution is a
legacy lowering of that positive receipt; the intended endpoint is for each
continuation to use its named program-owned execution path, while a genuinely
unresolved read returns an evidence request or `Stuck` for an external
guidance layer.

## World, knowledge, and rollback

`_PilotState` deliberately separates two kinds of state.

**`_World` is revertible executable truth:**

- the current PLC fork;
- committed acts and replay steps;
- active `PilotRung` overlay;
- trend and productive-dwell accounting.

A checkpoint restores these together. Never restore only the PLC while
leaving its operation journal or overlay behind.

**Knowledge survives rollback:**

- Compass observations, transitions, nogoods, probes, and coast receipts;
- active requirements and expectation/effect receipts;
- WorkingTheory lifecycle facts;
- earned-work and departure evidence;
- the append-only `journey` and other diagnostics.

This is why an attempted world can be discarded while the evidence learned
from it remains. `Compass.apply(...)` is the navigation-knowledge mutation
boundary. WorkingTheory facts enter through their reducer/recording boundary.
Readers and renderers do not mutate either store.

## Physical time and receipt identity

Scan numbers alone are not historical identity. Every executed span belongs to
the immutable `Epoch` that ran it, and rollback sources belong to exact
checkpoint owners. Use `EpochRef`, `CheckpointRef`, exact boundaries, and
receipts rather than reconstructing ownership from integers or current state.
Ambiguous or missing ownership fails closed.

`ExecutionReceipt` is the physical truth for one steer/run/observe cycle. It
owns the before/after snapshots, epoch-owned kernel spans, configurations,
coast timeline, stop boundary, effect observations, and verified progress
evidence. Later stages should carry or query this receipt instead of copying
parallel fields.

Pulse execution may distinguish the source scan, productive assertion scan,
and a retained look-ahead or exact consumer boundary. A productive scan does
not imply that the landing still owns its progress; `ScanProgressReceipt`
records that distinction. Logical coast folding is an execution optimization,
not permission to invent physical occurrences.

Search scans and productive program dwell are separate budgets. Accepted
productive dwell is credited; sterile search remains bounded.

## Attempt transaction

`attempt_transition.transition_once()` coordinates one Bearing and does not
own repetition:

1. preserve the source checkpoint and current-read evidence;
2. `steer.execute()` validates freshness and runs exactly one act on a fork;
3. freeze the physical result as an `ExecutionReceipt`;
4. `verify.verify_gates()` judges the exact applied artifact and receipt;
5. record accepted or rejected evidence once;
6. `trial_commit.adopt_trial()` atomically adopts an accepted fork and its
   replay steps.

Verification consumes the complete physical `ActPolicy.applied` artifact.
Requested action pairs remain policy/nogood identity, but they are not a
substitute for what actually executed.

Rejections are narrowly scoped. A failed joint act does not reject its members
individually, and the same act may remain admissible in another world. Proof
rejections and empirical nogoods remain distinct evidence.

## Progress, departure, and recovery

`progress.py` owns post-commit retention. A locally valid action may expose a
later channel departure, erase previously earned target work, or land beyond
the productive occurrence. Progress policy may therefore:

- bank a qualified checkpoint;
- continue observing a pending departure;
- investigate exact causal evidence;
- retain new requirements or correction evidence;
- restore the exact source World.

A pending departure that expires without proof is rolled back without
manufacturing a nogood. Investigation and counterfactual replay produce
evidence only; their disposable forks are never adopted directly.

`journey` includes later-reverted attempts. `_World.committed_acts` and the
successful fork contain only the clean replay path.

## Temporal repair and WorkingTheory

An observed failed effect can become an inert, exact requirement. WorkingTheory
retains the detached causal and lifecycle facts needed to answer what the
current tip requires next. It survives rollback but owns no executable future.

The lifecycle is:

```text
accepted execution evidence
        -> failed effect / requirement
        -> exact source checkpoint and expectation receipts
        -> WorkingTheory facts reduced into the ledger
        -> fresh current-world CandidateRead
        -> at most one composed correction or setup act
        -> execute, verify, record progress
        -> fresh read again
```

Detached requirements must be resolved to a unique current active requirement
before execution. Source/version/receipt ownership is revalidated at mutation
and execution boundaries. That defensive repetition is intentional: these are
different trust boundaries, not one manager waiting to be extracted.

Temporal Boolean requirements are lazy: a top-level `AND` is one obligation
set, while `OR` alternatives are explored one branch at a time. Only
authority-approved adjustable operands become assignments. Intrascan research
and counterfactual patches remain analysis-only.

## Nonlocal invariants

- Production execution forks must include the active `PilotRung` overlay.
- One physical scan cannot belong to multiple committed executions.
- A target is accepted only at a receipted replay tip; never annex an
  unreceipted suffix merely because the target appears there.
- Avoidance is enforced before execution, across observable execution, and at
  the retained landing.
- Endpoint coincidence is insufficient when selected effect obligations or
  active requirements are unsatisfied.
- Already-earned target work cannot be erased merely to obtain a transient
  target appearance.
- Writer availability ranks candidates; it is not proof for rejecting one.
- Learned transitions and static routes are suggestions, never substitutes for
  live verification.
- Occurrence identity belongs to the execution Epoch. Do not reinterpret an
  old scan beneath a newer overlay.
- Checkpoint, Epoch, expectation, requirement, and speculative-world identities
  are deliberately distinct. Consolidate only identical projections, not the
  concepts.
- Recovery may retain facts and restore a World; it may not retain executable
  navigation state.
- Recording and event rendering have no decision authority.

## Where to start

- Public behavior and result assembly: `api.py`, then `graph.py::Plan`.
- Main loop: `pilot.py::_pilot_loop_events`.
- Current-world reading: `orientation.py`, `options.py`,
  `navigation_contracts.py`.
- One attempted transition: `attempt_transition.py`, `steer.py`, `verify.py`,
  `trial_commit.py`.
- Physical evidence and rollback: `execution.py`, `world.py`, `types.py`.
- Post-commit judgment: `progress.py` and the recovery/investigation modules.
- Temporal repair: `working_theory.py`, `theory_reducer.py`,
  `theory_recording.py`, `theory_drive.py`, `theory_orientation.py`.

Read the relevant module docstrings after choosing a path. Do not recreate a
file-by-file ownership index here.

## Vocabulary

- **Bearing** — one next direction bound to a current-world read.
- **Compass** — immutable navigation facade over static references and durable
  navigation knowledge; Orientation makes the selection.
- **coast** — hold declared inputs while program scans pass toward a boundary.
- **dwell** — allow program-owned motion without declaring an input action.
- **frontier** — unresolved non-steerable needs in the selected trace.
- **world key** — executable-state and active-overlay identity used to scope
  observations and nogoods.
- **PilotRung** — temporary guarded steering logic, not a user ladder rung.
- **earned work** — conservative target-relative evidence of completed work.
- **requirement** — inert evidence describing an exact unmet effect; not an
  executable assignment.
- **WorkingTheory** — rollback-stable causal/lifecycle facts used during fresh
  orientation; never a stored plan.
- **consumer boundary** — the exact dynamic read occurrence that consumed a
  produced value.
- **checkpoint-local repair** — restore an exact source, compose at most one
  correction, and return to fresh orientation.

Avoid extending the nautical metaphor in technical contracts. Prefer ordinary
architecture terms when they name the concept more directly.

## Changing PILOT

Prefer changes that make authority easier to see:

- pass an existing typed owner or receipt instead of parallel copied fields;
- derive fields from that owner at the consuming boundary;
- name a repeated exact identity projection, but keep distinct trust-boundary
  validation;
- keep selection, execution, verification, adoption, and retention separate.

Do not introduce a broad manager/facade merely to hide lifecycle complexity.
When a decision moves, update the affected module docstrings and this document
only if the cross-cutting contract changed.

Run:

```text
make lint
make test-pilot
make test-tumbler
```

`make test-pilot` is the bounded core suite. Run it before the generated-program
Tumbler gate. For long or stalled generated drives, use the process-isolated
watch tools under `devtools/`; their own help and module documentation define
the current diagnostics and budgets.
