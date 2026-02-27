# Development

## Setting Up uv

This project is set up to use [uv](https://docs.astral.sh/uv/) to manage Python and
dependencies. First, be sure you
[have uv installed](https://docs.astral.sh/uv/getting-started/installation/).

Then [fork the ssweber/pyrung
repo](https://github.com/ssweber/pyrung/fork) (having your own
fork will make it easier to contribute) and
[clone it](https://docs.github.com/en/repositories/creating-and-managing-repositories/cloning-a-repository).

## Basic Developer Workflows

The `Makefile` simply offers shortcuts to `uv` commands for developer convenience.
(For clarity, GitHub Actions don't use the Makefile and just call `uv` directly.)

```shell
# First, install all dependencies and set up your virtual environment.
# This simply runs `uv sync --all-extras --dev` to install all packages,
# including dev dependencies and optional dependencies.
make install

# Run uv sync, lint, and test:
make

# Build wheel:
make build

# Linting:
make lint

# Run tests:
make test

# Delete all the build artifacts:
make clean

# Upgrade dependencies to compatible versions:
make upgrade

# To run tests by hand:
uv run pytest   # all tests
uv run pytest -s src/module/some_file.py  # one test, showing outputs

# Build and install current dev executables, to let you use your dev copies
# as local tools:
uv tool install --editable .

# Dependency management directly with uv:
# Add a new dependency:
uv add package_name
# Add a development dependency:
uv add --dev package_name
# Update to latest compatible versions (including dependencies on git repos):
uv sync --upgrade
# Update a specific package:
uv lock --upgrade-package package_name
# Update dependencies on a package:
uv add package_name@latest

# Run a shell within the Python environment:
uv venv
source .venv/bin/activate
```

See [uv docs](https://docs.astral.sh/uv/) for details.

## IDE setup

If you use VSCode or a fork like Cursor or Windsurf, you can install the following
extensions:

- [Python](https://marketplace.visualstudio.com/items?itemName=ms-python.python)

- [Based Pyright](https://marketplace.visualstudio.com/items?itemName=detachhead.basedpyright)
  for type checking. Note that this extension works with non-Microsoft VSCode forks like
  Cursor.

## Internal Debug Architecture

Debugger internals are intentionally split into a typed trace model plus adapter serialization:

- Core stepping (`PLCDebugger`) emits `TraceEvent` objects (`SourceSpan`, `TraceRegion`,
  `ConditionTrace`) and `ScanStep.trace` carries this typed model.
- DAP owns the wire conversion boundary and translates typed traces into the `pyrungTrace`
  event payload consumed by clients.
- The debugger/runner contract is expressed through `DebugRunner` in
  `src/pyrung/core/debugger_protocol.py`.

This debug contract is **internal-only**:

- Do not export `DebugRunner`, handler classes, or debug trace models in top-level public
  API surfaces.
- Keep compatibility commitments focused on user-facing APIs and DAP wire payload fields,
  not internal debugger helper protocols.

Instruction stepping uses a registry + handler pattern:

- `CallInstructionDebugHandler` and `ForLoopInstructionDebugHandler` handle control-flow-heavy
  instructions.
- `GenericInstructionDebugHandler` is the fallback for all other instructions.
- `PLCDebugger` remains an orchestrator; handlers encapsulate instruction-specific flow.

## Documentation

- [uv docs](https://docs.astral.sh/uv/)

- [basedpyright docs](https://docs.basedpyright.com/latest/)

* * *

*This file was built with
[simple-modern-uv](https://github.com/jlevy/simple-modern-uv).*
