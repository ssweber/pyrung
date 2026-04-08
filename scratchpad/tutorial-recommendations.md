# pyrung Learn: Recommendations

Review of <https://ssweber.github.io/pyrung/learn/> and lesson pages.

## Fix first (blocking bugs)

Action these before anything else in this document — they're the only items that break copy-paste.

- **L8, first code block:** uses `out(Light)` and `latch(Running)` before `Light` is defined (`Light = Bool("Light")` appears in the *next* block), and `Mode = Int("Mode")` is declared mid-rung inside `with Program()`. Copy-pasters hit `NameError`. Consolidate declarations at the top of a single example, or split into two complete runnable snippets.
- **L7:** the `COUNTING` state is misnamed — it doesn't count anything, it cleans up and returns to idle. Rename to `RESETTING` or `CLEANUP`.
- **L2:** the Python instinct block is a strawman — `conveyor_speed = 0 # who cares?` isn't what a modern Python dev writes. Show `conveyor_speed: int = 0` and then pivot to "still doesn't tell you 16-bit signed, non-retentive, mapped to physical memory."

## Overall

- Voice is strong and consistent; keep it.
- "Python instinct → Why? → The ladder logic way → Try it → Key concept → Exercise" scaffold is great — don't break it.
- "pyrung won't let you cheat" is the best sentence in the docs; echo it wherever guardrails come up.
- **Every lesson should end with an ASCII ladder diagram** of the rung being taught — cheap, high payoff, anchors Python to the visual vocabulary. Lesson 1 has one and it's great; the pattern should repeat. (I will not re-note this per-lesson below.)
- **Exercises trend strong but uneven.** Lessons 3, 5, 6, 7, 8, 10 have excellent adversarial variants ("here's a rung that looks right but has a bug"); Lessons 1, 2, 4 are uniformly passive ("add a tag, write a rung, test it"). Bringing the early exercises up to the late standard would raise the perceived rigor of the whole guide.
- **The active-context pattern (`with runner.active()`) is inconsistent across lessons.** Lessons 5 and 7 split it confusingly; Lesson 10 finally uses it canonically. Either retro-fit the earlier lessons or have Lesson 2 (the first lesson that uses it) name the canonical pattern explicitly: active context for tag I/O, outside the context for `step()`/`run()`.
- **Title consistency.** "Testing Like You Mean It" vs "Testing" in the sidebar — pick one. Same for any other lesson where the body title differs from the sidebar.

## Landing page (`/learn/`)

- Lead with the guardrails idea; the "pyrung won't let you cheat" paragraph should come first.
- Surface the pedagogical move ("Python instinct, then the ladder way") as a bullet under "What you're building."
- Lesson title consistency: either give them all personality or rename "Testing Like You Mean It" to just "Testing" (sidebar already does).
- Lesson 11 teaser: "Map your project to a real Click PLC or P1AM-200" beats the current phrasing.
- Prerequisites footnote: "Code samples use PLC-style TitleCase for tag names — more on that in Lesson 2."

## Naming convention callout

Place in Lesson 2, right after the first `ConveyorSpeed`/`SpeedLimit` code block. Keep as an admonition, not a section.

> **A note on naming:** Tag names in this guide use `TitleCase` (e.g. `ConveyorRunning`), not Python's `snake_case`. Two reasons:
>
> 1. **It matches PLC convention** — what you'll see in Click, Do-More, Rockwell, and Productivity projects.
> 2. **Characters are a budget.** Do-More caps tag names at 16, Click at 24, Rockwell at 40. `EStopPressed` fits on a Do-More; `e_stop_pressed` doesn't.
>
> On flat-namespace PLCs like Click, underscores do a different job: they group related tags into a pseudo-namespace (`Bin1_Count`, `Bin1_Full`) that becomes a real UDT member (`Bin1.Count`) on platforms with structures. More in [Structured Tags and Blocks](../structured-tags/).

Supporting content beneath the admonition:

| PLC | Tag name limit | Notes |
|---|---|---|
| Do-More | 16 | Alphanumeric + single underscore |
| Click | 24 | Flat namespace; underscore as pseudo-scope |
| Rockwell Logix | 40 | Applies to *every* identifier; no double underscores |
| Productivity | 40+ | Generous |

- **TitleCase within a name**, **underscore as namespace separator** on flat-namespace PLCs, **dot as real member accessor** on Rockwell/Productivity. `Bin1_Count` on Click collapses to `Bin1.Count` on Logix — same source, two faithful emissions.
- On flat-namespace PLCs the namespace prefix must come *first* and be consistent (`Conveyor_Run` groups; `Run_Conveyor` doesn't).

## Lesson 1: The Scan Cycle

- "Last one wins" is a landmine disguised as a footnote; promote it to a callout with a concrete example.

> **Heads up — elsewhere:** `out()` is usually called `OUT` or `OTE`. A rung condition `X` is a "normally open contact" or `XIC`. `~X` is a "normally closed contact" or `XIO`. If you Google any of those, you'll find the same thing in a different dialect.

## Lesson 2: Tags

- Fix the Python instinct strawman (see Fix first).
- Acknowledge the doubled name string (`ConveyorSpeed = Int("ConveyorSpeed")`): "the Python variable is how *you* reference the tag; the string is the tag's identity in PLC memory — it's what HMIs and tag exports see. They're allowed to differ; matching them avoids confusion. This duplication goes away in UDT members in Lesson 9, where the member name *is* the string."
- Promote **retentive vs. non-retentive** to its own subheading — no Python analog, deserves the spotlight.

> **Heads up — elsewhere:** `Bool` tags are called control relays, `C` bits, `X`/`Y` for I/O, or just `BOOL`. `Int` is a 16-bit signed type almost everywhere (`DS`, `V`, `INT`). `Real` is a 32-bit float (`DF`, `R`, `REAL`). "Retentive" is universal — it's the tag's ability to survive a power cycle or STOP→RUN transition.

## Lesson 3: Latch and Reset

This lesson carries the single most valuable rename in the whole guide. Three distinct items, not one.

### 1. Rename `Estop` → `StopBtn` throughout the early lessons, use it as `~StopBtn`

Two improvements in one move:

1. **The name stops carrying safety expectations** the example can't deliver. `Estop` implies safety-rated devices on independent circuits, dual-channel monitoring, sometimes hardwired around the PLC entirely — and the lesson can't deliver any of that without a multi-page detour every time the tag appears. `StopBtn` is honest about what it actually is: a momentary stop button handled in software.
2. **The `~StopBtn` convention teaches good wiring practice from day one without the safety overlay.** Real stop buttons on real machines are *typically wired normally-closed* even when they're not safety-rated, because NC fails safe on wire breaks: a loose connector or chewed cable reads as "stop pressed" instead of silently leaving the machine running. This isn't a safety concept — it's wiring discipline every electrician follows. By writing `~StopBtn` from Lesson 3 onward, the learner internalizes the NC habit eight lessons before they meet a real E-stop, so by Lesson 11 the wiring direction is muscle memory and the *only* new content is the governance story.

Rewritten example. `latch` is sticky and requires an explicit reset rung:

```python
StartBtn = Bool("StartBtn")    # NO momentary contact
StopBtn  = Bool("StopBtn")     # NC contact: conductive at rest
Running  = Bool("Running")

with Program() as logic:
    with Rung(StartBtn, ~StopBtn):
        latch(Running)
    with Rung(~StopBtn):
        reset(Running)
```

The first rung says "if Start is pressed AND the stop circuit is healthy, latch Running." The second says "if the stop circuit is broken (button pressed, wire cut, power lost), reset Running." `~StopBtn` appears in both rungs because both care about the *stop-asserted* condition for opposite reasons: interlock on the latch rung, trigger on the reset rung.

Make the callback to Lesson 1 explicit: "Remember — last rung wins. Here you're using that on purpose. Safety wants the last word."

### 2. Teach `~` as "NC contact," not "NOT"

This is the conceptual bridge that turns ladder from mysterious to obvious for a Python dev. Its own callout:

> **What `~` actually means.**
>
> Your Python instinct reads `~StopBtn` as "not StopBtn" — a Boolean inversion of a value. That's not what it is. In ladder logic, `~` declares the **contact type**. It says: *this contact is normally-closed — conductive in its resting state — and the rung resolves TRUE when that conductive path is broken.* In a real ladder editor, `~` is drawn as `|/|` (NC), versus `| |` for NO. Two different symbols, two different physical contact types, not "X" and "not X."
>
> Why does this matter? Because the ladder reading composes naturally with how real devices are wired:
>
> - **Stop buttons** are NC so wire breaks fail safe → `~StopBtn`
> - **Door interlocks** are NC so a hanging open door stops the machine → `~DoorClosed`
> - **Motor overload contacts** are NC so a tripped overload registers → `~OverloadOK`
> - **Level sensors** are often NC so a floating-out cable stops the pump → `~TankLow`
>
> Every NC sensor on a real machine reads with a `~` in the rung not because they're "alarmed" but because they're *physically wired* as normally-closed devices. Once you read `~` as "NC contact" instead of "not," ladder rungs start reading like wiring diagrams. Which is what they are.

### 3. Land the latch-vs-`out` distinction harder

The whole reason Lesson 3 exists is that `latch` is *sticky* and `out` is *not* — and a Python dev's first instinct is to read them as the same thing. They're not. If `latch` worked like `out`, the entire start/stop pattern would collapse to `with Rung(StartBtn): out(Running)` and the conveyor would stop the moment your finger left the button — which is *exactly* the bug Lesson 1 set up at the end.

> **Why two rungs instead of one?**
> Your Python instinct says "the rung went false, the output should drop." That's how `out` works. But `latch` isn't `out`. `latch` sets the bit and *leaves it set* — that's the whole point. The latch rung above only fires *once*, when Start is pressed. After that, `Running` stays true on its own. To make it false again, you need a *separate* rung that explicitly clears it with `reset()`. That's why this lesson has two rungs: one to start, one to stop. If you only had the first rung, the motor would stop the instant you released Start, which is exactly the bug Lesson 1 ended on.

Any reader who collapses `latch` and `out` is showing the lesson missed its mark.

### Smaller things

- **Forward ref to Lesson 8 seal-in (one line):** "You'll see this pattern again in Lesson 8 as a *seal-in rung* — same behavior, single rung, self-holding via a feedback branch. We use latch/reset here because two named operations are easier to step through."
- **Forward ref to Lesson 11 E-stop (one line):** "By Lesson 11 you'll meet `EstopOK` — same NC wiring, different governance story."
- `comment()` framing ("not a `#` comment — it's rung metadata that travels with the program") is perfect; reuse this framing elsewhere for "X looks like a Python thing but it's a PLC artifact."

> **Heads up — elsewhere:** `latch` is called `SET`, `OTL`, or `S`; `reset` is `RST`, `OTU`, or `R`. Seal-in rungs look the same in every ladder editor — Start OR-branched with Running, ANDed with the stop contact. `comment()` is just a rung comment.

## Lesson 4: Assignment

- `rise()` is buried — give edge detection its own mini-section with a concrete scenario (sensor held True 100 scans → counter increments once). Biggest conceptual jump from Python; it unlocks one-shots, pulses, debounce.
- Instruction-order-within-a-rung is implicit in the exercise — name the rule before the exercise uses it: "instructions within a rung execute top-to-bottom."
- `copy` clamps / `calc` wraps is gold — add one line on *why* to reach for each: clamping for data movement (don't silently roll over a sensor), wrapping for counters/accumulators (matches PLC arithmetic hardware).
- Argument order (`copy(source, dest)`): say it once and note vendors vary — Rockwell MOV is the same direction, Click reads destination-first in its editor.
- Naming nit: `LastSize` vs `PreviousSize` is ambiguous (both mean "the one before"); use `CurrentSize`/`PreviousSize` or `LastSize`/`PriorSize`.
- Unconditional rung placement — one sentence tying back to "last rung wins" and forward to state-machine rung discipline.
- "Python would `sleep`. A PLC can't sleep." is a perfect bridge — keep it.

> **Heads up — elsewhere:** `copy` is `MOV`, `COP`, or `MOVE`. `calc` is `MATH` or `CPT` (or an expression in Structured Text). `rise()` and `fall()` are "leading-edge" / "trailing-edge" contacts, one-shots (`ONS`/`OSR`), positive/negative differentials (`PD`/`ND`), or `R_TRIG`/`F_TRIG`. An unconditional rung is "always on" — some PLCs expose a special bit (`SP1`, `S:1/15`), others just wire straight from the rail.

## Lesson 5: Timers

- "This is why pyrung exists" lands — sharpen it by naming the alternative: "In pytest you'd reach for `freezegun` or monkeypatch `time.time`. pyrung bakes determinism in because PLC time *is* the scan clock."
- Name the timer family in one sentence: on-delay (TON), retentive on-delay (RTON), off-delay (TOF). Each has different reset behavior. Even better — pyrung encodes RTON elegantly: `on_delay(...)` is TON, but **chain `.reset(X)` and it becomes RTON**. Same instruction, mode determined by the fluent chain:

  ```python
  # TON — auto-resets when rung goes False
  on_delay(HoldDone, HoldAcc, preset=2000, unit=Tms)

  # RTON — same instruction, but holds accumulator across rung-false;
  # only the explicit reset clears it
  on_delay(BatchDone, BatchAcc, preset=3600, unit=Ts) \
      .reset(BatchReset)
  ```

  Pre-introduces the chained-builder pattern that returns in counters.
- **Terminal chains are an honesty feature, not a limitation** — worth its own callout. In Click and most ladder editors, the reset input on a counter or RTON is just another wire powered from the rail with independent conditions. That flexibility is also what makes Click rungs hard to read at a glance. pyrung's terminal `.reset(...)` encodes that logical independence in syntax:

  > **Why is `.reset()` terminal?**
  > In Click and most ladder editors, the reset input on a counter or retentive timer is its own wire — you can power it from the rail with completely independent conditions. That flexibility makes rungs hard to read: reset logic *looks* tied to the main rung when it isn't. pyrung makes the chain terminal so the syntax matches the semantics. Conditions inside `.reset(...)` belong to the reset, not the rung. If you need to chain more instructions after, write a separate rung.

- Explain the accumulator tag: "timers have state — a done bit and an accumulator. Real PLCs bundle these as a structured type (`TIMER` in Rockwell, `T1.ACC`/`T1.DN` in Click). pyrung makes them explicit tags so you can inspect and test them." Quietly foreshadows Structured Tags.
- Time base choice (`Tms` vs `Ts`) deserves a footnote: real PLCs pick a time base, and it affects resolution + max range on a 16-bit accumulator. Hardware constraint, not a pyrung quirk.
- `Tms`/`Ts`/`Tm`/`Th`/`Td` is a deliberate naming choice worth a sidebar:

  > **Why `Tms` and not `Milliseconds`?**
  > Time units in pyrung are 2–3 characters: `Tms`, `Ts`, `Tm`, `Th`, `Td`. The `T` prefix mirrors Codesys/IEC `T#2s500ms` literals, the short form fits Do-More's 16-character tag budget, and it sidesteps the `Min` ambiguity (minute vs minimum, plus shadowing Python's `min()`). The same convention works as a tag-name suffix: `HeatTs`, `MotorTms`, `IdleTm` — the unit travels with the tag.

- ⚠️ **Naming collision worth flagging:** Click uses `TD` as the timer-data (accumulator) prefix; pyrung uses `Td` as the day time-base unit. Case differs, contexts differ, no conflict in practice — but worth a footnote. Same shape for counters: Click `CT1` is the counter done bit, `CTD1` the accumulator.

> **Heads up — elsewhere:** on-delay is `TON` or `TMR`; off-delay is `TOF`; retentive on-delay is `RTO` or `TMRA`; pulse is `TP` (or a hand-rolled one-shot). Time bases are usually plain units (`ms`, `s`) or a fixed resolution baked into the instruction. The done bit is `.DN`, `.Done`, or `.Q`; the accumulator is `.ACC`, `.Acc`, or `.ET`.

## Lesson 6: Counters

- **"No `for` loop. Count the edges."** is a perfect Python-instinct hook — keep it.
- **The "chip with multiple input pins" framing is gold and underused.** The `.reset(CountReset)` explanation is the best paragraph in any lesson so far. Promote it: this mental model isn't just for counters, it's how *every* box instruction in real PLCs works — multi-input, multi-output blocks (timers, counters, PID, FIFO, message blocks, motion). Hoist into a Key Concept callout: "PLC instruction blocks are chips, not function calls. They have a power input (the rung), but other pins — reset, preset, enable — are separate wires hooked to their own conditions. The chained `.reset(...)` method is pyrung's way of drawing those extra wires." Ties back to RTON in Lesson 5.
- **The headline concept should be "counters count every scan, not edges."** Currently buried. Lead with it: "A counter is a pure accumulator. While its rung is True, it adds 1 every scan — not every edge. On a real sensor held True for 100 scans, the counter would rack up 100 counts off a single box. Wrap with `rise()` to count edges instead. You'll do this 95% of the time on sensor inputs."
- **Counters mirror timers.** Both chain `.reset()` — same fluent-builder pattern, same chip-with-pins model. State the parallel.
- **Show bidirectional counters as a bonus.** pyrung's CTUD is a `count_up` with a `.down()` link, not a separate instruction:

  ```python
  count_up(NetCountDone, NetCountAcc, preset=50) \
      .down(BoxLeavesSensor) \
      .reset(NetCountReset)
  ```

  Use case: "boxes entering minus boxes leaving = boxes currently in zone." Real, intuitive, shows chains composing.
- **Terminal-chain caveat.** `count_up(...).reset(...)` is terminal for the same honesty reason as RTON — reference the Lesson 5 callout instead of repeating it.
- **Why `Dint`, not `Int`?** A 16-bit `Int` rolls over at 32,767 — on a fast line that's a few hours of production. One line: "Counters use `Dint` (32-bit) because a 16-bit accumulator rolls over at 32,767 — faster than you'd think on a real line. Production counters in real PLCs are almost always 32-bit for the same reason."
- **Meta-irony worth naming.** The test uses `for _ in range(3): BinASensor.value = True; step(); ...` — Python loops in the *test* simulating physical events, while the *logic* has no loops at all. "You use Python where Python belongs (driving the simulation, asserting state), and ladder logic where ladder belongs (the actual control). The boundary is the runner."

> **Heads up — elsewhere:** Counters are `CTU`/`CTD`/`CTUD`, or just `CNT` with a direction flag. Done bits and accumulators look like timers — `.DN`/`.Done`/`.Q` and `.ACC`/`.Acc`/`.CV`. Reset is its own input (`RES`, `RST`, or an `R` pin on the block). Edge-counting is always "one-shot feeding the counter" — never the counter itself.

## Lesson 7: State Machines

- **Magic numbers are the elephant in the room** — the answer isn't Python `Enum`, it's the **tag-as-constant** pattern. Define an `Int` tag with an `initial=` value and never write to it again. State values live in the PLC's tag table, visible to anyone who opens the project — *better* documentation than a Python comment because it travels with the project file:

  ```python
  # State values as tag-constants — initialized once, never written
  IDLE      = Int("IDLE",      initial=0)
  DETECTING = Int("DETECTING", initial=1)
  SORTING   = Int("SORTING",   initial=2)
  RESETTING = Int("RESETTING", initial=3)

  State = Int("State", initial=0)

  with Rung(State == IDLE, rise(EntrySensor)):
      copy(DETECTING, State)
  ```

  This is *the* moment to land *"Name the purpose, not the part."* A Python dev's instinct is `Enum`; the lesson should redirect to "constants are tags here, and that's a feature."
- The lesson can acknowledge that bare numbers (`State == 0`) are common in real PLC code — they're not *wrong*, just hostile to readers. Show both, say which ages better.
- **Name "PackML" out loud.** You don't have to teach PackML, but name-drop it: "this is a 4-state hand-rolled machine. Industrial packaging standardizes on PackML, which defines ~17 states (Aborted, Stopped, Idle, Starting, Execute, Completing, Complete, Resetting, Held, Suspended, etc.) so any operator from any vendor recognizes the lifecycle. We're keeping it simple here, but PackML is what real production lines look like." Gives learners a search term and a real-world target.
- **Callback `rise()` from Lesson 4.** The `rise(EntrySensor)` in the IDLE→DETECTING transition is load-bearing — without it you'd transition every scan the sensor sees a box. Make the callback explicit: "Remember `rise()` from Lesson 4? This is *why* it matters."
- **The repeated `State == 1` is a feature, name it.** In Python you'd write `if state == DETECTING:` once and nest three statements inside. In ladder, each rung stands alone, so the condition repeats. That looks like a code smell — but it's the *point*: any rung can be deleted, edited, moved, or commented out without affecting any other. Each rung is independently grep-able by state. "The maintenance tech at 3am is searching for `State == 1`, and every rung that participates in DETECTING shows up in the results."
- **Implicit timer reset is subtle and unexplained.** The transition rung `with Rung(DetDone): copy(2, State)` doesn't reset `DetDone` or `DetAcc`. It works because once `State` changes to 2, the `State == 1` rung goes false, which auto-resets the TON. Add one sentence: "Notice we don't manually reset `DetDone`. Once `State` leaves 1, the `on_delay` rung goes false and the TON auto-resets — `DetDone` clears with it. The transition fires once."
- **`IsLarge` latch crossing states is a great implicit lesson.** It's set in DETECTING, used in SORTING, reset in RESETTING. "Latches outlive rungs — they're how a state machine remembers things between states without globals or context objects."
- **ASCII state diagram, please.** 4 states is small enough to draw inline and state machines benefit from visualization more than any other pattern:

  ```
  [IDLE] --rise(Entry)--> [DETECTING] --DetDone--> [SORTING] --HoldDone--> [RESETTING] --> [IDLE]
                              |
                              v (size > threshold)
                          latch IsLarge
  ```

- **DAP debugger callout at the end is well-placed.** Keep it; maybe strengthen: "from this lesson on, stepping through scans is faster than reading assertions."

> **Heads up — elsewhere:** State machines are almost always hand-rolled using an Int tag plus comparison contacts, or built on a dedicated sequencer instruction (`SQO`/`SQI`/`SQL`, `DRUM`). IEC 61131-3 has Sequential Function Chart (SFC) as a first-class language for this — if your PLC supports it, use it. PackML is usually an AOI library you drop in, not a language feature.

## Lesson 8: Branches and OR Logic

- **Fix the NameError** (see Fix first).
- **`|` vs `any_of` framing is incomplete.** The lesson says "use `any_of` when you have more than two conditions" but the real rule is **ergonomics and arity**, not type. `|` works on any condition — Bool tags *and* comparisons — but Python's operator precedence puts `|` higher than `==`/`<`/`>`, so you need parens around comparisons: `(Mode == 1) | (Mode == 3)`. `any_of` is variadic and reads cleaner once you have three terms or a mix of types. State the actual rule: "`|` is binary; comparisons need parens because `|` binds tighter than `==`. `any_of` is variadic and skips the paren noise. Reach for `any_of` when the parens get loud or you have more than two terms." Same shape for `&` vs `all_of`. Mirrors a familiar Python pattern: `a or b or c` vs `any([a, b, c])`.
- **Lesson 3 callback (one line):** "Same `~StopBtn` convention — the gate is open while the stop circuit is healthy."
- **The E-stop-as-gate pattern is canonical and deserves naming.** Putting a master condition on the parent rung and gating all branches under it is *the* textbook ladder pattern for any permission or interlock — guard doors, light curtains, machine-enabled flags. Name it: "this is the **gate pattern**. The parent rung holds your master condition, and every branch inside inherits that permission automatically. Lose the gate, lose all the outputs — atomically, in one scan." Real fail-safe E-stop wiring lives in Lesson 11; here, the gate pattern is general-purpose.
- **"All conditions evaluate before any instructions execute" deserves a Key Concept callout, not a one-line note.** This is the **atomic rung** property and it's load-bearing for the whole mental model: every rung is a snapshot of the world, evaluated then acted on as a unit. Ties back to Lesson 1's scan cycle and forward to Testing.
- **"One coil, one rung" gets cited explicitly.** The lesson finally names the rule against double-`out` and ties it to "last rung wins" — perfect. Cite the Zen line by name. This is the lesson where that principle cashes in.
- **Seal-in is a branch.** The Lesson 3 forward reference cashes in here because **seal-in IS a branch**. The classic `(Start | Running) & ~Stop` rung is literally Start branched in parallel with Running, ANDed with NOT-Stop. Show it contrasted with `latch`/`reset`:

  ```python
  # Latch/reset version (Lesson 3)
  with Rung(StartBtn):
      latch(Running)
  with Rung(StopBtn):
      reset(Running)

  # Seal-in version — same behavior, classic ladder shape
  with Rung(~StopBtn):
      with branch(StartBtn):
          pass
      with branch(Running):
          pass
      out(Running)
  ```

  Then say which to reach for: latch/reset is clearer for two-button start/stop; seal-in is what you'll see in textbooks and every legacy ladder you ever inherit. Both are valid.
- **`AutoDivert` connection.** Described as "set by state machine in auto mode" but the connection isn't shown. One-line forward reference: "`AutoDivert` is latched and reset by the state machine from Lesson 7 — this rung is the consumer."
- **Exercise is excellent.** The Auto→Manual mid-run handoff test is exactly the kind of mode-transition bug that's hard to catch on real hardware and easy in pyrung.

> **Heads up — elsewhere:** OR is a parallel branch of contacts; AND is contacts in series. `branch()` is "parallel branch" everywhere (sometimes with explicit `BST`/`BND` markers). The safety-gate pattern is "Master Control Reset" (`MCR`) in some editors, "master control" in others. Seal-in is the classic OR-branch with a series stop contact — every legacy ladder opens with one.

## Lesson 9: Structured Tags and Blocks

The current lesson surfaces only a fraction of what the Tag Structures guide offers. Several features deserve to be at least mentioned in the lesson body, with the guide as the deep dive.

### The Lesson 2 payoff

Lesson 2's doubled-name string is gone — and the explanation is actually deeper than "flattening." **pyrung's UDT model *is* the flat-name model with structured access on top.** UDT field names *are* the tag names, and pyrung generates flat underscore-separated identities (`Bin1_sensor`, `Bin1_acc`) under the dot-access syntax. So `Bin[1].sensor` is the *Python access path* to a tag whose real identity is `Bin1_sensor`. On Rockwell-style targets, the same structure can emit a real UDT member (`Bin[1].Sensor`). Same source, two faithful emissions — not "flattening."

Make it one explicit bullet, not three: "Remember the doubled name from Lesson 2? It's gone because there's no second name to maintain. pyrung generates the flat identity from the structure. On Click that's `Bin1_sensor`; on Rockwell it's a real UDT member. Your Python stays the same."

### Indexing

- **PLC arrays default to 1-indexed — flag it loudly.** `Bin[1]`, `SortLog[1]`, no `[0]`:

  > **PLC arrays start at 1.** `Bin[1]`, not `Bin[0]`. Every PLC vendor in the world is 1-indexed and pyrung honors that because the tag table you generate has to match the PLC's. Your Python instinct will betray you here exactly once.

- **0-based is opt-in.** One mention so it's discoverable: "If you specifically need 0-based addressing (matching a 0-based hardware range or porting code), Blocks accept a 0-based start. Default is 1, override when you mean it."
- **`.select(start, end)` is a deliberate design choice.** Same "honesty feature" pattern as terminal `.reset()`:

  > **Why `.select(1, 5)` instead of `[1:5]`?**
  > Python's `list[1:5]` is `[1, 2, 3, 4]` — exclusive end. PLC ranges like `DS1..DS5` are inclusive on both ends — `[1, 2, 3, 4, 5]`. Reusing `[]` for ranges would silently do the wrong thing exactly half the time. `.select(start, end)` is visibly different because the semantics are different. Both bounds are inclusive, every time.

### Features the lesson should at least name

- **Singleton UDTs get compact names automatically.** `@udt() class Motor: ...` produces `Motor_running`, `Motor_speed` — no instance number. With `count > 1` you get `Pump1_running`, etc.:

  ```python
  @udt()                       # singleton — Motor_running, Motor_speed
  class Motor:
      Running: Bool
      Speed: Int

  @udt(count=3)                # Pump1_Running, Pump2_Running, Pump3_Running
  class Pump:
      Running: Bool
      Flow: Real
  ```

- **`always_number=True` for consistency.** "If your naming convention wants `Motor1_running` even when there's only one (so future expansion doesn't rename everything), pass `always_number=True`."
- **`Field()` for retentive overrides and defaults.** "Want a UDT field that survives STOP→RUN even though the type is non-retentive by default? `id: Int = Field(default=100, retentive=True)`."
- **`auto()` for per-instance sequences.** `id: Int = auto(start=10, step=5)` gives `Alarm[1].id=10`, `Alarm[2].id=15`. Use case is real (alarm IDs, modbus addresses, channel numbers).
- **`@named_array` for same-typed structures.** Decision rule:
  - **UDT**: mixed types (`sensor: Bool`, `acc: Dint`, `setpoint: Real`) — like a struct
  - **named_array**: all same type — like a typed array of records
- **`stride` and gaps.** Important for hardware mapping. "If your structure needs to occupy fixed-width slots in hardware (because the device dictates the layout), `stride` lets you reserve gap slots."
- **`.clone()` for templating.** `Pump = Motor.clone("Pump")`, `Fan = Motor.clone("Fan", count=4)`. DRY pattern for repeated equipment types.
- **`.map_to(ds.select(...))` is the codegen story made tactile** and probably belongs as the closing note: "`Channel.map_to(ds.select(101, 106))` says 'these three Channel instances live at DS101–DS106.' The structure stays the same; only the hardware mapping changes per target." Forward reference to Lesson 11.
- **Block slot configuration with `.slot()`.** Without it, you're stuck with `DS1`, `DS2`, `DS3` — exactly the "magic numbers" problem from State Machines:

  ```python
  ds = Block("DS", TagType.INT, 1, 100)
  ds.slot(1, name="SpeedCommand")
  ds.slot(2, name="SpeedFeedback")
  ds.slot(10, retentive=True, default=999)
  ds.slot(20, 30, retentive=True)        # Range config
  ```

### Other things to fix or surface

- **TitleCase vs lowercase in UDT fields.** The lesson uses lowercase (`sensor`, `done`, `acc`) but every prior lesson established TitleCase. Real Rockwell UDTs use TitleCase members, and case matters for the generated tag table. My recommendation: TitleCase, no exception.
- **The `Bin[1]` / `Bin[2]` rung duplication wants to be addressed.** Second moment to cite *"If you need a FOR loop… no you don't."* Each rung is independently editable, grep-able, and visible. When one bin needs a different preset, you edit one rung — you don't fight a loop.
- **Build-time vs runtime loops.** If pyrung supports a build-time `for i in (1, 2): with Rung(...): ...` that emits N distinct rungs at program-construction time, show it once with the warning that duplicated rungs are usually more readable. If it doesn't (or actively forbids it), say so — Python devs *will* try.
- **`Bin[1].full` driven by an `out` rung mirroring `Bin[1].done` is confusing.** If `full` is just renamed `done`, why have both? If it combines additional conditions, say so.
- **Name the shift register pattern.** `blockcopy` over `select` is the canonical FIFO/shift-register pattern, with dedicated instructions on every PLC platform (BSL/BSR on Rockwell, SHIFT on Click and Do-More). Worth naming so learners have a search term.

> **Heads up — elsewhere:** Structured tags (UDTs, `STRUCT`s, "Structures") are available on higher-end PLCs. Flat-namespace PLCs fake it with underscore prefixes — exactly what pyrung generates as the flat identity. Arrays of structures are "UDT arrays" or "structure arrays." Typed memory ranges are addressed directly (`DS`, `V`, `DINT`) or aliased with nicknames. Block-copy, shift-register, and fill all have dedicated instructions — `COP`/`COPY`, `BSL`/`BSR`/`SHIFT`, `FAL`/`FILL`. "Retentive" is a per-tag flag or a memory range, depending on the platform.

## Lesson 10: Testing (Like You Mean It)

This lesson is the climactic payoff for everything since Lesson 5 — and it lands most of it, but several of pyrung's most distinctive features are presented as features instead of *superpowers*. They're not just "things pytest doesn't have" — they're things **physical hardware can't do**, and that framing turns each one from a bullet point into a sales pitch.

### The framing the lesson is missing

- Pick a title and stick with it (sidebar vs body).
- **Cite the Zen line.** *"Test it."* is right there, waiting to be the epigraph.
- **"This is why pyrung exists" cashes in here, not just Lesson 5.** "Remember the FIXED_STEP determinism from Lesson 5? This is what it was for. Every test in this lesson runs in deterministic, reproducible scan time. No flaky tests, no timing race conditions, no 'works on my machine.' One scan, one tick, every time."
- **"If you know pytest, you already know how to test pyrung."** Deserves to be the opening line. No `plc-test` framework to learn, no proprietary test runner, no XML config. Standard pytest fixtures and asserts. Huge lowering of the learning curve.
- **Name the canonical active-context pattern here.** This lesson is where it finally lands cleanly: `with runner.active()` for tag I/O, outside the context for `step()`/`run()`. Earlier lessons should match or add a forward link.

### Features that need to be sold harder

- **`fork()` is the headline feature and the lesson buries it as the third subsection.** Forking simulation state is **impossible on real hardware.** Loudest callout in the entire guide:

  > **`fork()` does something physical hardware can't.** You can't fork a real conveyor. You can't pause a real PLC, copy its state, and run two futures in parallel from the same instant. With pyrung you can. This is how you test "what if the part is large vs small," "what if the operator hits stop now vs in 100ms," "what if the network packet arrives before vs after the sensor edge." Two assertions from one starting point, no setup duplication, no flakiness.

  Promote to first or second subsection.

- **`history[-2]` is also impossible on real hardware.** Real PLC software has *trends* and *trace buffers*, but they're sampled and lossy. pyrung's history is **every scan, every tag, immutable, indexable.**

  ```python
  runner.run(cycles=100)
  with runner.active():
      assert JamAlarm.value is True
  # Why did the jam fire? Walk back through scans:
  for i in range(-1, -10, -1):
      snapshot = runner.history[i]
      print(f"scan {i}: State={snapshot[State]} EntrySensor={snapshot[EntrySensor]}")
  ```

  "This is post-mortem debugging. The alarm fired, you have the entire history of every tag for every scan, and you can walk backwards until you find the cause. Real PLCs can't do this."

- **The 3-tier signal-driving API needs a small table:**

  | Mechanism | Persistence | Use case |
  |---|---|---|
  | `tag.value = X` (inside `with runner.active()`) | one scan | Setting an initial value, simulating a one-shot input |
  | `runner.add_force(tag, X)` | persistent until removed | Holding a sensor on across many scans |
  | `runner.remove_force(tag)` | releases the force | Letting the logic see real value again |

  Tie forces back to real PLCs: forcing is a *real* debugging feature in Click, Rockwell, and Do-More — pyrung is mirroring an industrial concept. Click "force on/off," Rockwell "Force I/O" / `.FORCE` table, Do-More "force."

- **Coverage is the killer feature you didn't know to ask for.** Real PLC programmers have *no coverage tools*. None. If pyrung's instrumentation can produce "which rungs have been exercised by my tests" that is literally unprecedented for safety-critical PLC code — the kind of thing safety auditors will *care* about. Even if it's not implemented yet, the lesson should name it as a future direction. (Flag for the author to surface or to add.)

- **`pytest.mark.parametrize` is the missing complement to `fork()`.**
  - **`fork()`** — branch state *mid-test* from a shared dynamic starting point
  - **`parametrize`** — run the *whole* test N times with different starting conditions

  The conveyor sorting test is a natural fit:

  ```python
  @pytest.mark.parametrize("box_size,expected_diverter", [
      (50,  False),   # small
      (150, True),    # large
      (99,  False),   # boundary, just under
      (100, False),   # boundary, exactly at threshold
      (101, True),    # boundary, just over
  ])
  def test_box_classification(runner, box_size, expected_diverter):
      ...
  ```

  The kind of boundary testing that's *agonizing* on real hardware (load 5 specific test boxes, push them through by hand) and trivial in pyrung.

### Smaller things

- **Fixture isolation deserves a one-liner.** Default pytest scope is `function`, so every test gets a fresh `PLCRunner`. Worth one sentence so learners understand they're not accumulating state across tests.
- **The exercise is excellent.** Full lifecycle, large/small via fork, E-stop mid-sort. Don't change it.
- **`when tests aren't enough` → DAP debugger** is a great closing transition. Strengthen: "tests answer 'does this work?' The debugger answers 'why doesn't it?' Both matter. pyrung gives you both, in tools you already know."

> **Heads up — elsewhere:** `runner.step()` is "single scan mode" on most PLCs. `add_force`/`remove_force` mirror the universal Force On/Off / Force I/O features — forcing is a real debugging tool everywhere, not a pyrung invention. `history[-N]` is *sort of* like a trend or data log, except trends are sampled and lossy — pyrung's history is every scan, every tag, immutable, indexable. And then: **`fork()`, FIXED_STEP deterministic scan time, and rung coverage have no equivalent on real PLCs.** These capabilities simply don't exist in any vendor's tooling. pyrung isn't competing with PLC tooling here — it's offering things PLC programmers have never had.

## Lesson 11: From Simulation to Hardware

This is the climactic finale and it's *too short*. The lesson sells three deployment options as a brief list when it should be celebrating the moment everything you've built actually leaves the simulator.

### The framing the lesson is missing

- **The closing paragraph belongs at the *top*.** "You started with a button that turned on a motor. You ended with a tested, deployable conveyor sorting station…" That's the celebration line, buried at the bottom. Move it up — it's the payoff for 11 lessons and the learner deserves to feel it before the deployment options arrive.
- **Cite the Zen line.** *"The tech (maybe you) at 3am will thank you."* The learner is about to put their code on real hardware. The 3am tech is *them*. Epigraph.
- **The Lesson 10 testing payoff should land here explicitly.** "The tests you wrote in Lesson 10 are still your safety net. pyrung's simulation behavior matches its codegen output (within whatever the validator allows). The same assertions that proved your sorting logic in pytest will hold on the Click PLC. That's the bargain — you don't have to test on hardware because you already tested on the simulator."
- **Lesson 11 is where the real fail-safe E-stop discussion finally lives** — and because Lesson 3 taught NC wiring via `~StopBtn`, this lesson introduces *governance*, not wiring direction:

  > **`StopBtn` was the warm-up. Now meet the E-stop.**
  >
  > You've been writing `~StopBtn` since Lesson 3. That's the same NC wiring convention real stop buttons use — the bit is HIGH when healthy, LOW when pressed or broken. So you already know how fail-safe inputs read in code. Good. The wiring is the easy part.
  >
  > The hard part is **who owns the stop.** When you wired `StopBtn` to the PLC, the PLC was in charge: it read the bit, decided to call `reset(Running)`, and stopped the motor *as a software decision*. That works for a conveyor in the lab. It does *not* work on a machine that can hurt someone, because the PLC is not a safety device. PLC scan loops are not stop circuits. If your scan halts, your watchdog hangs, your firmware glitches, or your output transistor welds shut, the PLC's "decision" to stop the machine never reaches the actuator.
  >
  > A real E-stop fixes this by taking the PLC *out of the chain of command*. The red mushroom button wires to a dedicated **safety relay** (Pilz, Banner, ABB Jokab) rated to ISO 13849 / IEC 62061. The safety relay handles dual-channel monitoring, contact welding detection, and the actual stop circuit that drops power to dangerous outputs. The PLC reads the relay's permission contact as `EstopOK` and is *informed* — but not in charge. If the PLC dies, the safety relay still drops the contactor.
  >
  > For our example, we'll add `EstopOK` to the TagMap as a fail-safe NC input from a safety relay's permission contact, alongside the existing `StopBtn`:
  >
  > - **`StopBtn`** — operator says "please stop." PLC handles it in software. It's a control input.
  > - **`EstopOK`** — safety relay says "the world is OK to run." PLC obeys it as a gate. It's a permission input.
  >
  > Read this as a *demonstration* of the wiring pattern, not a safety design.

  Then show the TagMap with both `StopBtn: x[2]` and `EstopOK: x[3]` (both NC), and the rung gating outputs through `with Rung(EstopOK):` (no negation needed because the name encodes the polarity — `EstopOK` is TRUE when safety is satisfied). The naming difference does real work: `~StopBtn` reads as "stop is asserted," `EstopOK` reads as "safety is satisfied." Same NC wiring, opposite naming polarity, because they encode different *meanings*.

- **Add an AutomationDirect-style example-code disclaimer at the top of Lesson 11.** The disclaimer language belongs *here*, not on the landing page. AutomationDirect's own boilerplate is the right model — it lives in the same ecosystem your audience is targeting:

  > **About this example.**
  >
  > pyrung and the conveyor sorting station in this guide are provided as an educational example. Like AutomationDirect's own sample code policy, this is provided "as-is" with no expressed or implied warranty. If you adapt any of this code for a real application, **it is your responsibility to completely modify, integrate, and test it to ensure it meets all system and safety requirements for your intended use.**
  >
  > Like all general-purpose PLCs, the hardware targeted in this lesson is not fault-tolerant and is not designed, manufactured, or intended for use in hazardous environments requiring fail-safe performance — nuclear facilities, aircraft navigation, air traffic control, life support, or weapons systems — where failure could lead directly to death, personal injury, or severe environmental damage.
  >
  > Real installations must follow all applicable local and national codes (NEC, NFPA, NEMA, and the codes of your jurisdiction). pyrung verifies your *logic*; it cannot verify your wiring, your safety circuit, or your machine. Get a review from a qualified controls engineer before energizing anything that can move, heat, pinch, or otherwise hurt someone.
  >
  > The 3am tech is you. Be kind to them.

### The three deployment options aren't equal — explain that

The three options are three completely different deployment models. A decision matrix up front:

| Use case | Option | What runs where |
|---|---|---|
| Quick prototype, hook up to HMI/SCADA, demo to stakeholders | **A: Modbus runtime** | pyrung *is* the controller, running on a laptop or Pi, speaking Modbus to anything that wants to talk to it |
| Production deployment to a real PLC, integrate with existing plant equipment | **B: Click codegen** | pyrung translates your program into Click ladder CSVs; the Click PLC runs the ladder natively |
| Standalone embedded controller, no PLC software in the loop | **C: CircuitPython codegen** | pyrung transpiles to a CircuitPython file that runs the scan loop natively on a P1AM-200 microcontroller |

These are *philosophically different*:

- **A** says "pyrung is the brain. The hardware is just I/O." Good for prototyping, demos, lab work, hybrid systems.
- **B** says "pyrung translates faithfully to ladder." Good for production where the operations team wants a real PLC for support, training, and standardization.
- **C** says "pyrung transpiles to embedded Python." Good for cost-sensitive deployments, edge devices.

### Each option is undersold

- **Option A (Modbus) is described in three sentences and the most important thing isn't said.** "Your pyrung program runs on your laptop or a Pi or whatever, and exposes its tags as a Modbus TCP server. Anything that speaks Modbus — HMI software, SCADA, another PLC, ClickNick's Data View — can connect and read or write tags as if pyrung were a real Click PLC. You can wire a real button to a Modbus I/O block, point the I/O block at your laptop, and your laptop is the PLC."
- **Modbus has at least four distinct use cases** that the lesson collapses into one sentence. Break them out:
  1. **HMI integration during development** — connect a real HMI to your simulation, validate operator workflows before any hardware
  2. **Soft-PLC in production** — for non-safety-critical apps, pyrung *is* the runtime
  3. **Hybrid systems** — pyrung does the brain, a real PLC handles I/O via Modbus
  4. **Hardware-in-the-loop testing** — connect a real conveyor's sensors to a pyrung simulation that controls real outputs
- **Modbus protocol caveats deserve one line.** "Modbus is fine for development, monitoring, and HMIs. It's not a substitute for proper fieldbus protocols (EtherNet/IP, ProfiNet, EtherCAT) when you need deterministic timing or cybersecurity. Don't put Modbus on the open internet without a VPN."
- **Option B's `mapping.validate(logic)` deserves a callout, not a sentence.** This is *the* "honesty feature" of the codegen story:

  > **The validator is the bridge.** pyrung lets you write rich expressions because the simulator can handle them. Click can't. The validator catches every gap between what you wrote and what your target can run, and tells you exactly what to fix. By the time `validate()` is clean, the codegen is guaranteed to produce something the PLC can run — bit for bit, the same behavior as the simulator. You don't have to learn Click's restrictions up front; the validator teaches you which ones matter for your specific code.

- **Option C is *wild* and the lesson is matter-of-fact about it.** "This produces a self-contained CircuitPython file with a `while True` scan loop that runs the same sorting logic directly on a P1AM-200 microcontroller." Read that again. **pyrung is a transpiler that generates real hardware-targeting Python.** Celebrate it:

  > **Same source, two runtimes.** The CircuitPython codegen produces a complete Python file with a scan loop, hardware initialization, and your logic — ready to copy to a board's flash. Same conveyor sorting station you simulated, same tests you wrote, now running on a $200 microcontroller with real Productivity1000 I/O. No PLC software, no proprietary editor, no licensing fees, no vendor lock-in. If you can write Python, you can deploy industrial control. That's the whole pitch.

- **Mixed deployments aren't mentioned and they're real.** "These three options aren't mutually exclusive. Many real systems use Modbus for the HMI, generated Click code for the local control logic, and a P1AM-200 for distributed I/O. The same pyrung source can target all three."

### Hardware reality check

- **Add a "your simulation was deterministic; your hardware isn't" callout:**

  > **Hardware will surprise you.** Your simulation was deterministic. Your hardware is not. Sensor noise, contact bounce, ground loops, EMI, and mechanical chatter are real, and pyrung can't simulate them. When something behaves on the bench but misbehaves in the cabinet, you're back to oscilloscopes and multimeters. The DAP debugger and forces work against a *running* pyrung program (Option A) — but for Options B and C, your debugging tools are the vendor's: Click Programming Software's Data View, the P1AM-200's serial console.

- **Debounce/filtering forward reference.** "Lesson 5's `on_delay` and Lesson 4's `rise()` are the building blocks for debounce filters — the [Forces & Debug guide](https://ssweber.github.io/pyrung/guides/forces-debug/) covers patterns."

### "Where to go from here" — make it a story

The current section is a links list. Restructure:

1. **What you built** (the moved celebration paragraph from the bottom)
2. **What's next on the conveyor** — concrete suggestions for extending the project: add an HMI screen via Modbus, add Modbus comms to weigh-scale equipment, add a recipe system using `named_array`, add data logging via Block slot configuration. Each suggestion points to a relevant doc.
3. **What's next in PLC programming** — the broader landscape: PackML for state-machine standardization, OPC UA for plant-floor connectivity, safety-rated controllers (Pilz, Sick, Banner) for real safety, IEC 61131-3 SFC for graphical state machines. None are pyrung features but the learner is now ready to engage with them.
4. **What's next in pyrung itself** — the existing links list, framed as "deeper into the same toolkit," not "homework."

**Close with the Zen of Ladder mapping from the cross-cutting section below** — this is where the learner has earned every line. One sentence introducing it, then the table.

### Smaller things

- **`Bin[1].sensor` in the mapping** is a good demonstration of `.map_to()` from Lesson 9 — the lesson should explicitly forward-reference it.
- **The `pyrung_to_ladder(logic, mapping, "conveyor/")` call generates a directory** — say which files: "you'll get a folder with one CSV per program, a nickname file for the tag table, and a manifest. ClickNick's Guided Paste reads the manifest and walks you through importing each piece into Click in the right order."
- **ClickNick name-drop needs one sentence.** "ClickNick is a separate tool that automates the painful parts of getting generated ladder into Click Programming Software — file-by-file Guided Paste, nickname imports, and address mapping."
- **No "what doesn't port" list.** Every codegen target has limits. Lesson 5 timer family, Lesson 9 structures, Lesson 4 inline math — each has aspects that need translation or rejection on Click. A small list at the bottom of Option B helps learners predict where validate() will complain.
- **The exercise is missing.** Every other lesson has one. The natural exercise: "run `mapping.validate(logic)` on your project. What does it complain about? Pick one and fix it."

> **Heads up — elsewhere:** Binding tag names to physical addresses is universal — every PLC calls it something (I/O assignment, module-defined tags, nicknames, slot binding). Fail-safe E-stops are also universal: the red button wires through a safety relay to a dedicated NC input, regardless of brand. **Everything else in pyrung's hardware story has no equivalent anywhere.** Cross-target validation, multi-runtime codegen, the Modbus TCP runtime as a programmable option — none of this exists in vendor tooling. Real PLC software is single-vendor, single-target, no validation across vendors, no test continuity, no codegen story. pyrung is the only thing that lets you write logic once, validate it against multiple targets, test it deterministically, and deploy it to real hardware — pick your runtime.

---

## Cross-cutting: "Order matters, so don't make it matter"

This is a single teaching currently split across four lessons (1, 4, 7, 8) as if it were four separate concerns. It's not. It's one idea with a problem and an answer:

- **The problem (Lesson 1):** Order has meaning. The scan walks rungs top-to-bottom and the last write wins. That's a fact about how PLCs execute and learners need to know it.
- **The catch (Lesson 4):** Order-dependence is a *side effect* — invisible from any single rung. To know what value `Motor` ends up with, you have to know every other rung that touches `Motor` *and* their relative positions. That's not a property of the rung you're reading; it's a property of the whole program. Hostile to grep, hostile to refactoring, hostile to the 3am tech.
- **The answer (Lesson 8):** **One coil, one rung.** If `Motor` is only written in one place, the order-between-rungs stops mattering for that coil. The rung you're reading is the *complete* story for that output. To get there, you fold every reason the output should energize into the conditions of that single rung — which is exactly what branches, `any_of`/`all_of`, and the gate pattern are *for*. Lesson 8 isn't really about "how to OR things together"; it's about "how to fold every reason an output should energize into one rung so order stops being a side effect."
- **The exception that proves the rule:** mutually-exclusive subroutines that conditionally write the same coil from different places. It works *because* the mutual exclusivity guarantees exactly one rung executes per scan — "one coil, one rung" enforced by control flow instead of by structure. Worth mentioning once as an advanced pattern, with a warning that the burden of proving mutual exclusion is on the programmer.

The two Zen lines that capture this should be quoted *together*, not separately:

> *"And order has meaning. But use order side effects sparingly."*
> *"One coil, one rung."*

The first is the problem; the second is the answer. They're not two rules — they're one rule stated twice, once as a description of reality and once as a discipline. Lessons 1 and 4 should set up the problem, and Lesson 8 should land the answer with the explicit callback: "Remember 'order has meaning' from Lesson 1? This is how you escape it."

This reframing also retroactively explains why the "last rung wins" gotcha shows up so often in the early lessons — it's not noise, it's foreshadowing.

## Cross-cutting: The Zen of Ladder as connective tissue

This is the canonical home for the Zen mapping. Individual lessons reference it; don't restate it.

- The [Zen of Ladder](https://ssweber.github.io/blog/zen-of-ladder/) is the perfect spine for the whole guide. It's a Zen of Python pastiche, which means the Python audience already knows the *form* — it lands instantly. Link it from the Learn landing page as a manifesto, and use individual Zen lines as lesson epigraphs.
- Lesson 11's "Where to go from here" closes with the full mapping as a victory lap. Everywhere else just cites a single line as an epigraph.

| Lesson | Zen line |
|---|---|
| 1. Scan Cycle | *"The scan cycle is fast."* / *"Rungs giveth power and taketh away."* / *"And order has meaning. But use order side effects sparingly."* |
| 2. Tags | *"Name the purpose, not the part… unless you need a map to find it."* |
| 3. Latch and Reset | *"Latch only when needed."* + *"Don't forget safety."* |
| 4. Assignment | *"One coil, one rung."* (setup; payoff in 8) |
| 5. Timers | *"If you need a FOR loop… no you don't."* |
| 6. Counters | *"If you need a FOR loop… no you don't."* (count edges, not iterations) |
| 7. State Machines | *"PackML and state machines are a honking great idea — let's use more of those."* |
| 8. Branches | *"One coil, one rung."* (payoff) |
| 9. Structured Tags | *"Name the purpose, not the part."* |
| 10. Testing | *"Test it."* |
| 11. Hardware | *"The tech (maybe you) at 3am will thank you."* |

- **A `pyrung.zen` Easter egg** that prints the Zen of Ladder à la `import this` would be free marketing. Every Python dev who runs `import pyrung; pyrung.zen` will share the screenshot.

## Cross-cutting: Cross-lesson callbacks

Several threads are set up early and never explicitly closed. A "see Lesson N" forward/backward reference culture would tighten the whole guide. Each lesson should be writable as a standalone page but should also feel connected when read in sequence.

- **Lesson 1's "last rung wins"** returns in Lessons 3, 4, 7, and 8 but rarely with an explicit reference. Close the loop in Lesson 8 (see "Order matters" cross-cutting above).
- **Lesson 4's `rise()`** is critical in Lessons 5, 6, 7 but isn't called back. Lesson 7's IDLE→DETECTING transition is the highest-leverage place to land the callback.
- **Lesson 2's doubled-name string** finally resolves in Lesson 9. Lesson 9 should say so.
- **Lesson 3's `~StopBtn` NC convention** should pay off in Lesson 11's TagMap + `EstopOK` discussion. Currently doesn't.
- **Lesson 5's FIXED_STEP determinism** should cash in at Lesson 10. Currently implicit.

Each of these is a one-line "remember X from Lesson N?" — not a full re-teaching.
