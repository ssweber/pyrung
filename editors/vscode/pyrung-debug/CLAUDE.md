# pyrung-debug VS Code Extension

## Event Architecture

### Live-Forward During Continue (Push)

During Continue, the adapter emits `pyrungScanFrame` compound events at ~30fps (33ms throttle). Each frame contains everything live views need:

- `trace` — regions, tagValues, forces, tagTypes, tagHints, tagGroups (for decorations, data view, graph)
- `changes` — net tag deltas since last frame (for history panel live-append)
- `monitors` — user monitor change notifications
- `snapshots` — logpoint-triggered snapshot confirmations
- `outputs` — logpoint output messages

The extension fans out `pyrungScanFrame` in `extension.js` `onDidSendMessage` to all consumers. Views never poll the adapter during continue.

### Request-Based When Stopped (Pull)

After a `stopped` event, views that need historical data use DAP custom requests:
- `pyrungTagChanges` — backward pagination of tag change history
- `pyrungSeek` — move playhead to a historical scan
- `pyrungForkAt` — fork a new runner from a historical scan
- `pyrungHistoryInfo` — get retained scan range

### Step Commands (Unchanged)

Step handlers (next/stepIn/stepOut/pyrungStepScan) emit `pyrungTrace` events synchronously with `step` populated (per-rung, single step context). These use the existing handler — no frame involved.

## Adding a New Live View

1. Create the webview provider
2. In `extension.js`, add consumption from the `pyrungScanFrame` handler (access `body.trace.tagValues`, `body.changes`, etc.)
3. The view receives data pushed to it — no round-trips during continue

## Key Files

- `extension.js` — main activation, DAP tracker with event fan-out
- `adapterFactory.js` — debug adapter descriptor (resolves Python path, launches DAP)
- `decorationController.js` — editor inline decorations from trace regions
- `inlineValuesProvider.js` — VS Code inline values during debug (filters noise identifiers)
- `historyPanel.js` — history timeline, Chain tab for causal queries (`pyrungCausal` requests)
- `dataViewProvider.js` — live tag value grid with force/patch/unforce
- `graphPanel.js` — Cytoscape dependency graph with live value coloring

## Local Packaging Notes

- On this Windows machine, prefer the globally installed `vsce.cmd` over `npx @vscode/vsce package`. `npx` may fail on npm cache permissions even though `vsce` is already installed.
- Package with PowerShell from this folder:
  `& "$env:APPDATA\npm\vsce.cmd" package`
- If `code` is not on `PATH`, reinstall with:
  `& "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd" --install-extension "$PWD\pyrung-debug-0.6.0.vsix" --force`
