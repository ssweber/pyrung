# Session capture via the DAP console

The DAP console is already a command-evaluated interface to a live PLC.
Extending it with recording and condensation produces a session format that
doubles as regression tests — no separate session machinery, no per-session
UI, no custom format.

## Core primitive

The console transcript is the session format. Every operator action —
typed at the console, clicked via DAP UI buttons, or sent by an LLM through
`pyrung-live` — emits the same console command. One vocabulary, one capture
mechanism, one replay path (feed the file back into the console).

## Recording verbs

Two new console commands bracket a capture:

```
record ACTION_NAME
...operations...
record stop
```

Everything between is captured as a raw transcript. On `record stop`, two
artifacts are produced from the same capture: a minimal reproducer and a
set of candidate invariants for review.

## The condenser: minimal reproducer

Idle time and operator fumbles collapse to causal minima. A raw capture
like:

```
record start_machine
patch State 1
run 500 ms       # operator watched the machine
patch State 2
step 1
record stop
```

Condenses to:

```
# action: start_machine
patch State 1
run 20 ms
patch State 2
step 1
```

Rules:

- Each `run` duration is replaced with the minimum required to observe all
  relevant transitions (downstream of the session's current observation
  focus, per the PDG).
- Op-then-inverse-op within a short window with no intervening relevant
  transitions is elided — operator fumbles.
- Idle scans between ops collapse to `max(0, deadline - 1)` where deadline
  is the first pending timer expiry or decay.

Default condensation rule: minimum to observe all relevant transitions.
Once a spec exists, the rule shifts to "minimum to satisfy all accepted
temporal bounds" — aligns the reproducer with the spec.

Prefer an implicit state snapshot at `record start` over requiring explicit
`clear_forces` — more forgiving, with a warning if the starting state has
active forces.

## The miner: candidate invariants

The same capture feeds the invariant miner over the recorded scans.
Candidates surface for review:

```
start_machine candidates:
  [?] State↑ → Running↑ within 1 scan         accept / deny / suppress
  [?] Running ⟹ ~Fault                        accept / deny / suppress
  [?] State=2 ⟹ MotorOut=1 within 2 scans     accept / deny / suppress
```

Accept → spec section under the `start_machine` label. Deny → dropped.
Suppress → added to a suppressions list so it doesn't re-propose next
session.

Review fires on `record stop`, not session close. Small reviewable batches
tied to a just-framed action, not a pile of invariants at end of day.

## Reproducer + spec share the action name

`start_machine` is the reproducer file's comment header and the spec
section's label. Same name joins them. Replaying the reproducer against a
refactored program re-runs the ops; the spec section's accepted invariants
get checked live; the diff reports preserved / broken / new / unobserved
per action.

## Live vs sim mode

Same console commands, different backend:

- `mode live` — `run 1h` waits an hour in wall-clock.
- `mode sim` — `run 1h` advances the scan engine until sim-clock ≥ 1h.

Recording captures ops, not mode. Replay defaults to sim regardless of
recording mode. Humans default live (matches physical intuition); LLMs and
CI default sim (speed). Both produce identical recordings.

Mixed sessions are normal: engineer watches the fill phase live
(`run 2 min`), fast-forwards through the mix cycle (`run 1 h`), watches
the drain live (`run 5 min`). Condenser collapses all three uniformly; the
reproducer always runs in sim at causal minima.

## pyrung-live: remote console attachment

Out-of-process clients talk to the live DAP-hosted console via a
session-keyed Unix socket:

```
pyrung-live --session NAME step --n 5
pyrung-live --session NAME patch State 1
pyrung-live --session NAME cause Running
pyrung-live list
```

Each invocation is a stateless client; the DAP process is the stateful
server. Session name from DAP UI is the only coordination — engineer
copies it, hands it to an LLM, LLM connects. Socket path is
`/tmp/pyrung/<session>.sock` (or platform-equivalent named pipe).

Every `pyrung-live` response returns three things in the envelope:

- **Result** — what the tool call returned.
- **Since-last-call delta** — actions since the last call (forces, steps,
  patches), tagged by who: `human` / `llm` / `dap`.
- **Intent notes** — short free-text notes the engineer optionally writes
  to explain what they're doing.

The delta auto-syncs the LLM to whatever the engineer (or another LLM) did
while it wasn't looking. Intent disambiguates ambiguous action sequences.
Both ride over the same socket; both are part of the session transcript.

## CLI over MCP

The agent surface is CLI / Python library, not MCP. Reasons:

- MCP preloads tool schemas (tens of thousands of tokens) regardless of
  whether they're needed. Public benchmarks put MCP tasks at 4–30× the
  token cost of equivalent CLI tasks.
- LLMs are fluent in shell and Python from training; MCP launched late
  2024 and is effectively absent from training data.
- Single-user local process has no OAuth / RBAC / audit-log needs that
  MCP solves.
- CLI composes via pipes in one LLM call; MCP requires a round-trip per
  tool call.

An LLM using the library looks like:

```python
from pyrung import attach
s = attach("startup_exploration_a3f")
s.patch("State", 1)
s.run("20ms")
print(s.mine())
```

Same semantics as the CLI, same socket underneath. An MCP wrapper can be
added later if a client demands it — ~200 lines on top of the library.
For v1, skip it.

## Session file format

Plain text. Console commands, one per line. Comments for action labels and
optional intent notes. No custom grammar, no serialization format.

```
# action: start_machine
# intent: testing the happy-path startup
patch State 1
run 20 ms
patch State 2
step 1

# action: fault_during_run
patch Overtemp true
step 2
```

Readable, diffable, grep-able, hand-editable. Versioned alongside the
ladder program in whatever repo the project uses — no pyrung-specific
version control needed.

Replay has two equivalent paths, both backed by the same console
evaluator:

- **Paste into the DAP console.** The transcript is exactly what the
  console accepts as input, so selecting the file contents and pasting
  them in just works. Useful mid-debug when an engineer wants to
  reproduce a scenario to the scan they were at.
- **Programmatic via `pyrung.live(text)`.** Takes a multiline string,
  queues the commands in order, runs them against the attached session.
  Identical semantics to a paste; useful from Python, from pytest, from
  an LLM driving the library, from anywhere that can produce the string.

Same file, same commands, same result. The only difference is who's
holding the keyboard.

## What needs building from current state

1. **Missing console verbs** — `unforce`, `clear_forces`, whatever isn't
   already wired into the DAP console evaluator.
2. **Capture mode on the console** — flag that buffers every evaluated
   command with scan_id. Passive, no new semantics.
3. **`record` / `record stop` verbs** — the bracket primitive, plus the
   action label written as a `# action:` comment.
4. **The condenser** — takes buffer + PDG, emits minimum reproducer.
   Includes fumble detection and causal-minimum `run` substitution.
5. **Review UI** — on `record stop`, surface mined candidates with
   accept/deny/suppress buttons. Accepted ones write to spec under the
   action label.
6. **`pyrung-live` CLI** — Unix-socket client against the DAP-hosted
   server.
7. **Session envelope** — result + delta + intent notes returned on every
   tool call.
8. **Replay** — `pyrung-live exec session.txt` or equivalent runs the
   reproducer against the current program.

## Deferred

- **Git-backed session storage.** Text transcript + comments is enough for
  v1; version control happens in the ambient project repo.
- **Nested recordings.** Flat is sufficient; hierarchy via prefixed names
  (`start_machine_phase1`, `start_machine_phase2`).
- **MCP wrapper.** Library + CLI first; MCP only if a client demands it.
- **Operator/LLM pairing UX beyond the shared socket.** The socket is
  enough mechanism for now; fancier collaboration UI comes after the
  basics work.

## The shape, summarized

The console is the interface. The transcript is the artifact. The
condenser produces the reproducer. The miner produces the invariants. The
engineer's accept/deny calls build the spec. Replay is running the
transcript back through the console. Everything else is a view on or
operation over these primitives.
