# Console

The Console takes typed commands to run the PLC, force tags, ask why something happened, and record what you did. You can type at it in three places.

- The Simulator console in ClickNick.
- The Console in the [VS Code debugger](dap-vscode.md).
- `pyrung live` in a terminal, covered [below](#pyrung-live).

Type `help` to print the list below. All three take these commands, with these differences:

- ClickNick and `pyrung live` chain commands with `;` and stop at the first error. VS Code sends the whole line as one command.
- ClickNick doesn't support `autoreload`, `record` or `replay` yet.
- In ClickNick, send `reload` on its own. ClickNick writes the program to the Simulator (a RUN Time Edit in RUN) and carries forces and links to the new program.
- ClickNick refuses commands while it writes a program.

## Command reference

<!-- dap-console-help:start -->
```text
execution:
  autoreload [off]
  continue
  mode [run|stop]                                       (PLC RUN/STOP mode)
  pause
  reload
  run <N | duration>  (e.g. 10, 500ms, 2 s)
  speed [N|max]                                         (1 = real time; 0 or max = unpaced)
  step [N]
  stop                                                  (cancels a running analysis)

data:
  bounds [clear]                                        (runtime constraint log)
  clear_forces
  force <tag> <value>
  get <tag> [tag2 ...]
  link <feedback> <[~]enable[:trigger]> key=value ...   (bench wiring)
  links [bench on|off | enable <tag> | disable <tag>]
  monitor <tag>
  note <text>
  patch <tag> <value>
  restore <tag>                                         (drop a bench change)
  unforce <tag>
  unlink <tag>                                          (cut on the bench)
  unmonitor <tag>

analysis:
  check [RULE_OR_CATEGORY ...]
  log [N]

  dataview <text | i: | p: | t: | upstream:tag>
  downstream <tag>
  upstream <tag>
  structures

  cause <tag>[@scan|:value]
  effect <tag>[@scan|:value]
  recovers <tag>
  why <tag> [tag2 ...]
  how <expression>[, <expression>...] [avoid <expression>[, <expression>...]]  (runs planner)
  prove always|never <expression> [--settled] [--paced]  (exhaustive, needs annotations)

  simplified [tag]

capture:
  harness <install|remove|status>                       needs physical+link=
  record <action> | record stop
  replay <filepath> [--harness current|recorded|off]

review:
  accept <id>
  candidates [list]
  deny <id>
  spec [list] | spec test <filepath>
  suppress <id>

other:
  help
```
<!-- dap-console-help:end -->

## Values, forces, patches, and bounds

```text
get Button Counter        # read one or more current values
force Button true          # persistent override, held across scans
unforce Button             # remove a force
clear_forces               # remove all forces
patch Button true          # one-shot input, consumed after one scan
bounds                     # review runtime min/max/choices violations
bounds clear               # clear the accumulated violation log
```

## Stepping and running

```text
step                       # advance one scan
step 5                     # advance 5 scans
run 100                    # run 100 scans
run 500ms                  # run for 500ms of sim time
run 2 s                    # run for 2 seconds (split unit ok)
continue                   # run continuously (background)
pause                      # stop a running continue
```

`step` and `run` process breakpoints and logpoints during execution. If a breakpoint fires, the command stops early and reports it. Duration parsing accepts `ms`, `s`, `m`, `min`, `h`, and `d`, including compact compound durations such as `1h30min`.

`mode` and `speed` set the PLC mode and the pacing. Each prints the current setting when given no argument.

```text
mode stop                  # put the PLC in STOP
mode run                   # back to RUN
speed 2                    # run Continue at twice real time
speed max                  # unpaced (same as speed 0)
```

## Why, cause, effect, and how

These four answer "what happened" and "how do I make it happen". The [Cause & Effect](analysis-causal.md) and [Diagnosis](analysis-diagnosis.md) guides cover the same questions from Python.

```text
cause Light                # why did Light last change?
cause Light@5              # why did Light change at scan 5?
cause Light:true           # how could Light reach true? (projected)
effect Button              # what did Button's last change cause?
effect Button:true         # what would happen if Button became true?
recovers Light             # can Light return to its resting value?
why Light Button           # explain current values without scan history
how Running                # find a verified input-change sequence
how State == HELD avoid State == FAULTED
how Burner avoid ProdMode, MaintFault
```

`cause` and `effect` read the scan history. `@scan` picks a scan and `:value` asks about a projected change instead of a past one. `why` needs no history, so it works on a fresh session.

`how` takes one or more comma-separated targets that must all hold at the end of the path. The optional `avoid` clause lists conditions the path must never pass through. Each is avoided on its own. The planner prints progress as it works.

`how` and `prove` hold the Console's state lock while they run. Cancel one from another terminal with `pyrung live stop`, since a `stop` typed in the same Console cannot preempt its own request. `cancel` works as well as `stop` from `pyrung live`.

## Program inspection and verification

```text
check                      # run every static ladder check
check COIL TAG             # select rules or categories
structures                 # list UDTs and named arrays
prove always ~Fault        # exhaustively verify an invariant
prove never MotorA, MotorB # verify a bad combination is unreachable
```

## DataView queries

```text
dataview Motor             # tags containing "Motor"
dataview i:                # all input tags
dataview p:Motor           # pivots containing "Motor"
dataview upstream:Light    # upstream dependencies of Light
dataview downstream:Button # downstream effects of Button
upstream Light             # shorthand for dataview upstream:Light
downstream Button          # shorthand for dataview downstream:Button
```

The query language supports role prefixes (`i:` inputs, `p:` pivots, `t:` terminals, `x:` isolated) and slice prefixes (`upstream:`, `downstream:`). Multiple tokens are applied left to right.

## Simplified form

```text
simplified                 # show all terminals' simplified Boolean forms
simplified MotorOut        # show one terminal, resolved to inputs
```

## Monitors

```text
monitor Button             # watch Button for value changes
unmonitor Button           # stop watching
```

## Notes and log

Annotate the timeline with free-text notes and review recent activity.

```text
note testing startup sequence   # attach a note to the current scan
note momentary start press      # another note before the next action
log                             # show last 20 scans of activity
log 50                          # show last 50 scans
```

`log` shows everything that happened in the scan window. That covers patches, force changes, tag transitions and notes.

## Session capture

Record a sequence of console commands as a replayable transcript. ClickNick doesn't support `record` or `replay` yet.

```text
record start_machine       # begin recording action "start_machine"
patch State 1
run 500ms
patch State 2
step 1
record stop                # stop recording, print transcript
```

On stop, the transcript is printed as plain text, with one command per line and a `# action:` comment header. The transcript is the session file format. Paste it back into the console, or save it to a file for replay.

```text
replay session.txt                            # use the current program harness
replay session.txt --harness recorded         # replay recorded harness inputs
replay session.txt --harness off              # ignore harness inputs
```

Replay executes each command in order. If a breakpoint fires or a command fails, replay halts and reports the line. The default `current` harness mode installs the harness declared by the current program and skips recorded harness inputs. `recorded` removes the current harness and applies provenance-tagged harness inputs from the transcript. `off` removes the harness and skips those inputs.

Stopping a recording also mines candidate invariants from the captured scan range. Review them before turning accepted formulas into tests:

```text
candidates                     # list mined candidates and their IDs
accept c-01                    # keep a candidate
deny c-02                      # discard this occurrence
suppress c-03                  # discard and suppress the formula in later captures
spec                           # list accepted formulas
spec test tests/test_specs.py  # generate pytest tests for accepted formulas
```

## Autoharness

If your program uses [Physical annotations](physical-harness.md) with `physical=` and `link=` declarations, the adapter installs the autoharness automatically at launch. A banner appears in the Console:

```text
Harness: 3 feedback loop(s) (2 delay, 1 ramp); `harness status` for details
```

A force still wins over the harness. Force a feedback tag to hold it whatever the harness computes.

```text
harness status             # show couplings, pending patches, active state
harness remove             # disable the harness for this session
harness install            # re-install after removal
```

## Bench links

Bench links change the feedback wiring for this session without editing the program. You can add a link, replace one the program declares, or cut one to simulate a failed sensor or a stuck contactor. They take the same keys as a link in a nickname comment. Tags may be names or CLICK addresses:

```text
link X002 Y001 on_delay=2s off_delay=500ms     # Y001 drives X002 after 2 s, drops 500 ms after
link DF5 Y003 ramp_on=0.8 ramp_off=-0.05       # analog feedback
link Sorted State:2 on_delay=1s                # value trigger
unlink X002        # cut the program's link (X002 is a free input), or remove a bench link
restore X002       # drop the bench change (back to what the program declares)
links              # list program and bench links
links bench off    # set every bench change aside (`links bench on` brings them back)
links disable X002 # set one aside (`links enable X002` brings it back)
```

A link added while its enable is already on starts its on-delay from zero, so the feedback follows only after the on-delay.

Bench links live in the debug session, not the runner, so `reload` keeps them. A recording captures the link verbs, and replay runs them only with `--harness current`.

## pyrung live

Attach to a running debug session from another terminal or process.

When the DAP adapter launches, it starts a TCP server on localhost and writes the port to a session file. The session name defaults to the program filename stem (`logic.py` → `logic`). In VS Code, you can set it in the launch configuration instead:

```json
{
  "type": "pyrung",
  "request": "launch",
  "program": "${file}",
  "session": "my_session"
}
```

### CLI usage

```text
pyrung live                                # list active sessions
pyrung live step 5                         # works when only one session is active
pyrung live -s my_session step 5           # explicit session when multiple are running
pyrung live "force Button true; step 5; cause Light"  # chain commands with ;
pyrung live -h                             # show all available commands
```

Commands chained with `;` run in order over one connection and halt on the first error. The exit code is 0 on success and 1 on error.

### Python library

```python
from pyrung.dap.live import send_command, list_sessions

sessions = list_sessions()          # ["logic", "pump_test"]
ok, text = send_command("logic", "step 5")
print(text)                         # "Stepped 5 scan(s), now at scan 10"
```

### Pair commissioning

One person drives VS Code (breakpoints, Data View, Graph View). Another person runs `pyrung live` from a terminal. Both work on the same PLC state. Commands from the live client show `(live)` in the `log` output, so you can tell who did what:

```text
scan 10:
  patch StartBtn True
  Running: False -> True
scan 11:  (live)
  force EntrySensor True
  State: 0 -> 1
```

The live client can do everything the Console can, including forces, patches, causal queries and recording. Stepping from either side advances the same scan counter.

## Reload

`reload` re-executes the program file and keeps PLC state. Tags, memory, timer accumulators, counter values and forces all carry over. History clears. This is the same experience as downloading a new program to a running PLC.

```text
reload                         # from the Console
pyrung live reload             # from another terminal
```

Scan ID and timestamp continue from where they were. Debug registrations (breakpoints, monitors) are cleared since rung indices may have shifted.

A reload keeps the PLC mode, so a reload in STOP stays in STOP. A reload sent while `continue` runs pauses it first.

Forces and bench links follow the tag name. A reload names each force and bench link whose tag moved to another CLICK address, and each one it dropped:

```text
The force on A moved from C1 to C5.
Dropped the force on B because B is not in the new program.
```

`autoreload` does the same on every file save. See [Auto-reload](dap-vscode.md#auto-reload-on-file-save) for how it watches.

### Tag type changes

If a tag changes type between reloads (e.g. `Bool('X')` becomes `Int('X')`), the old value is dropped and the tag gets its new default, with a warning.

New tags added to the program get their defaults. Removed tags become harmless orphans in state.

### Reload errors

If the program file has a syntax error or the runner can't be discovered, the reload aborts and the old runner stays active. Fix the file and reload again.
