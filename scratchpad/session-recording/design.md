# Session recording: design for the release after 0.16.0

Sam ruled on 2026-11-13 that recording stays out of 0.16.0 and becomes the headline of the next release ("it's starting to grow and we want to get it right"). This note holds the design agreed with the planner session. Nothing here is built yet.

## Principles

- Commands are text, and each one carries its source. State is data. A reload with a carry plan is a structured request.
- The command language lives in pyrung. Clients send lines verbatim. They don't split on `;`, and they don't render their own echo.
- Participants send commands: the keyboard, an agent on `pyrung live`, and the Simulator panel.
- Devices don't send commands. These are Modbus writes from a Dataview or an HMI, and harness feedback. Each device change is recorded with its source at the point it reaches the engine.
- Dataview stays on Modbus for reads and writes, so a real PLC's refusals stay in one place.
- One recorder sits under everything. The ScanLog is its clock and its audit, not its source.
- Every accepted command is broadcast to all participants.

## What exists today (checked 2026-11-13, updated after the record machinery went)

- `dispatch()` calls `capture_hook()`, which feeds the capture buffer (`dap/console.py`).
- These structured requests call the runner directly and are recorded nowhere: pyrungForce, Unforce, ClearForces, Patch, SetMode and StepScan.
- Link requests call `capture_hook` directly.
- Modbus writes reach `runner.patch()` through `click/data_provider.py` and are recorded nowhere.
- The capture leaves out pause and continue (`_CAPTURE_EXCLUDED`), so a free run between two actions is lost.
- `record stop` prints the raw transcript and saves nothing.
- `CaptureEntry` keeps the command, scan_id, timestamp and provenance, but not the command's output.
- The transcript keeps queries.
- Every launch starts a LiveServer named after the session. A `pyrung live` command runs the same `dispatch` on the same adapter, so an agent and the panel already share one session.
- DAP `evaluate` calls `dispatch` with no source.
- `;` chaining is split only by two clients: the `pyrung live` CLI (`live.py:343`) and ClickNick's console panel. Each sends the pieces one by one, each under its own hold of the state lock. Neither split knows about quotes.
- STOP->RUN (`set_mode_locked`) resets non-retentive memory and clears forces and patches, then applies the request's forces and patches with no scan in between. A transcript writes it as `mode run`, then the force and patch lines.
- `speed [N|max]` exists. Speed paces wall time against simulated time, so the scan counts between actions are exact at any speed.
- There are two partial records: the capture and the ScanLog.

## Work list, in order

1. **Console commands for every change.** Each structured mutation becomes a thin wrapper that builds its command line and dispatches it with a source. The request shapes stay, so ClickNick 0.16 and the VS Code extension keep working. Check these traps:
   - error messages ClickNick matches on;
   - unforce of a tag that isn't forced is not an error today;
   - `invalidate_mid_scan`.
2. **Source on `evaluate`**, for example `panel` or `vscode`.
3. **`;` on the server.** It splits with a quote-aware tokenizer, runs the whole chain under one hold of the state lock, and records the chain as one entry. Both clients stop splitting. This replaces a separate multi-pair `patch`. Decide what an error halfway through a chain does. My suggestion: check every write before applying any, and keep steps and runs in order.
4. **Literals typed by the tag**, in place of `_parse_literal`'s guess. The renderer quotes text values. Add a round-trip property test (render(parse(x)) == x).
5. **A `pyrungCommand {text, source, scan_id}` event** for each accepted mutation, sent to every participant. ClickNick shows it in place of its own echo.
6. **A SessionRecorder** replaces CaptureBuffer:
   - it is always on, and `record X` / `record stop` mark named ranges inside it;
   - it records a free run as a scan count;
   - Modbus writes come in as `patch` with source `modbus`;
   - `record save <file>` writes the transcript.
7. **The ScanLog audit**, as a test. Every patch and force change in the ScanLog must match a recorder event.
8. **The check and the replay report** (see below). Then `pytest` generation from a transcript, as plain PLC calls the way `spec test` wrote them (commit 5d0569a still has `src/pyrung/dap/spec.py`).

**The reload plan stays structured.** It is recorded as `reload` followed by the carried outcomes.

## First step after 0.16.0

Prototype the replay report headless on a real program before building any UI:
1. record a session;
2. change the program;
3. replay the session;
4. read the report.

## A saved recording

- **Header:** the program file and its hash, the pyrung version, the time mode and dt, and the harness mode.
- **The start:** every recording starts at a power-up of the program. The first lines restore forces, bench links and any retentive value that differs from the program's initial value. Those lines come after `mode run`, because STOP->RUN clears pending patches.
- **The waits:** a gap between actions is a scan count, or a `run until` (see below). Replay runs unpaced.
- **A reload:** it ends the recording, or starts a new segment of it.
- **A replay after the program changed:** it continues past a refused line, such as a renamed tag, and reports it.

## The check (under discussion)

Sam's idea is that the check is explicit. The person asks `get <tag>` at the moments they care about. The recording keeps each `get` with the answer it gave. A replay runs the same lines, and its result is a diff of the answers. There is no `expect` verb and no implicit trace of every output.

Sam added: "typically PLCs sorta go through a sequence. If something doesn't work, it won't get to the tag you wanted to be True." A `get` on a milestone covers everything upstream of it.

How a check works:
- **The wait** before a milestone `get` is `run until <milestone>`. It allows at most the recorded scan count plus a margin, so replay waits on the milestone, not on the clock.
- **On a timeout,** the report runs `why <milestone>` at the scan where the wait gave up, and shows where the sequence stalled. `runner.why()` explains the current state without history.
- **Accepting a change** rewrites the recorded answers in the file. The file sits in git next to the program, so the change can be reviewed.
- **The implicit output trace** is information only. It shows what else changed, never pass or fail.
- **"never" checks** are a later, separate kind: an output that should not energise during a sequence.

## Known weaknesses

- **Replay is open-loop.** Actions fire at recorded scan offsets, not on conditions. After a program change, the report must lead with the first difference, because what follows it is mostly consequence. `run until` waits on milestones fix most of this.
- **Exact matching is brittle.** A one-scan timing shift flags everything. Sampling at chosen milestones avoids most of it.
- **A replay checks no more than the plant model.** Without links or a harness, a simulated machine has no feedback, so a replay checks little. This points back to the plant-pane idea.
- **Watch the wording.** pyrung sees only the Simulator session, not work done on a real CLICK. Say "rehearse commissioning in the Simulator, and replay it after every change."
