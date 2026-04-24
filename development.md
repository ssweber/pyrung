# Development

## Setting Up uv

This project uses [uv](https://docs.astral.sh/uv/) to manage Python and dependencies.
[Install uv](https://docs.astral.sh/uv/getting-started/installation/) first.

Then [fork the repo](https://github.com/ssweber/pyrung/fork) and
[clone it](https://docs.github.com/en/repositories/creating-and-managing-repositories/cloning-a-repository).

## Basic Developer Workflows

The `Makefile` offers shortcuts to `uv` commands.
(GitHub Actions call `uv` directly, not the Makefile.)

```shell
# Install all dependencies (locked):
make install

# Run install, lint, and test:
make

# Linting (codespell, ruff, ty):
make lint

# Run tests:
make test

# Build wheel:
make build

# Delete build artifacts:
make clean

# Upgrade dependencies:
make upgrade
```

### Running tests by hand

```shell
uv run pytest                              # all tests
uv run pytest -s tests/core/test_tag.py    # one file, showing output
```

### Dependency management

```shell
uv add package_name          # add dependency
uv add --dev package_name    # add dev dependency
uv lock --upgrade            # upgrade all to latest compatible
uv lock --upgrade-package X  # upgrade one package
```

## VS Code Extension (`editors/vscode/pyrung-debug/`)

The debug extension is plain JS with no build step.

```shell
# Requires Node.js LTS:
winget install OpenJS.NodeJS.LTS    # Windows
# or download from https://nodejs.org

# Package:
cd editors/vscode/pyrung-debug
npx @vscode/vsce package

# Install the .vsix:
code --install-extension pyrung-debug-0.1.0.vsix
```

## IDE Setup

If you use VS Code (or Cursor/Windsurf):

- [Python](https://marketplace.visualstudio.com/items?itemName=ms-python.python)
- [ty](https://marketplace.visualstudio.com/items?itemName=astral-sh.ty) for type checking

## Internal Debug Architecture

Debugger internals are split into a typed trace model plus adapter serialization:

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
