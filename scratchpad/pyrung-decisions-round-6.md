# pyrung cleanup — Round 6 decisions

## Checklist

- [x] 1. Conditions: `And` / `Or`, drop `&` / `|`
- [x] 2. Ship built-in `Timer` and `Counter` udts
- [x] 3. `Timer.name(n, "Name")` for named instances
- [x] 4. Single-argument timer/counter signatures
- [ ] 5. Tag naming guidance in docs (separate task)

## 1. Conditions: `And` / `Or`, drop `&` / `|`

- Rename `all_of` → `And`, `any_of` → `Or` (PascalCase, reads as combinator).
- Remove `&` and `|` operators for conditions (keep for math/bitwise).
- Comma inside `Rung(...)` stays as implicit AND.
- One way to AND (comma or `And()` when nested), one way to OR (`Or()`), no precedence trap.

```python
with Rung(Start, Or(RemoteStart, AutoStart)):
    latch(Motor)

with Rung(Or(Start, And(AutoMode, Ready), RemoteStart)):
    latch(Motor)
```

## 2. Ship built-in `Timer` and `Counter` udts

- Core `pyrung` defines them (generic):
  ```python
  @udt()
  class Timer:
      done: Bool
      acc: Int

  @udt()
  class Counter:
      done: Bool
      acc: Dint
  ```
- Click dialect provides auto-pairing to `T[i]`/`TD[i]` and `CT[i]`/`CTD[i]` via `TagMap` special-casing on the built-in types.
- User-defined udts with Bool+Int/Dint shape still work via explicit per-field `map_to`.

## 3. `Timer.name(n, "Name")` for named instances

- Classmethod on the udt. Configures instance `n` with a semantic name and returns that instance.
- Parallels the layering we already have:
  - `Timer[n]` — anonymous, auto-generated name, throwaway/simulation.
  - `Timer.name(n, "Oven")` — named instance, 95% case for real code.
  - `Timer.clone("NewType", count=5, retentive=True)` — new udt type with different structure/policy.
- No generic `configure()` on udts. Name is the only per-instance override worth a shortcut; everything else belongs on `clone()`.

```python
OvenTimer  = Timer.name(1, "OvenTimer")
CycleTimer = Timer.name(2, "CycleTimer")

with Rung(Enable):
    on_delay(OvenTimer, preset=3000)
```

## 4. Single-argument timer/counter signatures

- Drop the two-tag `(done, acc)` form entirely. No overload, no legacy path.
- Instructions take a `Timer` or `Counter` instance:

```python
on_delay(OvenTimer, preset=3000)
on_delay(OvenTimer, preset=3000).reset(ResetBtn)      # RTON
off_delay(CoolDown, preset=5000)
count_up(PartCounter, preset=100).reset(ResetBtn)
count_down(Dispense, preset=25).reset(Reload)
```

- Nothing's in the wild; search-replace and ship.
- Every docs example rewrites to this form at the same time as the udt-pattern rewrite.

## 5. Tag naming guidance in docs

- For real programs deploying to hardware, use `Timer.name(n, "...")` — `Timer1_done` in a fault log six months later tells you nothing; `OvenTimer_done` tells you everything.
- `Timer[n]` is fine for throwaway simulation tests.
- Document this explicitly on the Timers learn page.
