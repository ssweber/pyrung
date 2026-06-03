# Analysis

pyrung's analysis tools answer four questions, each requiring different inputs. Pick the page that matches what you have:

1. **[Program Structure](analysis-structure.md)** — just the program. DataView, simplified forms, static validators. "What does my program look like?"

2. **[Diagnosis](analysis-diagnosis.md)** — program + snapshot. `why()` and `how()`. "My machine is down — what's wrong and how do I fix it?"

3. **[Cause & Effect](analysis-causal.md)** — program + scan history. `cause()`, `effect()`, projected paths, `assume=`. "What happened, why, and what would happen if?"

4. **[Test Coverage](analysis-coverage.md)** — program + test suite. Cold/hot rungs, stranded bits, pytest plugin, CI gating. "Is my testing complete?"

The four pages escalate by what you need to bring — and map to the lifecycle: write → deploy → debug → verify.
