# Physical realism model

The PLC's world is microseconds to milliseconds; the physical world is
milliseconds to seconds. Three to six orders of magnitude separate
"logically possible response" from "actually possible response," and that
gap is where most field bugs live.

Two annotation surfaces declare what physics applies:

- **Tag-level**: `min=` / `max=` for analog value domain, optional
  `unit=` for display, optional `profile=` for analog feedback response.
  Peers with the existing `choices=` (which serves the discrete case).
- **Kind-level**: a `physical=KIND` flag referencing a project-level
  TOML that defines `on_delay`, `off_delay` (bool Fb only), optional
  `system=` grouping, and optional `profile=` default (analog Fb only)
  per kind.

The miner, static validator, fuzzer, condenser, and chaos/autoharness
layer all consume the same model.

Conceptually this is the PLC analog of Static Timing Analysis in chip
design — declarative metadata about physical characteristics, used to
check whether the program's timing assumptions are reachable in reality.
STA has been non-negotiable in EDA for decades because designs are too
complex to reason about timing by hand. PLC programs have the same
problem and no equivalent tooling; engineers paper over it with
defensive timers added reactively during field commissioning.

This is *not* a physics simulator (SIMIT, Emulate3D, PLCSIM Advanced).
Those run scenarios against a model of reality. The realism model here
is declarative metadata consumed at analysis time — closer to a type
system for physical behavior than to a simulation engine. Complementary
to digital twins; different tool for a different job.

## The mismatch

A scan is 1–10ms. A contact bounces for 5–50ms. A solenoid valve actuates
in 10–100ms. A burner stabilizes in seconds. Logic-level `if Input then
Output` fires in one scan; every physical device behind it responds
slower.

Classic bug: code says "if LimitSwitch then StopMotor," tests pass, field
commissioning reveals the switch bounces and the motor chatters. Fix is
always "add a debounce TON." Invisible to every tool that only reads code
— the code is internally consistent, its timing assumptions just don't
match reality. A realism model captures the physical side so the tools
can see the mismatch.

## Tag-level annotations: value domain

Peers with `choices=`. Where `choices=` declares the discrete set, `min=`
and `max=` declare the analog range.

    [min=0,max=50]                   # bare range on a Real tag
    [min=0,max=50,unit=gpm]          # with unit for display
    [min=-10,max=250,unit=degC]      # negative allowed
    [choices=Off:0|On:1]             # discrete case, unchanged

CSV composes with existing flag syntax:

    [public,min=0,max=100,unit=psi]

Semantics:

- **`min=` / `max=`** — inclusive bounds on the value domain. Writes
  outside the range are `CORE_RANGE_VIOLATION` findings, same shape as
  `CORE_CHOICES_VIOLATION`. Runtime bounds-check fallback for
  non-literal writes.
- **`unit=`** — opaque display string. No unit algebra, no conversion,
  no validation. If two tags have different `unit=` values and one is
  assigned from the other, that's not a finding. Display only; rendered
  in DataView, HMI codegen, and the LLM-facing context. Cosmetic,
  intentionally.

The range is a value-domain constraint, not a timing constraint. It
lives on the tag because every tag has its own range; kind-level
declaration would force every 0–50 GPM flowmeter to share a kind just
because they share a range, which is backwards.

## Kind-level annotations: timing and profile

A `physical=KIND` flag references a kind defined in project TOML.

    # pyrung_physics.toml

    [physical.LIMIT_SWITCH]
    on_delay = "T#5ms"
    off_delay = "T#5ms"

    [physical.VACUUM_SENSOR]
    on_delay = "T#200ms"
    off_delay = "T#50ms"
    system = "vacuum_supply"

    [physical.SOLENOID_VALVE]
    on_delay = "T#20ms"
    off_delay = "T#20ms"

    [physical.BURNER]
    on_delay = "T#3000ms"
    off_delay = "T#500ms"

    [physical.THERMOCOUPLE]
    system = "thermal_zone_1"
    profile = "generic_thermal"

    [physical.PRESSURE_TRANSMITTER]
    system = "pneumatic"
    profile = "generic_pressure"

Bool kinds and analog kinds are self-documenting by which properties
are present: `on_delay`/`off_delay` means bool Fb, `profile=` means
analog Fb. Both can carry `system=`.

### `on_delay` / `off_delay` (bool Fb only)

Asymmetric by default. `on_delay` is the nominal time from command rising
to feedback rising; `off_delay` is the nominal time from command falling
to feedback falling. A vacuum sensor may take 200ms to confirm vacuum
but 50ms to confirm vent — they're different physical processes.
Symmetric devices set them equal.

These properties apply only to bool Fb tags. When a `profile=` is
present (analog Fb), `on_delay`/`off_delay` are ignored — the profile
function owns all timing.

Units are IEC 61131 `T#` literals (`T#20ms`, `T#500ms`, `T#3s`). Parsed
into milliseconds internally. Using the IEC syntax keeps the TOML
readable to engineers who already know timer presets, and avoids unit
bikeshedding.

The miner uses these as floors on the response-time direction that
applies — `on_delay` floors "edge-rising → feedback-rising within N"
invariants, `off_delay` floors the falling-edge counterpart. Observed
behavior faster than the floor is either a sim artifact or a bug worth
surfacing.

### `profile=` (analog Fb only)

Names a registered analog response function. Declares how the feedback
tag responds when its linked En transitions. The function owns
direction, rate, shape, and decay — the full analog behavior.

`profile=` can appear at the kind level (TOML) as a default for all
tags of that kind, and at the tag level as an override. Tag wins when
both are present. This mirrors how `min=`/`max=` live on tags while
kind-level properties live in the TOML — the profile is a property
of the *installation*, not the sensor. Two thermocouples on different
burners share a kind but may respond differently.

```python
# Kind default applies — both use "generic_thermal"
Fb_Temp1 = Real(physical=THERMOCOUPLE, link="En")
Fb_Temp2 = Real(physical=THERMOCOUPLE, link="En")

# Tag override — this installation has a specific response
Fb_Temp3 = Real(physical=THERMOCOUPLE, link="En", profile="120BTU_burner")
```

See autoharness-and-chaos.md for the profile function signature,
registration decorator, and dispatch behavior.

### `system=`

Opaque grouping string. Tags/kinds sharing a `system` value are part of
the same physical resource pool — shared vacuum line, shared pneumatic
supply, shared hydraulic reservoir, shared electrical phase, shared
thermal zone.

No capacity math in the nominal schema. No `draw_lpm`, no
`capacity_gal`, no flow/load calculations. `system=` is a label, not an
arithmetic model. It exists so the chaos/autoharness layer can perturb
a shared resource and observe cross-device effects — that's a *budget*
fault (see below), and the budget belongs to the fault adapter, not the
nominal realism declaration.

Kinds that don't participate in a resource pool omit `system=` entirely.

## What consumes the model

**Miner floor.** Never propose a temporal invariant tighter than declared
physics. For bool Fb: "within 1 scan" becomes "within max(1 scan,
on_delay)" or the falling-edge equivalent. For analog Fb: the profile
function defines response dynamics; the miner does not propose
settling-time invariants tighter than observed profile behavior.
Observed behavior faster than declared physics routes to the fix bucket,
not the invariant-review bucket.

**Static validator.** Range checks against `min=`/`max=` at write sites
(`CORE_RANGE_VIOLATION`). Anti-toggle checks against
`Fb.on_delay + Fb.off_delay` as the cycle-rate floor per linked pair
(`CORE_ANTITOGGLE`) — derived from the feedback's declared physics, no
separate `min_off_ms` knob needed. Anti-toggle applies to bool Fb only.
See "Anti-toggle" below.

**Fuzzer.** Hypothesis strategies respect the declared floors when
generating transition sequences — doesn't assert edges faster than
physics allows.

**Condenser.** Reproducer `run` durations honor `on_delay` / `off_delay`
as minimum windows between a command edge and its expected confirmation
(bool Fb). For analog Fb, the condenser uses the profile's observed
settling time from the recorded session.

**Chaos / autoharness adapter.** Consumes the same kinds, interprets
them as nominal-behavior baselines against which perturbations are
defined. For bool Fb, perturbations are timing faults against
`on_delay`/`off_delay`. For analog Fb, chaos adapters wrap or replace
the profile function. See the autoharness-and-chaos doc for the full
adapter model.

**Review UI.** Three buckets for mined temporal findings:

- Observation matches declared physics → accept into spec.
- Observation slower than declared physics → accept; physics is a floor,
  not a ceiling.
- Observation faster than declared physics → **fix the program**.
  Suggestion attached: "add delay of `on_delay`/`off_delay`."

## Fault taxonomy

Faults that the realism model informs fall into three shapes — every
PLC-observable fault decomposes into one of them:

- **Value faults** — tag reads outside its declared domain. Sensor
  stuck at a value, sensor pegged at `min`/`max`, analog drift outside
  range. Declared by `min=`/`max=` on the tag; perturbed by the chaos
  layer as "force value outside range" or "hold value stuck."
- **Timing faults** — feedback arrives outside its declared window.
  `Fb` never confirms after `En` edge (stuck actuator), `Fb` confirms
  much later than `on_delay` (slow response), `Fb` chatters (repeated
  edges within `on_delay`). Declared by `on_delay`/`off_delay` on the
  kind (bool Fb); perturbed by the chaos layer as window violations.
  For analog Fb, timing faults manifest as the profile response
  deviating from nominal — slower ramp, stalled value, oscillation.
- **Budget faults** — cross-device shared-resource violations. Multiple
  `system="vacuum_supply"` devices energized simultaneously beyond the
  supply's capacity; shared hydraulic pump starved; shared electrical
  phase overloaded. Declared by `system=` grouping; perturbed by the
  chaos layer as "withdraw the shared resource" and observed across
  every member of the group.

Value and timing are per-device; budget is per-`system`. The nominal
realism model carries just enough to identify which tags and kinds
participate in each fault class; the adapter defines what perturbation
to apply and how it propagates.

## Anti-toggle: still static, via the linked Fb

Previously, the anti-toggle check was motivated by a `min_off_ms` kind
property: a program path that could de-energize and re-energize a
solenoid faster than its minimum off-time is a bug. `min_off_ms` is
gone from the nominal schema, but **the static check survives** —
it just derives the floor from the linked feedback's timings rather
than a separate knob.

Anti-toggle applies to bool Fb only. Analog Fb does not have a
meaningful cycle-rate floor derivable from the realism model; analog
cycling behavior is domain-specific and belongs in chaos adapters.

The reasoning: an actuator cannot complete a cycle faster than
`Fb.on_delay + Fb.off_delay`, because the linked sensor physically
cannot confirm the transitions any faster. A program that commands
cycling below that floor is either:

- Issuing commands the device can't execute (observable as `Fb`
  never tracking the `En` edges — coupling-check finding).
- Depending on behavior that won't happen in the field (the device
  will appear stuck from the PLC's view, or respond to only a
  subset of commands).

Both are bugs. The static validator walks for `En` paths that could
produce rising-then-falling (or falling-then-rising) edges within
`Fb.on_delay + Fb.off_delay` under some reachable input combination,
and flags them as `CORE_ANTITOGGLE` findings citing the triggering
input sequence and the declared Fb timings.

No `min_off_ms` needed — the linked feedback's declared response
times *are* the physical floor on cycle rate. If the engineer wants
to override for a specific device ("this actuator tolerates faster
cycling than its sensor can confirm"), they can set a looser
tolerance on the coupling check, but the default is derived from
physics already declared for other reasons.

Other dropped properties (`min_on_ms`, `warmup_ms`, `cooldown_ms`,
`debounce_ms`, `max_cycles_per_hour`) are genuinely fault-regime:
they describe misbehavior boundaries rather than nominal response,
and the linked Fb doesn't inform them. They belong in chaos adapters
when the perturbations they enable are worth the schema cost. None
make the nominal schema.

## Composition with existing flags

- `external + physical=KIND` — field input with kind physics. Fuzzer
  respects floors; miner applies them.
- `external + choices + physical=KIND` — enumerated field input (mode
  selector, analog with discrete levels).
- `external + min + max + physical=KIND` — analog field input with
  range and kind.
- `external + min + max + physical=KIND + profile=NAME` — analog field
  input with range, kind, and explicit response profile.
- `final + physical=KIND` — single-writer output to hardware. Both
  disciplines enforced.
- `readonly + physical=KIND` — incoherent (readonly means nothing writes;
  physical implies responsive hardware). Reject at construction time.
- `min`/`max` + `choices` — incoherent; choices is the discrete case,
  range is the analog case. Reject at construction time.
- `profile=` + no `link=` — incoherent; profile defines response to a
  linked En. Reject at construction time.
- Bool Fb + `profile=` — incoherent; bool Fb uses `on_delay`/`off_delay`.
  Reject at construction time.

Software-only tags opt out by omitting `physical=`. Explicit
`[physical=NONE]` for the residual cases where a tag looks physical but
isn't. `physical=` on a pivot (software tag) warns — the flag is for
tags corresponding to real-world devices or processes, not internal
state.

## UDT coupling via link=

The realism model extends naturally to user-defined types. A UDT
representing a physical device declares its actuator and feedback tags,
and uses `link=` on the feedback side to explicitly pair it to the
corresponding command.

```python
@udt
class Solenoid:
    Cmd = Bool(public=True)
    Sts = Bool(public=True, final=True)
    En = Bool(physical=SOLENOID_VALVE)
    Fb = Bool(physical=LIMIT_SWITCH, link="En")

@udt
class VacuumGripper:
    Cmd = Bool(public=True)
    Sts = Bool(public=True, final=True)
    En = Bool(physical=VACUUM_VALVE)
    Fb_Contact = Bool(physical=LIMIT_SWITCH, link="En")
    Fb_Vacuum = Bool(physical=VACUUM_SENSOR, link="En")

@udt
class TwoStageActuator:
    Cmd = Bool(public=True)
    Sts = Bool(public=True, final=True)
    En_Pilot = Bool(physical=PILOT_VALVE)
    En_Main = Bool(physical=MAIN_VALVE)
    Fb = Bool(physical=LIMIT_SWITCH, link="En_Main")   # confirms main only

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

The Heater example shows bool and analog Fb coexisting in one UDT.
`Fb_Contact` is bool — auto-driven by `on_delay`/`off_delay` from its
LIMIT_SWITCH kind. `Fb_Temp` is analog — driven by the
`120BTU_burner` profile, overriding the THERMOCOUPLE kind's
`generic_thermal` default. Both link to the same `En`. The harness
dispatches each independently based on Fb type.

The coupling is declared, not inferred. Names are cosmetic — engineers
who prefer `Drive` and `Confirm` just update the link string
accordingly. The analyzer reads `link=` and runs the coupling check
between whatever fields are pointed at.

### Link semantics

- **Same-scope only.** `link=` resolves within the UDT (or named array)
  that declares it. No cross-UDT linking; if a coupling crosses device
  boundaries, that's modeled as two separate devices interacting, not
  one linked pair.
- **String must be an attribute of the same UDT.** Validated at UDT
  construction. `link="DoesNotExist"` raises at class-body time, not
  runtime.
- **Direction is always `Fb → En`.** The link points from the feedback
  at its triggering actuator. No reverse or bidirectional links;
  keeps the schema minimal.
- **Multiple `Fb*` can link to the same `En`.** Each coupling check runs
  independently against the linked actuator. Bool and analog Fb can
  link to the same En — each dispatches through its own mechanism.
- **Each `Fb*` links to exactly one `En`.** Simplifies the windowing and
  avoids any-of / all-of ambiguity. Devices with fan-in feedback
  (one sensor confirming multiple commands) factor into separate UDTs
  with explicit interface contracts.

### Coupling check

For bool Fb: for every `En` rising edge, the analyzer expects every
bool `Fb*` linked to it to transition within that sensor's
`on_delay + tolerance`. For falling edges, `off_delay + tolerance`.
Missing transitions = feedback failure (stuck valve, broken sensor,
miswired IO). Transitions without a corresponding `En` edge on the
linked actuator = spurious feedback or some other path actuating the
device. Findings cite the specific linked pair and observed vs
expected timing.

For analog Fb: the coupling check verifies that the profile function
is being invoked by the harness — i.e., that a profile is registered
and the linked En edge triggers the analog response. The profile
function itself defines what "correct response" looks like; the
coupling check doesn't interpret the profile's output beyond
confirming it runs.

Multi-feedback devices: each `Fb*` is checked independently against its
linked `En`, with its own kind-declared window (bool) or profile
(analog). A gripper whose `Fb_Contact` confirms but `Fb_Vacuum` doesn't
within spec is a partial failure — contact closed, seal didn't form —
surfaced automatically per instance, no hand-written interlock needed.
A heater whose `Fb_Contact` confirms but `Fb_Temp` never reaches the
program's setpoint is likewise a partial failure, surfaced by the
program's own timeout logic exercised against the profile's ramp rate.

### Device template

Pyrung ships a `PhysicalDevice` UDT template with every field already
annotated. Engineers clone it, rename, fill in the kinds, delete what
they don't need.

```python
@udt
class PhysicalDevice:
    # Public interface — caller-facing
    Cmd = Bool(public=True)
    Sts = Bool(public=True, final=True)

    # Parameter interface — optional, one group per parameter
    # Cmd_P1 = Real(public=True, min=0, max=100, unit="gpm")
    # Sts_P1 = Real(public=True, final=True, min=0, max=100, unit="gpm")
    # Adm_P1_Name = Str(public=True, readonly=True)

    # Admin / nameplate — read-only metadata
    Adm_Name = Str(public=True, readonly=True)

    # Physical coupling — private implementation
    En = Bool(physical=...)               # fill in actuator kind
    Fb = Bool(physical=..., link="En")    # fill in sensor kind (bool)
    # Fb_Analog = Real(physical=..., link="En",
    #                  min=..., max=..., unit="...",
    #                  profile="...")      # analog sensor (optional)
```

The prefix convention encodes a semantic bundle per field role:

- **`Cmd_*`** — public, caller-writable. The request surface.
- **`Sts_*`** — public, `final`. The authoritative validated value;
  written once per scan by the UDT's own logic.
- **`Adm_*`** — public, `readonly`. Configuration and nameplate
  metadata set at power-on.
- **`En_*`** — private, `physical=actuator`. Field-facing command.
- **`Fb_*`** — private, `physical=sensor`. Field-facing feedback,
  coupled to an `En_*` via `link=`. Bool or analog; dispatched
  accordingly by the harness.

This is PackML's Command/Status/Admin taxonomy (ISA-TR88.00.02) with
flags attached. Engineers from packaging backgrounds recognize the
pattern immediately; pyrung's contribution is making it
analyzer-enforced rather than documented-only.

The template is explicit rather than inferred. No "if a tag starts with
`Cmd_` we auto-apply `public=True`" magic, no "we find the `Fb` and pair
it with the `En` by name" sniffing. The engineer writes `public=True`
and `link="En"` because they cloned a template that already had them.
Explicit declarations are honest, greppable, and easy to override
per-field when a real program needs to deviate.

### What the analyzer actually reads vs what's convention

Worth being explicit about which naming is load-bearing:

- **`Cmd`/`Sts`/`Adm`** — convention for humans and PackML compatibility.
  The analyzer reads the flags (`public`, `final`, `readonly`), not the
  names. Rename freely; keep flags correct.
- **`En`/`Fb`** — convention for the template and readability. The
  analyzer reads `link=`, not the names. Rename freely; keep the link
  string pointing at the right attribute.

Nothing in the analyzer matches on field names. The names are
consistency, not contract.

### Physical = device-level, not just tag-level

Once UDTs exist, the `physical=` flag graduates from per-tag annotation
to device-type definition. Declare `Solenoid` once with its En/Fb
kinds and link; instantiate it twenty times across the program; every
instance inherits the full analysis surface (physics floors on
invariants, coupling checks, chaos perturbation points, analog profile
dispatch) identically. The realism model is reused at every
instantiation without repetition.

## What needs building from current state

1. **Tag fields** — add `min`, `max`, `unit` to the Tag dataclass with
   validation (`min < max`, incoherence with `choices`).
2. **`physical=` field on Tag** — dataclass attribute with validation.
3. **`profile=` field on Tag** — optional string, validated against
   registered profile functions. Incoherent with bool Fb (reject at
   construction time). Incoherent without `link=` (reject).
4. **Click comment parser** — `[min=X,max=Y,unit=Z]`,
   `[physical=KIND]`, and `[profile=NAME]` tokens via the existing
   CSV syntax.
5. **Realism model loader** — TOML at project root, kinds →
   `{on_delay, off_delay, system, profile}` maps. `T#` literal parsing.
   Bool kinds carry timing; analog kinds carry profile; both optional
   `system`.
6. **Miner floor** — temporal-invariant proposer reads model, applies
   `on_delay`/`off_delay` as lower bounds per edge direction (bool Fb).
   Analog Fb miner behavior deferred to profile-aware observation.
7. **`CORE_RANGE_VIOLATION` validator** — literal writes checked against
   `min=`/`max=`, runtime fallback for non-literals.
8. **Condenser integration** — `run` durations honor `on_delay` /
   `off_delay` (bool Fb). Analog Fb uses observed profile settling time.
9. **Fuzzer integration** — strategies respect floors.
10. **Coupling check** — per-link feedback validation using `on_delay`
    for rising and `off_delay` for falling edges (bool Fb). Analog Fb
    coupling check verifies profile registration and invocation.
11. **Review UI bucket** — "fix the program" distinct from accept / deny
    / suppress.

## Deferred

- **Analog response curves** — hysteresis, settling profiles, PID-style
  dynamics. Profile functions handle response shape for v1; richer
  curve primitives (exponential approach, S-curve) are adapter-level
  refinements.
- **Fault-adapter properties** — `min_on`, `min_off`, `debounce`,
  `warmup`, `cooldown`, `max_cycles_per_hour`. These describe misbehavior
  boundaries and belong to the chaos/autoharness layer, not the nominal
  schema. Add back as opt-in per-kind properties if real programs
  demand static enforcement rather than sampled detection.
- **Budget arithmetic** — `draw`, `capacity`, load math on `system=`
  groups. Nominal schema has grouping only; arithmetic belongs to
  budget-fault adapters.
- **Unit algebra** — `unit=` is cosmetic by design. If unit mismatches
  become a real bug source worth flagging, a conversion/algebra layer
  could be added later without changing the annotation surface.
- **Learned physics from observation** — infer `on_delay`/`off_delay`
  from recorded behavior. Correct defaults matter more first.
- **Hardware kind inference from naming** — "this tag's name implies
  SOLENOID." Tempting; brittle. Explicit annotation is honest.
- **Import from device description standards** — IODD (IO-Link devices),
  EDDL, FDT/DTM, OPC UA device info models already publish timing
  characteristics for most IO-Link and fieldbus devices. A future
  importer could populate kinds automatically from device files attached
  to the plant's hardware. Out of scope for v1, but worth not
  precluding — property names (`on_delay`, `off_delay`) should stay
  compatible with IODD vocabulary where concepts overlap.

## Why it's worth its own artifact

The realism model is a parallel document to the program itself. Captures
the plant's physics once, so every subsequent analysis respects it.
Cheap to author — three to four properties per kind (timing or profile,
plus optional system), a range-and-unit per analog tag, declared once
per project, reused across every tag of that kind. Unlocks findings no
pure code analysis can produce: the mismatch between what the program
*says* happens and what the plant *allows* to happen.

Every entry is a statement about reality that the engineer knows and the
tools don't. Capturing those statements, once, in a form the tools can
consume, is disproportionately high-leverage. A TOML with a dozen kind
definitions and a handful of profile functions may prevent more field
bugs than hundreds of unit tests.
