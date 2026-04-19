# pyrung testing stack

Three artifacts describe one layered system. This doc is the map.

## Thesis

The engineer declares the plant's physics once. A stack of consumers
reuses that declaration. Session capture records and replays the
result.

## The three docs

**physical-realism.md** — the declaration.

- Tag-level: `min=`, `max=`, `unit=`, `profile=` (analog Fb override).
- Kind-level: `on_delay`, `off_delay` (bool Fb only), `system`,
  `profile` (analog Fb default) in project TOML.
- UDT-level: `link=` coupling between `En*` and `Fb*`.
- Bool kinds and analog kinds are self-documenting: timing properties
  mean bool Fb, profile property means analog Fb.
- Fault taxonomy (value / timing / budget) is grounded here; other
  docs reference it.

**autoharness-and-chaos.md** — two consumers of the declaration.

- Nominal harness dispatches on Fb type:
  - Bool Fb: auto-closes every declared `En → Fb*` pair by enqueueing
    patches into the DAP's pre-scan patch queue (same queue as humans
    and LLMs, tagged `harness:nominal`). The scheduler reads the PLC's
    `dt` and converts declared delays to tick counts (`on_delay=20ms`
    at `dt=0.010` → schedule at `now + 2`).
  - Analog Fb: calls a registered profile function every tick with
    `(cur, en, dt)`. Profile named by `profile=` on the tag or kind
    default. Engineer writes rate-per-second math; `dt` makes it
    stable across scan rates. Tagged `harness:analog:<profile_name>`.
- Chaos: user-authored adapters mutate bool timing or wrap/replace
  analog profiles, tagged `harness:chaos:<name>`.

**session-capture.md** — the recording / replay / distillation layer.

- Console transcript is the session format. `record NAME` / `record
  stop` bracket a capture.
- Condenser: raw transcript → minimum reproducer.
- Miner: same transcript → candidate invariants for review. Invariants
  tagged with the `dt` they were observed under.
- `pyrung-live`: out-of-process clients over a session-keyed socket.
  Envelope returns result + delta + intent on every call.
- Replay: as-recorded (deterministic, all patches re-execute) or
  live-harness (skip `harness:*` patches, regenerate from current
  model).

## PLC construction knobs

Two parameters at PLC construction control time:

- **`dt`** — scan granularity. `dt=0.010` is the default. `dt=0.001`
  for chaos tests that need sub-10ms resolution. `dt=0.100` for coarse
  long-duration exploration.
- **`realtime=True`** — wall-clock pacing. Default for live engineer
  sessions (physical intuition matches sim clock). Off for CI, replay,
  and LLM-driven sessions.

Program-internal waits (TON presets, soak timers) iterate their full
counts regardless of `dt`. Skipping a program wait is done explicitly
by patching the timer's `.ACC` — a recorded op like any other,
optionally sugared as `skip <timer>`.

## The core claim

One `physical=KIND` + `link=` annotation yields seven jobs:

1. Static validator (range + coupling checks).
2. Miner floor (no invariant proposed tighter than declared physics).
3. Fuzzer (respects floors when generating sequences).
4. Condenser (reproducer `run` durations honor floors).
5. Nominal autoharness — bool (feedback closes itself in tests).
6. Nominal autoharness — analog (profile function drives response).
7. Chaos surface (same declaration is the perturbation target).

An annotation the engineer would write anyway.

## Build order

Each layer unblocks the next. Skipping one breaks everything above it.

### 1. Realism model

- Tag fields: `min`, `max`, `unit`, `physical`, `profile` on the
  dataclass.
- CSV flag parser: `[min=..,max=..,unit=..]`, `[physical=..]`,
  `[profile=..]`.
- Project-root `pyrung_physics.toml` loader with `T#` literal parsing.
  Bool kinds → `{on_delay, off_delay, system}`. Analog kinds →
  `{profile, system}`.
- UDT `link=` resolution at class-body time.
- Findings: `CORE_RANGE_VIOLATION`, `CORE_ANTITOGGLE` (bool Fb only),
  coupling check.
- Validation: `profile=` without `link=` rejects; bool Fb with
  `profile=` rejects; analog Fb + link with no resolvable profile →
  `CORE_MISSING_PROFILE`.

### 2. Autoharness

- Scheduler primitive: pre-scan heap of `(sim_tick,
  patch_to_enqueue)`, drained before each `plc.scan()`. Reads `dt`
  from PLC construction; 1-tick floor on scheduled delays.
- Autoharness installer: walks UDT instances, hooks rising/falling on
  every `En` with linked `Fb*`. Dispatches on Fb type: bool →
  scheduler; analog → profile call.
- Profile registry: `@pyrung.analog.profile(name)` decorator, keyed
  by string. Profile function signature: `(cur, en, dt)`.
- Patch provenance: `human` / `llm` / `dap` / `harness:nominal` /
  `harness:analog:<profile>` on every patch landing in the transcript.

### 3. Session capture + miner

- Capture buffer with scan_id + provenance on every evaluated command.
- `record` / `record stop` verbs, `# action:` comment emission.
- Condenser with causal-minimum `run` substitution + fumble detection.
- Miner candidate proposer against the recorded buffer, physics floors
  applied, invariants tagged with observation `dt`.
- Review UI: accept / deny / suppress / fix-the-program buckets.
- `pyrung-live` client + session envelope (result + delta + intent).
- Replay: as-recorded vs live-harness modes.

### 4. Chaos

- Adapter registry + `@pyrung.chaos.adapter(name)` decorator.
- `chaos <name> <target>` console verb (pytest flags translate to
  this).
- `harness:chaos:<name>` provenance on adapter-issued patches.
- For analog Fb: chaos adapters wrap or replace the active profile
  function.
- Finding reporter: cites adapter + target + causal path by reading
  the tag off the patch line.

## Deferred, across the stack

MCP wrapper, git-backed sessions, nested recordings, adapter library,
profile library, learned fault profiles, shrinking, unit algebra,
budget arithmetic, IODD/EDDL/OPC UA import, hardware kind inference,
operator fault profiles, analog response curves, storm-mode tuning,
`skip <timer>` sugar.
