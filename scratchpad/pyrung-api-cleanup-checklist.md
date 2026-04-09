# pyrung API Cleanup Checklist

No engine semantics change. Naming, ergonomics, doc rewrites.
Published but v0.3, no users — breaking changes are fine.

## Phase 1: Foundation (do first, everything references it)

- [x] **#1 `PLCRunner` -> `PLC`** — Drop `.active()`, fold `dt=` into constructor (default `0.010`). `Program` stays unchanged.
- [x] **#2 Stay in the context manager** — Doc rewrite: hoist `with PLC(...) as plc:` once at top, stop showing enter/exit-per-operation.
- [x] **#6 Drop `TimeMode` from public surface** — Replace with `PLC(logic, realtime=True)` kwarg. Enum stays internal. Note: `dt=` and `realtime=True` are mutually exclusive — raise if both provided.
- [x] **#8 `set_battery_present` -> property** — `plc.battery_present = False` instead of method call.

## Phase 2: Public surface cleanup

- [ ] **#3 `plc.debug.*` namespace** — Move 11 debugger-internal methods off public class into `plc.debug.*`. Also privatize `system_runtime`.
- [ ] **#5 Drop `_fn` variants** — Merge `run_until_fn` into `run_until`, `when_fn` into `when`. Type-check the argument (condition vs callable).
- [ ] **#7 Force API: one verb stem** — `force()`, `unforce()`, `clear_forces()`, `with plc.forced({...}):`, `plc.forces`.
- [ ] **#20 Privatize `system_runtime`** — Rename to `_system_runtime` or move under `plc.debug`. Rides with #3.
- [ ] **#24 Privatize `Program` internals** — `add_rung`, `start_subroutine`, `end_subroutine`, `evaluate`, `current` -> `_private`. Delete legacy `call_subroutine`.
- [ ] **#25 Privatize `TagMap` internals** — `resolve`, `offset_for`, `block_entry_by_name`, `mapped_slots`, `tags_from_plc_data`, `owner_of` -> `_private`.

## Phase 3: Time units + validation

- [ ] **#4 Time units as strings** — Drop `Tms`/`Ts`/`Tm`/`Th`/`Td` as importable symbols. Use `unit="Tms"` string form. `Literal[...]` type hint. Codec shim: `pyrung_to_ladder` make sure to still write as unit=Td (no quotes) `ladder_to_pyrung` wraps token in quotes.
- [ ] **#23 Validation: three entry points -> two** — Keep `logic.validate(dialect=...)` (universal) + `mapping.validate(logic)` (convenience). Drop standalone functions. Surface `register_dialect` in docs.

## Phase 4: Docs pass (no code risk, can parallel)

- [ ] **#10 udt-based timers in generic docs** — Rewrite quickstart/concepts/learn around `@udt` timer pattern. Click dialect unchanged.
- [ ] **#11 Document numeric behavior loudly** — De-duplicate: `instructions/math` is authoritative, drop table from `guides/runner`. Reference fault flags by name.
- [ ] **#12 Click timer preset INT cap** — Add preset-range table to Click docs. Validate at construction time (raise instead of silent clamp).
- [ ] **#13 Conditions: comma-first** — Lead with comma form in docs. Document `&`/comparison precedence trap prominently.
- [ ] **#14 Document `system` namespace** — Add prose section explaining `system.sys.*`, `system.rtc.*`, `system.fault.*`.
- [ ] **#15 Small doc cleanups** — Counter accumulator positional fix. Rewrite `ScanContext` mention. Check slot config error message.

## Skipped

- **#9 Import split** — Marginal benefit, migration burden on every example/test.
- **#16 Dialect validation convergence** — #23 gets 80% of the value. Field name convergence can wait.
- **#18 `hw.slot()` NamedTuple** — 3 of 35 modules. Not worth the diff.
- **#19 `target_scan_ms` vs `dt`** — Self-documenting suffix, not worth the breakage.
- **#26 `run_function` rename** — Escape hatch, rarely used. Rename when someone complains.
- **#27 Hide builder base classes** — Cosmetic autogen. Fix if already touching doc config.
- **#28 `r.comment` removal** — Needs investigation. Separate ticket.

## Explicitly NOT Changing

- Tag names stay explicit (`Bool("Button")`)
- Timer/counter instruction names stay (`on_delay`/`off_delay`)
- `copy` clamps, `calc` wraps (matches Click)
- Three AND forms, two OR forms stay (`all_of`/`any_of` load-bearing)
- `oneshot=True` stays as cross-cutting modifier
- `patch()` and `.value =` both stay
- `Rung.continued()` stays (codegen round-trip)
- `Int2`, `Bit`, `Float`, `Hex`, `Txt` Click aliases stay
- Drum/shift/search builder chains stay
- CommStatus deferred to later
