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
- `decorationController.js` — editor inline decorations from trace regions
- `historyPanel.js` — history timeline (live-append from frame, backward-page from request)
- `dataViewProvider.js` — live tag value grid
- `graphPanel.js` — Cytoscape dependency graph with live value coloring
