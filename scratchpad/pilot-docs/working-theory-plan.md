# Stage 6B — Scan-level intrascan control

This is the implementation contract for Stage 6B. It replaces the old
“discover a repair, prove it elsewhere, then replay the repaired transaction”
model with one lazy scan-by-scan control loop.

The governing question is:

> What would the technician do if they could press `step()` and inspect the
> exact scan that just ran?

They would keep the useful edge, read what that scan exposed, make only the
newly justified intervention, and step again. They would not first prove a
whole future and then repeat the work on the live runner.

## Before → after

### Before

1. Compass selected a steer.
2. The steer ran through a broad pulse/settle window.
3. A later regression monitor discovered that a selected effect had been
   overwritten or displaced.
4. Requirement recovery restored an old source, executed private local repair
   loops, sometimes folded a program continuation, and proved a corrected
   future.
5. The outer loop replayed or adopted a version of work that had already been
   executed for proof.

Consequences:

- the system could do the same work twice;
- “settled” state could hide the exact scan where conductivity changed;
- one requirement executor and the ordinary Compass loop could both own the
  next action;
- successive requirements were accumulated around a replay root instead of
  being learned at the current scan edge;
- OR branches tended to become eagerly materialized recovery plans;
- exact writer/call-site identity was difficult to preserve across folded
  continuations.

### After

1. Compass reads the current world and chooses one ordinary executable
   `Bearing`, `NeedProbe`, or `Stuck`.
2. The bearing executes once.
3. Intrascan reads that same execution’s owner-bound projection. If it exposes
   an exact temporal requirement, WorkingTheory records the fact and its
   executable source.
4. A fresh Compass read asks what is executable now, with the theory facts
   supplied as constraints.
5. Compass lowers only the next exact temporal assignment or composes it with
   the fresh ordinary pulse when they share the transaction.
6. That scan executes once and is either adopted directly or discarded.
7. If it exposes another requirement, the theory refines and Compass reads
   again from the retained source or productive tip.
8. If its scan-progress receipt says the retained landing still owns the
   productive tip, that landing is the next working edge. Otherwise the same
   receipt preserves the productive S1 evidence while ordinary regression
   handling restores the causal source. No proof replay occurs.

The loop is therefore:

```text
read current edge
→ execute one bearing
→ inspect that exact scan
→ keep an exact productive tip OR restore the exact source
→ add only newly learned facts
→ read again
```

## The scan window

For one bearing:

- `S0` is the exact executable source before the bearing.
- `S1` is the assertion/productive scan.
- `S2` is an optional single look-ahead scan already owned by that ordinary
  execution.

`S1` and `S2` are observations, not a promise to settle. The system keeps both
only when their exact receipts matter.

A temporal setup or retry uses `ASSERTION_SCAN`: it executes the chosen phase
through `S1` and yields. Ordinary readers may choose a one-scan look-ahead when
that is how current program motion is observed. No temporal path asks for a
fixpoint or a broad `settle=True` behavior.

The ordinary terminal coast and departure machinery still has bounded settle
operations. Those are not evidence that a temporal retry may settle past its
scan edge.

## What counts as progress

Stage 6B uses one general receipt:

> This exact scan advanced the selected producer or target-relative frontier.

`ScanProgressReceipt` binds:

- source scan and source world;
- productive scan and retained landing scan;
- the selected physical act;
- progress kind (`target`, `selected-producer`, `frontier`, `earned-work`, or
  explicitly declared local conductivity);
- before/after target-relative coordinates.
- whether the retained landing still owns the productive tip.

The receipt is earned only from executed evidence. Examples:

- the selected producer appeared and its required consumer-relative shape
  survived at the tip;
- the target itself held at the retained scan;
- an event-earned ordinal advanced;
- an exact outstanding target frontier became true;
- a declared temporal phase reached its local boundary and passed ordinary
  verification.

Mere state change, elapsed scans, a theory being active, a new unrelated trace
condition, or a later settled landing is not progress.

Productivity and landing ownership are deliberately separate. A selected
producer may survive on `S1` and then be displaced on `S2`. That earns exact
evidence for the working theory, but it does not authorize adopting the
regressive `S2` landing.

### The bounded compatibility exemption

Legacy outer trend monitoring remains temporarily for ordinary accepted
landings that do not yet carry sufficient scan-level evidence.

It is bypassed only when the exact receipt proves that the selected producer
or exact source-tree frontier owns the retained tip. An active WorkingTheory by
itself does **not** bypass monitoring.

This is intentionally the landing seam:

- productive exact tips are reread directly;
- regressive or merely accepted tips still reach the existing monitor;
- a pre-theory displaced producer still reaches investigation so its first
  requirement can be learned;
- once every accepted path has an adequate scan receipt, the monitor exemption
  can replace the remaining legacy monitor path rather than coexist with it.

The cleaner follow-up is to make the receipt sufficient for every accepted
scan and then delete the compatibility monitor decision, not broaden the
exemption.

## Where the next requirement lives

The requirement remains a WorkingTheory fact. It records:

- its exact condition and Boolean structure;
- demanding occurrence and deadline occurrence;
- selected writer and owner/call-site identity;
- operand authority;
- source scan, source world, and retained checkpoint owner;
- lifecycle status and provenance.

It does not contain a future action.

Compass receives a detached `TheoryView` plus the live requirements resolved
for the theory’s next temporal request. Compass and its readers decide the
fresh bearing. This keeps the ownership split clean:

- intrascan reports what the executed scan needed;
- WorkingTheory remembers facts, attempts, versions, sources, and progress;
- Compass decides what is executable in the restored/current world;
- steer executes exactly one chosen bearing;
- verification decides whether that exact execution may be adopted.

Requirements may be discovered on the original scan, on `S2`, or on a later
scan reached from a productive tip. The scan number is diagnostic; only the
exact retained executable source and owner identity control the retry.

## Same-scan and prior-scan lowering

### `RETRY_TOGETHER`

When the missing condition belongs at the selected consumer in the assertion
transaction:

1. restore the exact source;
2. ask Compass for its ordinary current-world pulse;
3. use the same `CandidateRead` for the ordinary pulse and temporal
   composition—ProgramStep, AdvanceProfile, availability, tide-table, and
   Crossing readers run once for that world;
4. read only same-transaction siblings visible in that fresh read;
5. lazily lower one compatible requirement branch;
6. compose the assignment, ordinary pulse, and required siblings into one
   physical act;
7. execute one assertion scan;
8. adopt that exact fork only if ordinary verification accepts it.

If the attempt exposes a second exact requirement, that rejection is theory
refinement, not theory failure. Restore the same source, add the new fact, and
read again. A same-scan pair of timer hazards therefore becomes:

```text
try Command
→ learn PresetA
try PresetA + Command
→ learn PresetB
try PresetA + PresetB + Command
→ keep the accepted S1 tip
```

### `SETUP_FIRST`

When the condition must exist before the assertion transaction:

1. restore its exact earlier source;
2. lower the smallest admissible setup assignment;
3. execute and verify one assertion scan;
4. retain the accepted setup as the working tip;
5. discharge only the exact requirement established by that phase;
6. clear the immediate temporal request and reread Compass.

The original pulse is never queued. It must be rediscovered from the new tip.

Program-owned guard rebasing follows the same rule: retained history may turn
a later program-written blocker into an earlier adjustable prevention fact,
but the resulting assignment is executed as an ordinary theory bearing. There
is no private repair executor.

## Boolean branching

Branch traversal is depth-first and lazy.

- `AND` is one atomic branch. Every member must be jointly lowerable and
  non-conflicting before execution.
- `OR` yields one branch at a time in stable source order.
- A failed OR branch earns an exact attempt receipt; only then may the next
  branch be yielded.
- A branch is never retained as a future bearing or queue.
- A later fact may refine the theory version, at which point branch identity is
  recomputed from that version’s exact requirements.

DFS matches the technician’s behavior: finish learning whether one concrete
conductive branch works before moving to its sibling. BFS would require
retaining multiple speculative futures, which this model forbids.

## Reader-aware ordering

Temporal control does not make every theoretical assignment executable.
Compass first applies the same readers and admission rules as an ordinary
steer:

1. exact current trace and Crossings;
2. writer availability and tide-table constraints;
3. active avoid, configured-input, force/patch, and blocked-action rules;
4. awaited external handoff evidence;
5. `program_step` / `AdvanceProfile` evidence for owned program motion;
6. learned exact batches and branch alternatives;
7. skiff/probe only when the current frontier remains unreadable.

Temporal lowering consumes those readings. It does not bypass them.

This matters because some nominally satisfying changes are regressive. The
selected branch must preserve all active requirements and must not defeat the
current target guard, availability contract, or exact operation boundary.

## Authority

Direct assignment is permitted only for an adjustable operand whose current
value is still the unset/default value represented by the requirement.

Never assign:

- a forced or patched/configured value;
- a program-written value at the demanding occurrence;
- an unknown-authority operand;
- a non-default preset merely because a different value would be convenient;
- timer `.Done`, accumulator internals, or another derived result to bypass the
  real preset/guard;
- a mixed `AND` containing an authoritative unsatisfied member.

A mixed `OR` may still expose a wholly adjustable alternative. The lazy branch
reader may try that branch without weakening the authoritative sibling.

## WorkingTheory lifecycle

One local theory is active at a time.

- **Open** on exact actionable `SETUP_FIRST` or `RETRY_TOGETHER` evidence.
- **Refine** when a scan exposes a genuinely new exact requirement or a
  program-written blocker is rebased through retained history.
- **Advance** only from an accepted exact scan-progress receipt.
- **Yield** after each accepted phase so Compass rereads the tip.
- **Prove** when the local claim’s requirements are discharged and its exact
  target/producer obligation is satisfied.
- **Abandon** only when the bounded exact experiments are exhausted or the
  claim is falsified; budget exhaustion is not global impossibility.

The ledger retains facts and attempt identities, not PLC forks, routes,
bearings, or action queues.

## Landed 6B boundary

Implemented in this slice:

- typed detached temporal requests;
- exact bootstrap source support, including scan zero before a normal world
  key exists;
- scan-progress receipts and adjacent-tip revisit admission;
- assertion-only temporal setup/retry;
- same-transaction fresh-pulse composition;
- successive same-scan refinement without adoption or replay;
- successive later-scan refinement from productive tips;
- lazy Boolean DFS with atomic AND;
- authority-preserving scalar/guard lowering;
- reader-aware program/awaited-action/AdvanceProfile paths;
- program-guard rebasing through retained history;
- removal of the active requirement repair executor from the drive loop;
- no production `requirement_locally_repaired` event;
- narrow receipt-based exemption from legacy outer monitoring.

Still intentionally deferred to the cleaner follow-up:

- make scan-progress receipts sufficient for every ordinary accepted landing;
- remove the remaining legacy trend/departure monitor decision from this seam;
- delete now-dead local-repair helpers and state fields;
- remove folded repaired-program-continuation helpers after their remaining
  non-borrowing diagnostic use is replaced by scan receipts;
- rename residual “settle” comments that describe ordinary coast behavior but
  could be confused with temporal control.

## Acceptance tests

The required order is:

1. `make test-pilot`
2. `make test-tumbler`

Focused contracts cover:

- timer preset `RETRY_TOGETHER`;
- scan-zero `SETUP_FIRST`;
- same-source successive timer requirements;
- later-scan successive requirements;
- adjustable versus configured/program-written guards;
- lazy adjustable OR versus mandatory mixed AND;
- transient target/zero-net writes;
- off-path and repeated-callsite writer identity;
- exact terminal stall receipts;
- plan replay reaching the same final target.

Completion means the public plan reaches and replays the target without
private repair execution, without proving then replaying, without assigning an
authoritative preset/guard, and without settling past a temporal scan edge.
