# Handoff:

## Suggested Next Steps

1. **Other missing instructions** — `loop()`, `search()`, `shift_register()`, `pack_bits()`/`unpack_to_bits()`,
2. **Spec alignment audit** — check `dsl.md`, `engine.md`, `instructions.md` for divergences like types.md had
3. **Debug API** (`spec/core/debug.md`) — `force()`, `when()`, `monitor()`, history — larger effort

## Key Design Decisions Made (cumulative)

- Click-specific features (nicknames, register, read_only) deferred to future `dialects/click` `TagMap`