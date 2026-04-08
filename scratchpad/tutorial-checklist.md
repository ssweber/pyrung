# Tutorial Recommendations Checklist

## Blocking Bugs
- [x] **L8** — Fix `NameError`: `out(Light)` / `latch(Running)` used before `Light` / `Mode` defined
- [x] **L7** — Rename `COUNTING` state -> `RESETTING` or `CLEANUP`
- [x] **L2** — Fix Python instinct strawman (use typed `conveyor_speed: int = 0`, then pivot)

## Overall
- [x] Add Mermaid diagram to every lesson (ladder rungs, state machines, etc.)
- [ ] Make early exercises adversarial (L1, L2, L4 are passive — match L3/5/6/7/8/10 rigor)
- [ ] Standardize `with runner.active()` pattern across all lessons (canonical in L10)
- [ ] Fix title inconsistency ("Testing Like You Mean It" vs sidebar "Testing")

## Landing Page
- [ ] Lead with "pyrung won't let you cheat"
- [ ] Surface the pedagogical scaffold as a bullet
- [ ] Fix lesson title consistency
- [ ] Better L11 teaser: "Map your project to a real Click PLC or P1AM-200"
- [ ] Add TitleCase footnote in prerequisites

## Per-Lesson

### L1 — Scan Cycle
- [ ] Promote "last one wins" to a callout with example
- [x] Add "Heads up" box: `out`=OTE, `X`=XIC, `~X`=XIO

### L2 — Tags
- [ ] Explain doubled-name string (`ConveyorSpeed = Int("ConveyorSpeed")`)
- [ ] Promote retentive vs non-retentive to its own subheading
- [x] Add naming convention admonition + PLC tag-limit table
- [x] Add "Heads up" box: type aliases across vendors

### L3 — Latch and Reset
- [x] Rename `Estop` -> `StopBtn` throughout early lessons, use as `~StopBtn`
- [x] Teach `~` as "NC contact," not "NOT" — dedicated callout
- [x] Land latch vs `out` distinction harder (sticky vs non-sticky)
- [x] Forward ref to L8 seal-in and L11 E-stop (one line each)
- [x] Add "Heads up" box: `latch`=SET/OTL, `reset`=RST/OTU

### L4 — Assignment
- [x] Give `rise()` / edge detection its own mini-section
- [x] Name instruction-order-within-rung rule before exercise uses it
- [x] Add one line on why clamp (`copy`) vs wrap (`calc`)
- [x] Note `copy(source, dest)` argument order + vendor variance
- [x] Fix naming: `LastSize`/`PreviousSize` ambiguity
- [x] Add "Heads up" box: `copy`=MOV, `calc`=MATH/CPT, `rise()`=ONS/R_TRIG

### L5 — Timers
- [ ] Sharpen "this is why pyrung exists" (name `freezegun` alternative)
- [x] Show TON vs RTON as same instruction +/- `.reset()` chain
- [x] Add terminal-chain callout ("Why is `.reset()` terminal?")
- [x] Explain accumulator tag (done bit + acc, foreshadow structured tags)
- [ ] Add `Tms`/`Ts` naming sidebar ("Why `Tms` and not `Milliseconds`?")
- [ ] Flag `TD` naming collision (Click timer-data vs pyrung day unit)
- [x] Add "Heads up" box: TON/TOF/RTO across vendors

### L6 — Counters
- [x] Lead with "counters count every scan, not edges" — use `rise()` for edges
- [x] Promote "chip with multiple input pins" to Key Concept callout
- [x] State counter/timer parallel (both chain `.reset()`)
- [ ] Show bidirectional counter (`count_up(...).down(...).reset(...)`)
- [x] Add "Why `Dint`, not `Int`?" one-liner (16-bit rolls at 32,767)
- [ ] Name the meta-irony (Python loops in test, no loops in logic)
- [x] Add "Heads up" box: CTU/CTD/CTUD across vendors

### L7 — State Machines
- [x] Replace magic numbers with tag-as-constant pattern (`IDLE = Int("IDLE", initial=0)`)
- [x] Name-drop PackML (give learners a search term)
- [x] Explicit `rise()` callback from L4
- [x] Explain repeated `State == 1` as a feature (grep-able, independent)
- [x] Explain implicit timer reset (TON auto-resets when rung goes false)
- [x] Note `IsLarge` latch crossing states ("latches outlive rungs")
- [x] Add Mermaid state diagram
- [x] Add "Heads up" box: SQO/SQI/SQL, DRUM, SFC

### L8 — Branches and OR Logic
- [ ] State actual `|` vs `any_of` rule (precedence + arity, not count)
- [ ] Name the gate pattern (master condition on parent rung)
- [ ] Promote "all conditions evaluate before any instructions" to Key Concept
- [ ] Show seal-in as a branch (contrast with L3 latch/reset)
- [ ] Clarify `AutoDivert` connection (one-line forward ref)
- [x] Add "Heads up" box: BST/BND, MCR, seal-in

### L9 — Structured Tags and Blocks
- [ ] Land L2 payoff: doubled name is gone, explain why
- [ ] Flag PLC arrays are 1-indexed — loudly
- [ ] Explain `.select(start, end)` inclusive semantics vs Python slice
- [ ] Show singleton vs counted UDT naming
- [ ] Mention: `always_number`, `Field()`, `auto()`, `@named_array`, `stride`, `.clone()`, `.map_to()`, `.slot()`
- [ ] Fix TitleCase inconsistency in UDT field names
- [ ] Address rung duplication (feature, not smell)
- [ ] Clarify build-time vs runtime loops
- [ ] Name the shift register pattern (`blockcopy` over `select`)
- [x] Add "Heads up" box: UDT/STRUCT, COP/BSL/BSR/FILL

### L10 — Testing
- [ ] Pick one title (sidebar vs body)
- [ ] Open with "If you know pytest, you already know how to test pyrung"
- [ ] Cash in FIXED_STEP from L5 explicitly
- [ ] Promote `fork()` — lead feature, "impossible on real hardware"
- [ ] Promote `history[-N]` — "also impossible on real hardware"
- [ ] Add 3-tier signal-driving table (`.value` / `add_force` / `remove_force`)
- [ ] Add force safety warning — real PLCs gate forces behind confirmation dialogs with injury/death disclaimers (Codesys, Rockwell). Forces override the program's control of physical outputs and bypass safety interlocks. Teach respect for the tool, not just the API
- [ ] Show `pytest.mark.parametrize` as complement to `fork()`
- [ ] Add fixture isolation one-liner
- [ ] Name canonical `with runner.active()` pattern
- [x] Add "Heads up" box: force I/O, fork/FIXED_STEP have no vendor equivalent

### L11 — From Simulation to Hardware
- [ ] Move celebration paragraph to the top
- [ ] Add E-stop discussion: `StopBtn` (control) vs `EstopOK` (permission)
- [ ] Add AutomationDirect-style disclaimer
- [ ] Add decision matrix for 3 deployment options (Modbus / Click codegen / CircuitPy)
- [ ] Expand Option A: 4 Modbus use cases + protocol caveat
- [ ] Expand Option B: `mapping.validate()` callout + "what doesn't port" list
- [ ] Expand Option C: celebrate the transpiler ("same source, two runtimes")
- [ ] Mention mixed deployments
- [ ] Add "hardware will surprise you" callout
- [ ] Add "Where to go from here" as story (built -> extend -> broader PLC -> deeper pyrung)
- [ ] Close with full Zen of Ladder mapping table
- [ ] Add exercise (run `mapping.validate()`, fix a complaint)

## Cross-Cutting
- [ ] Unify "order matters" thread across L1->L4->L8 with explicit callbacks
- [x] Add `pyrung.zen` Easter egg (prints Zen of Ladder a la `import this`)
- [ ] Add one-line cross-lesson callbacks: L1->L8 (last rung wins), L4->L5/6/7 (`rise()`), L2->L9 (doubled name), L3->L11 (`~StopBtn`->`EstopOK`), L5->L10 (FIXED_STEP)
