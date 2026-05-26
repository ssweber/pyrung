# `diagnose()` amendments

## Multi-tag unified diagnosis

The runner method should accept multiple tags, matching the input style of `cause()` / `effect()` / `prove()`:

```python
def diagnose(self, *tags: Tag | str) -> CausalChain:
```

Since the full system state is already loaded, the tags aren't inputs — they're queries. "I've given you everything. Now explain these, together."

```python
# one tag — why is this the way it is?
result = runner.diagnose(FaultAlarm)

# two tags — why are these the way they are, together?
result = runner.diagnose(FaultAlarm, MotorStall)

# three — narrow further
result = runner.diagnose(FaultAlarm, MotorStall, CoolingPumpOff)
```

The multi-tag case isn't two independent diagnoses. It's one unified question: what single explanation is consistent with all of these being in these states simultaneously? Branches that explain one tag but are inconsistent with another get pruned. What survives is the common causal structure.

Output is one tree, not N trees. Multiple consequences converging on shared roots.

## Interactive exploration

The engineer uses this to poke around. Start with one tag, see what comes back, add another, watch branches collapse. Each call is cheap — same snapshot, same program, different query.

```python
# "huh, fault alarm is on"
runner.diagnose(FaultAlarm)

# "oh, motor stalled too — are these related?"
runner.diagnose(FaultAlarm, MotorStall)

# "and the cooling pump is off — that's the link"
runner.diagnose(FaultAlarm, MotorStall, CoolingPumpOff)
```

Each additional tag is a constraint that narrows the explanation. The engineer brings domain knowledge the tool doesn't have — they know which tags smell wrong, which ones are surprising, which ones shouldn't be in that state. The tool does the structural reasoning. The engineer steers.

In the GUI this is selecting tags from a list. Check one, see a tree. Check another, tree simplifies. The ambiguous OR branches from a single-tag diagnosis resolve as you add observations. The engineer converges on the root cause by combining what they see on the machine with what the tool knows about the program.
