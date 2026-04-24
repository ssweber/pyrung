# Autoharness and chaos

The physical realism model plus UDT `link=` coupling is already a
complete specification of how a device should respond: "`Fb` linked to
`En` with `physical=LIMIT_SWITCH` (`on_delay=T#5ms`)" means En rises, Fb
rises 5ms later. The same declaration used for static analysis and
invariant-miner floors is also a complete specification for driving a
test harness and perturbing it. Four consumers, one model.

## The missing piece

Historically, testing a device-heavy PLC program meant writing test code
that toggles inputs at the right moment: "step 3: set `LS_1 = True`
after `SOL_A.En` goes high." Twenty solenoids, twenty feedback loops,
twenty blocks of harness boilerplate — maintained by hand, diverging
from the device model over time, and wrong in subtle ways (the engineer
used `5ms` in the test, the realism model said `20ms`).

The autoharness closes this gap. The realism model already declares
nominal feedback behavior. The harness reads the declaration and
synthesizes the feedback closure at every instance. Engineers write
zero lines of feedback-toggle code.

Once the nominal harness is automatic, chaos — perturbing nominal
responses via user-authored fault adapters — falls out of the same
mechanism. The scheduler enqueues `Fb*` patches in response to `En*`
edges: nominal enqueues what the model declares; chaos routes through
an adapter that may enqueue something else.

## Nominal autoharness

The harness dispatches on the Fb tag's type. Bool Fb is fully
automatic — the realism model provides everything the harness needs.
Analog Fb delegates to a user-authored profile function — the
response shape is too domain-specific for the harness to assume.

### Bool Fb: scheduler-driven

For every instance of a UDT with bool `Fb*` and `link=` coupling, the
harness installs an edge observer:

- On `En` rising edge: schedule an `Fb*=True` patch for tick
  `now + on_delay` (per linked Fb's declared kind).
- On `En` falling edge: schedule an `Fb*=False` patch for tick
  `now + off_delay`.

On/off delays are asymmetric by default — a solenoid energizes in 20ms
and de-energizes in 80ms. Multiple `Fb*` linked to one `En` schedule
independently, each with its own kind's timing. A vacuum gripper's
`Fb_Contact` (LIMIT_SWITCH, on_delay=T#5ms) and `Fb_Vacuum`
(VACUUM_SENSOR, on_delay=T#80ms) schedule independently from the same
En rising edge.

```python
# What the engineer wrote
@udt
class VacuumGripper:
    Cmd = Bool(public=True)
    Sts = Bool(public=True, final=True)
    En = Bool(physical=VACUUM_VALVE)
    Fb_Contact = Bool(physical=LIMIT_SWITCH, link="En")
    Fb_Vacuum  = Bool(physical=VACUUM_SENSOR, link="En")

# What they did NOT have to write
def test_gripper_cycle():
    gripper.Cmd = True
    plc.step()
    assert gripper.En.value
    plc.run("5ms")
    gripper.Fb_Contact.value = True   # <-- this line
    plc.run("75ms")
    gripper.Fb_Vacuum.value = True    # <-- and this one
    ...
```

The harness enqueues `Fb_Contact=True` at tick 5ms and `Fb_Vacuum=True`
at tick 80ms automatically. The test reads `assert gripper.Sts` at
the end.

### Analog Fb: profile-driven

For every instance of a UDT with analog (Real/Int) `Fb*` and `link=`
coupling, the harness delegates to a registered profile function.
The profile is named by the `profile=` property on the tag (or
defaulted from the kind's `profile=` in the TOML).

The profile function signature is `(cur, en, dt)`:

- `cur` — current value of the Fb tag.
- `en` — current state of the linked En (True/False).
- `dt` — PLC scan period in seconds.

The function returns the next value. The harness calls it once per
tick while the profile is active, enqueuing the returned value as a
patch. The engineer writes in real-time units (per second); `dt`
makes the math stable across scan rates.

```python
@pyrung.analog.profile("120BTU_burner")
def burner_120btu(cur, en, dt):
    if en:
        return cur + 0.8 * dt   # 0.8 degrees per second
    return cur - 0.05 * dt      # slow ambient decay

@pyrung.analog.profile("generic_thermal")
def generic_thermal(cur, en, dt):
    if en:
        return cur + 0.5 * dt
    return cur                  # hold on En fall

@pyrung.analog.profile("generic_pressure")
def generic_pressure(cur, en, dt):
    if en:
        return cur + 10.0 * dt  # 10 PSI per second
    return cur - 5.0 * dt       # bleed down
```

The profile owns everything: direction, rate, shape, decay. A heater
ramps up on En rise and decays slowly. A chiller ramps down on En
rise and drifts back up. A pressure system builds on En rise and
bleeds on En fall. Each is a few lines of Python expressing the
physics the engineer already knows.

The program's own logic controls when En drops. A heater program
turns off En when Fb_Temp hits the setpoint — the profile was ramping
toward max, but the program cut it off at 180°C. The harness doesn't
need to know the settling point; the program does.

```python
@udt
class Heater:
    Cmd = Bool(public=True)
    Sts = Bool(public=True, final=True)
    En = Bool(physical=SOLENOID_VALVE)
    Fb_Contact = Bool(physical=LIMIT_SWITCH, link="En")
    Fb_Temp = Real(physical=THERMOCOUPLE, link="En",
                   min=0, max=250, unit="degC",
                   profile="120BTU_burner")
```

`Fb_Contact` is bool — auto-driven by scheduler, no engineer effort.
`Fb_Temp` is analog — driven by the `120BTU_burner` profile. Both link
to the same `En`. The harness dispatches each independently based on
Fb type.

### Analog Fb validation

- **Analog Fb + link + no profile resolved** — finding:
  `CORE_MISSING_PROFILE`. The harness can't guess; the engineer must
  assign a profile at the tag or kind level.
- **Bool Fb + profile** — finding: `CORE_INCOHERENT_PROFILE`. Bool Fb
  uses `on_delay`/`off_delay`; profile is for analog only.
- **Profile string doesn't match any registered function** — finding:
  `CORE_UNKNOWN_PROFILE`. Typo or missing registration.

### Patch provenance

Every patch entering the scan queue carries a source tag:

- `human` — engineer typing at the DAP console.
- `llm` — LLM driving via `pyrung-live`.
- `dap` — DAP UI actions (buttons, force dialogs).
- `harness:nominal` — autoharness-synthesized bool feedback from a
  declared `link=`.
- `harness:analog:<profile_name>` — profile-driven analog feedback,
  tagged with the profile name.
- `harness:chaos:<adapter_name>` — chaos-perturbed feedback, tagged
  with the adapter name.

Harness-issued `Fb` transitions land in the transcript as regular
patch lines; the provenance tag is another column. No separate chaos
log, no parallel event stream. The finding reporter cites adapter
name and target by reading the tag off the offending patch line.

### No harness opt-out per test

There is no per-test flag to disable the autoharness. When an engineer
wants to test alarm behavior by withholding feedback — "does the
fault timer trip when `Fb` never confirms?" — that's a chaos adapter
(`missing_feedback`, `stuck_off`), not a harness override. This
preserves "every test runs the real program": same scan engine, same
harness, same patch queue. It also kills the maintenance-mode-drift
failure mode of hand-written harnesses, where one test disables
feedback to exercise a path and the harness code quietly diverges from
the program's physical model as both evolve.

### Scheduling and dt

The scheduler reads the PLC's `dt` (scan granularity) and converts
declared delays to tick counts. `on_delay=20ms` at `dt=0.010`
schedules `Fb` at `now + 2`; at `dt=0.001`, `now + 20`; at `dt=0.100`,
`now + 1` (1-tick floor — you can't schedule a patch in the past).
The PLC's `realtime=` flag controls pacing (wall-clock sleep between
scans) independently; the scheduler behaves identically either way.

For analog Fb, there is no delay-to-tick conversion — the profile
function is called every tick with the current `dt`, and `dt` appears
in the function body so the engineer writes rate-per-second math.
The profile is dt-stable by construction.

Typical choices:

- `plc(dt=0.010, realtime=True)` — engineer at the console,
  wall-clock paced. Physical intuition matches sim clock. Default for
  live sessions.
- `plc(dt=0.010)` — CI and LLM-driven sessions, unpaced. Runs as fast
  as Python iterates. Default for sim sessions and replay.
- `plc(dt=0.001)` — chaos or other timing-sensitive runs where
  sub-10ms misbehavior matters.
- `plc(dt=0.100)` — long-duration scenarios where sub-scan precision
  doesn't; declared delays round up to the 1-tick floor.

Program-internal waits (TON presets, soak timers) are *not* affected
by dt beyond iteration count — a `TON(T#1h)` still counts through its
preset regardless of dt. Skipping a program wait is done by patching
the timer's `.ACC` directly:

```
patch SoakTimer.ACC 3600000   # pretend an hour passed
```

A patch like any other — recorded in the transcript, replayed
deterministically, provenance tag on the line. An optional
`skip <timer>` console verb desugars to the same patch.

Miner consequence: invariants are tagged with the dt they were
observed under. Causal-ordering invariants (`A before B`) are stable
across dt; wall-clock bounds (`A within 200ms`) observed only at
`dt=0.010` remain candidates until confirmed at finer dt.

## The realism model the harness consumes

The schema is deliberately minimal. Bool kinds carry timing; analog
kinds carry a profile name. Everything else is either a value-domain
constraint at the tag level or a fault shape expressed by an adapter.

```toml
[physical.LIMIT_SWITCH]
on_delay = "T#5ms"
off_delay = "T#5ms"
system = "electrical_24vdc"

[physical.SOLENOID_VALVE]
on_delay = "T#20ms"
off_delay = "T#80ms"
system = "pneumatic"

[physical.THERMOCOUPLE]
system = "thermal_zone_1"
profile = "generic_thermal"

[physical.PRESSURE_TRANSMITTER]
system = "pneumatic"
profile = "generic_pressure"

[system.electrical_24vdc]
[system.pneumatic]
[system.thermal_zone_1]
```

Bool kinds: `on_delay`, `off_delay`, optional `system`. Analog kinds:
`profile`, optional `system`. The presence of timing vs profile is
self-documenting — you can tell at a glance whether a kind drives
bool or analog feedback.

## Chaos: user-authored adapters

Faults decompose into value, timing, and budget shapes — one per axis
of the realism model (value domain, timing, system grouping); see
physical-realism.md.

The purpose of chaos is "does the program catch the fault," not "does
the plant model predict the fault." The electrician already sized the
supply; the system designer already did the load calculations. pyrung
doesn't redo that math. The controls engineer's question is: if
something fails, does my program notice?

That question is yes/no per fault mode. Whether the brownout happens
at 9 devices or 11 doesn't matter — what matters is whether the
program's alarm logic, safety interlocks, and recovery sequences
trigger correctly when the symptoms appear.

So chaos adapters are user-authored. The engineer writes the
misbehavior they want to inject; pyrung orchestrates where and when.

```python
@pyrung.chaos.adapter("slow_actuator")
def slow_actuator(device):
    device.on_delay *= 5       # 20ms becomes 100ms

@pyrung.chaos.adapter("intermittent_contact")
def intermittent_contact(device):
    device.edge_drop_prob = 0.1   # 10% of edges dropped

@pyrung.chaos.adapter("sensor_drift")
def sensor_drift(device):
    device.value_transform = lambda v: v * 0.85

@pyrung.chaos.adapter("brownout")
def brownout(system_members):
    for device in system_members:
        device.on_delay *= 3
        device.edge_drop_prob = 0.2
```

Adapters receive either a single device (value or timing faults) or a
list of devices sharing a system (budget faults). They mutate the
device's harness behavior for the duration of the chaos window. The
adapter's body is the engineer's expression of what the failure looks
like in their plant; pyrung doesn't prescribe semantics.

### Chaos on analog Fb

Chaos adapters can perturb analog profiles the same way they perturb
bool timing. The adapter wraps or replaces the profile function:

```python
@pyrung.chaos.adapter("sensor_noise")
def sensor_noise(device):
    original = device.profile_fn
    def noisy(cur, en, dt):
        return original(cur, en, dt) + random.gauss(0, 2.0)
    device.profile_fn = noisy

@pyrung.chaos.adapter("stuck_analog")
def stuck_analog(device):
    device.profile_fn = lambda cur, en, dt: cur  # frozen
```

The original profile provides the nominal baseline; the chaos adapter
expresses how the failure deviates. Same pattern as bool chaos where
`on_delay` is the baseline and `slow_actuator` multiplies it.

### Chaos console verbs

Chaos is configured through console verbs, so recorded sessions stay
plain text under one grammar:

```
chaos slow_actuator SOL_1       # apply adapter to one device
chaos brownout pneumatic        # apply to all devices in the system
chaos sensor_noise HEATER_1.Fb_Temp  # perturb an analog profile
chaos random device             # roll random device faults each scan
chaos storm                     # everything, randomly
chaos clear                     # remove all active adapters
```

Pytest flags remain as a CLI surface for batch runs and translate to
the same verbs at session start: `pytest --chaos=slow_actuator
--target=SOL_1` emits `chaos slow_actuator SOL_1` into the transcript.
Interactive and batch runs produce identical sessions.

Findings cite the adapter name and target by reading the patch
provenance tag off the offending scan:

```
FINDING (chaos): Gripper.Sts never rose within 5s under
  slow_actuator(SOL_5).

  Injected:  slow_actuator(SOL_5) at scan 4132
  Observed:  SOL_5.En raised, SOL_5.Fb arrived at scan 4221 (890ms)
  Expected:  gripper.Sts should track Fb within one scan
  Path:      Cmd → [rung 42] → SOL_5.En → Fb → [rung 67] → Sts
  Hypothesis: program has no timeout on Fb arrival; blocks indefinitely
```

Fault name, affected device, causal path, and timing gap all flow
from the provenance already on the patch line.

Default chaos is one device at a time. Most bugs in "we forgot to
handle this sensor dying" shape are caught by single-fault injection
across many scans. System-scope chaos is rarer as a regression test
but catches the class of bugs that only manifest under correlated
failure, which is also the class most likely to be catastrophic in
production.

## What gets built

The DAP owns `plc.scan()`. Every tick: drain pending patches from all
sources (human, llm, harness) → scan → emit deltas. The autoharness
is a patch producer feeding that same pre-scan queue, not a mid-scan
callback that writes tag values directly.

1. **Scheduler primitive** — heap of `(sim_tick, patch)` entries.
   Drained by the DAP tick driver before each scan; each due entry
   enqueues its patch through the console evaluator, tagged with the
   producing provenance (`harness:nominal` or
   `harness:chaos:<adapter_name>`). Tick counts derived from the
   PLC's `dt`.
2. **Autoharness installer** — walks UDT instances, finds every `link=`
   coupling, installs rising/falling edge observers on the En field.
   Dispatches on Fb type: bool → scheduler with on_delay/off_delay;
   analog → per-tick profile call.
3. **Profile registry** — `@pyrung.analog.profile(name)` decorator,
   stores profile fns keyed by name. Resolved from tag `profile=` or
   kind default.
4. **Adapter registry** — `@pyrung.chaos.adapter(name)` decorator,
   stores adapter fns keyed by name.
5. **Chaos runner** — selects adapters per scan/window, applies to
   target devices or system members, labels events. For analog Fb,
   wraps or replaces the active profile function.
6. **Pytest plugin flags** — `--chaos=NAME`, `--target=DEVICE`,
   `--system=NAME`, `--scope=device|system`. Translated to `chaos …`
   console verbs at session start; pytest and interactive sessions
   produce identical transcripts.
7. **Finding reporter** — chaos-aware output that cites the injected
   adapter alongside the observed violation. Profile-aware for analog
   findings.

## What this collapses

Without autoharness, a typical device-heavy test file is 80% harness
boilerplate and 20% assertion. The engineer writes
`gripper.Fb_Contact = True` after timing the delay by hand, repeats
for every device, maintains those delays when the realism model
changes, and re-authors per-test.

With autoharness:

```python
def test_gripper_cycle():
    gripper.Cmd = True
    plc.run("200ms")
    assert gripper.Sts
```

Every feedback response closes automatically — bool via scheduler,
analog via profile. Realism-model changes propagate without touching
tests. Adding a new UDT instance adds a new auto-driven harness loop
with no test code added.

With chaos on top:

```python
def test_gripper_survives_chaos():
    gripper.Cmd = True
    plc.run("5s")
    # assertion is the program's own safety invariants
```

Runs under random fault injection; miner surfaces which adapters break
which invariants; each finding fully traced.

## Why this is novel in PLC-space

Fault injection for PLCs exists as HIL rigs (dSPACE, NI VeriStand,
Beckhoff XAE). Those inject at runtime against a physical HIL bench,
do not analyze outcomes, and require hand-authored fault scenarios
per test. The integrated loop — declare device, synthesize nominal
harness, reuse the declaration to perturb it — is downstream of the
flag-and-link discipline and doesn't exist in tools that lack that
substrate.

Hardware FMEA lives in spreadsheets disconnected from code. IEC 61508
and 61511 hazard analyses are worksheet exercises. None of these are
executable against the program.

The autoharness + chaos loop makes the realism model do four jobs
from one declaration: static analysis (validator), invariant floor
(miner), nominal test driver (autoharness — bool and analog), adversarial
test driver (chaos). Each added job is ~100 lines of code against the
existing substrate.

## Deferred

- **Adapter library.** v1 ships core examples (slow_actuator,
  intermittent_contact, sensor_drift, brownout, sensor_noise,
  stuck_analog). A library of community-contributed adapters — keyed
  by physical kind or system — is v2.
- **Profile library.** v1 ships core profiles (generic_thermal,
  generic_pressure). A library of common analog response profiles —
  keyed by device application — is v2.
- **Learned fault profiles from field history.** Observe which faults
  actually occur, fit distributions, seed adapters from them.
- **Shrinking on chaos findings.** Hypothesis-style minimal fault
  sequence for a given failure. Today's chaos is random; shrinking
  produces "the smallest fault pattern that triggers this bug."
- **Operator fault profiles.** Double-press, panel-distance physical
  constraints, reaction time under alarm. Same adapter pattern,
  different domain.
- **Storm mode tuning.** Under heavy multi-fault injection most
  invariants fail; the question shifts to which fail first and how
  recoverable the program is. Probably a separate report shape from
  nominal regression.
- **`skip <timer>` sugar.** Convenience verb for `patch
  <timer>.ACC <preset-1>`; trivial to add once the core patch path
  is stable.

## The shape, summarized

Declare devices once with `link=`, `physical=KIND`, and optionally
`system=` and `profile=`. The realism TOML declares `on_delay`,
`off_delay`, and `system` per bool kind; `profile` and `system` per
analog kind. The nominal harness runs automatically — bool Fb via
scheduler, analog Fb via profile function with `(cur, en, dt)`
signature. Chaos adapters — user-written Python — perturb timing (bool)
or wrap/replace profiles (analog). Tests consist of operator-surface
stimulation and assertion on program invariants; nothing about feedback
toggling, nothing duplicated from the device model, nothing about fault
semantics pyrung doesn't need to know. The realism model does four
jobs from a declaration the engineer would write anyway.
