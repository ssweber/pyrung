# pyrung

PLC simulation engine with an immutable, pure-functional architecture.

## Key Folders / Files

- src/pyrung/core/   # Active development
- spec/ - Full architecture spec and API reference
- `docs/click_reference/README.md` - Click manual page links
- `tests/core/` - tests covering all implemented features

## Build & Development Commands

```bash
# Install dependencies
make install                    # or: uv sync --all-extras --dev

# Default workflow (install + lint + test)
make

# Individual commands
make lint                       # Run codespell, ruff (check + format), ty
make test                       # Run pytest (ALWAYS use this, not uv run pytest)
```

## Architecture

- **Immutable state**: `SystemState` is frozen, logic is pure `f(state) -> new_state`
- **Generator-driven**: Consumer controls execution via `step()`, `run()`, `patch()`
- **DSL**: Context managers for readable logic (`with Rung(Button): out(Light)`)

## Roadmap

1. `core/` is the generalized pyrung - loose typing, no PLC-specific restrictions
2. Future `dialects/click` module will layer Click-specific constraints (memory banks, type restrictions, address validation)

## Current Status

Milestones 1-6 mostly complete (core engine, logic, program structure);
Next: Realign to current spec