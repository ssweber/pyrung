# pyrung

**Pythonic PLC simulation engine.** Write ladder logic as Python, simulate it with full step-through debugging, and optionally target real hardware through dialect modules.

```python
from pyrung import Bool, PLCRunner, Program, Rung, TimeMode, out

# Define tags
Button = Bool("Button")
Light  = Bool("Light")

# Write logic
with Program() as logic:
    with Rung(Button):
        out(Light)

# Simulate
runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
runner.patch({"Button": True})
runner.step()

print(runner.current_state.tags["Light"])  # True
```

## Why pyrung?

| Feature | pyrung | Traditional simulation |
|---------|--------|------------------------|
| Logic syntax | Pure Python | Proprietary GUI / IEC text |
| State | Immutable snapshots | Mutable in-place |
| Time control | `FIXED_STEP` for exact determinism | Wall-clock only |
| Testing | Standard pytest | Custom tooling |
| Debugging | DAP + VS Code inline decorations | Separate runtime tool |

## Key concepts

- **Immutable state** — every scan produces a new `SystemState`; nothing is mutated in place.
- **Consumer-driven** — you call `step()`, `run()`, or `patch()`; the engine never runs unsolicited.
- **Hardware-agnostic engine** — base DSL/runtime APIs are exposed via `pyrung`. Click-specific features live in `pyrung.click`.

## Quick links

- [Installation](getting-started/installation.md) — `pip install pyrung`
- [Quickstart](getting-started/quickstart.md) — end-to-end example in 5 minutes
- [Core Concepts](getting-started/concepts.md) — Redux model, scan cycle, immutability
- [Writing Ladder Logic](guides/ladder-logic.md) — full DSL reference
- [API Reference](reference/index.md) — auto-generated from source
