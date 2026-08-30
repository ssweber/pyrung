# Analysis

pyrung's analysis tools answer five questions, each requiring different inputs. Pick the page that matches what you have:

1. **[Program Structure](analysis-structure.md)** — just the program. DataView and simplified forms. "What does my program look like?"

2. **[Ladder Lints](ladder-lints.md)** — just the program. Static checks for contradictory rungs, conflicting outputs, suspicious comparisons, and other structural problems. "What looks wrong before I run it?"

3. **[Diagnosis](analysis-diagnosis.md)** — program + snapshot. `why()` and `how()`. "My machine is down — what's wrong and how do I fix it?"

4. **[Cause & Effect](analysis-causal.md)** — program + scan history. `cause()`, `effect()`, projected paths, `assume=`. "What happened, why, and what would happen if?"

5. **[Test Coverage](analysis-coverage.md)** — program + test suite. Cold/hot rungs, stranded bits, pytest plugin, CI gating. "Is my testing complete?"

The five pages escalate by what you need to bring — and map to the lifecycle: write → deploy → debug → verify.
