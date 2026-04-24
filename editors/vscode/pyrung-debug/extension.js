const vscode = require("vscode");

const { PyrungAdapterFactory } = require("./adapterFactory");
const { PyrungDataViewProvider } = require("./dataViewProvider");
const { PyrungDecorationController } = require("./decorationController");
const { PyrungGraphPanelProvider } = require("./graphPanel");
const { PyrungHistoryPanelProvider } = require("./historyPanel");
const {
  PyrungInlineValuesProvider,
  lookupName,
  stripCommentsAndStrings,
  extractReferences,
  isLookupCandidate,
} = require("./inlineValuesProvider");

function isPyrungSession(session) {
  return Boolean(session) && session.type === "pyrung";
}

function activePyrungSession() {
  const session = vscode.debug.activeDebugSession;
  return isPyrungSession(session) ? session : null;
}

function pyrungSessionById(sessionId) {
  if (!sessionId) {
    return null;
  }
  const session = vscode.debug.sessions.find((candidate) => candidate.id === sessionId);
  return isPyrungSession(session) ? session : null;
}

exports.activate = function (context) {
  const output = vscode.window.createOutputChannel("pyrung: Debug Events");
  const decorator = new PyrungDecorationController();
  const inlineValuesProvider = new PyrungInlineValuesProvider({
    getConditionLinesForDocument: (document) => decorator.conditionLinesForDocument(document),
  });
  const sessionExecutionState = new Map();
  const requestLogCommands = new Set([
    "continue",
    "pause",
    "pyrungHistoryInfo",
    "pyrungSeek",
    "pyrungTagChanges",
    "pyrungPatch",
    "pyrungForce",
    "pyrungUnforce",
  ]);
  const historyPanel = new PyrungHistoryPanelProvider({
    getExecutionState: (session) => sessionExecutionState.get(session.id) || "unknown",
    log: (message) => output.appendLine(`[history] ${message}`),
  });
  const dataView = new PyrungDataViewProvider({
    onWatchHistory: async (tagName) => {
      output.appendLine(`[history] watchHistory(${tagName}) from Data View`);
      historyPanel.addTag(tagName);
      await vscode.commands.executeCommand("pyrung.historySlider.focus");
    },
  });
  const graphPanel = new PyrungGraphPanelProvider({
    onAddToDataView: (tagName) => dataView.addTag(tagName),
    onAddToHistory: async (tagName) => {
      output.appendLine(`[history] addToHistory(${tagName}) from Graph`);
      historyPanel.addTag(tagName);
      await vscode.commands.executeCommand("pyrung.historySlider.focus");
    },
  });
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("pyrung.historySlider", historyPanel)
  );
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("pyrung.dataView", dataView)
  );

  const monitorStatus = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  monitorStatus.command = "pyrung.monitorMenu";
  monitorStatus.tooltip = "pyrung monitor controls";
  monitorStatus.hide();
  const setMonitorStatus = (count) => {
    const session = activePyrungSession();
    if (!session) {
      monitorStatus.hide();
      return;
    }
    monitorStatus.text = `M:${count}`;
    monitorStatus.show();
  };

  const refreshMonitorStatus = async (session = activePyrungSession()) => {
    if (!isPyrungSession(session)) {
      setMonitorStatus(0);
      return;
    }
    try {
      const response = await session.customRequest("pyrungListMonitors", {});
      const monitors = Array.isArray(response?.monitors) ? response.monitors : [];
      setMonitorStatus(monitors.length);
    } catch (_error) {
      setMonitorStatus(0);
    }
  };

  const requireSession = () => {
    const session = activePyrungSession();
    if (!session) {
      vscode.window.showWarningMessage("No active pyrung debug session.");
      return null;
    }
    return session;
  };

  const addMonitor = async () => {
    const session = requireSession();
    if (!session) {
      return;
    }
    const tag = await vscode.window.showInputBox({
      prompt: "Tag name to monitor",
      placeHolder: "MotorTemp",
      ignoreFocusOut: true,
    });
    if (!tag || !tag.trim()) {
      return;
    }
    try {
      await session.customRequest("pyrungAddMonitor", { tag: tag.trim() });
      await refreshMonitorStatus(session);
    } catch (error) {
      vscode.window.showErrorMessage(String(error));
    }
  };

  const removeMonitor = async () => {
    const session = requireSession();
    if (!session) {
      return;
    }
    try {
      const response = await session.customRequest("pyrungListMonitors", {});
      const monitors = Array.isArray(response?.monitors) ? response.monitors : [];
      if (!monitors.length) {
        vscode.window.showInformationMessage("No monitors to remove.");
        return;
      }
      const items = monitors.map((monitor) => ({
        label: monitor.tag,
        description: `#${monitor.id}`,
        monitor,
      }));
      const picked = await vscode.window.showQuickPick(items, {
        placeHolder: "Select monitor to remove",
        ignoreFocusOut: true,
      });
      if (!picked) {
        return;
      }
      await session.customRequest("pyrungRemoveMonitor", { id: picked.monitor.id });
      await refreshMonitorStatus(session);
    } catch (error) {
      vscode.window.showErrorMessage(String(error));
    }
  };

  const findLabel = async () => {
    const session = requireSession();
    if (!session) {
      return;
    }
    const label = await vscode.window.showInputBox({
      prompt: "Snapshot label",
      placeHolder: "fault_triggered",
      ignoreFocusOut: true,
    });
    if (!label || !label.trim()) {
      return;
    }
    try {
      const response = await session.customRequest("pyrungFindLabel", {
        label: label.trim(),
        all: true,
      });
      const matches = Array.isArray(response?.matches) ? response.matches : [];
      if (!matches.length) {
        vscode.window.showInformationMessage(`No snapshots found for label '${label.trim()}'.`);
        return;
      }
      const top = matches[matches.length - 1];
      vscode.window.showInformationMessage(
        `Label '${label.trim()}' latest at scan ${top.scanId}, t=${top.timestamp}.`
      );
    } catch (error) {
      vscode.window.showErrorMessage(String(error));
    }
  };

  const monitorMenu = async () => {
    const picked = await vscode.window.showQuickPick(
      [
        { label: "Add Monitor", action: "add" },
        { label: "Remove Monitor", action: "remove" },
        { label: "Find Label", action: "find" },
      ],
      { placeHolder: "pyrung debugger actions", ignoreFocusOut: true }
    );
    if (!picked) {
      return;
    }
    if (picked.action === "add") {
      await addMonitor();
    } else if (picked.action === "remove") {
      await removeMonitor();
    } else if (picked.action === "find") {
      await findLabel();
    }
  };

  context.subscriptions.push(decorator);
  context.subscriptions.push(output);
  context.subscriptions.push(monitorStatus);
  context.subscriptions.push(
    vscode.languages.registerInlineValuesProvider(
      { language: "python", scheme: "file" },
      inlineValuesProvider
    )
  );

  context.subscriptions.push(
    vscode.debug.registerDebugAdapterDescriptorFactory(
      "pyrung",
      new PyrungAdapterFactory()
    )
  );

  // ---- Graph scoping: fetch graph filtered to active editor file ----
  let _lastGraphFile = null;

  function fetchGraphForActiveFile(debugSession) {
    if (!debugSession) return;
    const editor = vscode.window.activeTextEditor;
    const sourceFile = editor ? editor.document.uri.fsPath : undefined;
    // No active text editor (e.g. focus on webview) — keep current graph
    if (!sourceFile) return;
    if (sourceFile === _lastGraphFile) return;
    _lastGraphFile = sourceFile;
    debugSession
      .customRequest("pyrungGraph", { sourceFile })
      .then((data) => {
        graphPanel.updateGraph(data);
        dataView.updateGraph(data);
      })
      .catch(() => {});
  }

  context.subscriptions.push(
    vscode.window.onDidChangeActiveTextEditor(() => {
      const debugSession = vscode.debug.activeDebugSession;
      if (debugSession && isPyrungSession(debugSession)) {
        fetchGraphForActiveFile(debugSession);
      }
    })
  );

  context.subscriptions.push(
    vscode.debug.registerDebugAdapterTrackerFactory("pyrung", {
      createDebugAdapterTracker(session) {
        if (isPyrungSession(session)) {
          sessionExecutionState.set(session.id, "unknown");
          refreshMonitorStatus(session);
        }
        return {
          onWillReceiveMessage: (message) => {
            if (message?.type !== "request") {
              return;
            }
            const command = message.command;
            if (typeof command !== "string") {
              return;
            }

            if (requestLogCommands.has(command)) {
              output.appendLine(`[dap->] ${command}`);
            }

            if (command === "continue") {
              sessionExecutionState.set(session.id, "running");
              output.appendLine(`[state] ${session.id} -> running (request: continue)`);
            }
          },
          onDidSendMessage: (message) => {
            if (message?.type === "response") {
              const command = typeof message.command === "string" ? message.command : "";
              if (requestLogCommands.has(command)) {
                const suffix = message.success
                  ? "ok"
                  : `error: ${message.message || "unknown failure"}`;
                output.appendLine(`[dap<-] ${command} ${suffix}`);
              }
            }

            decorator.handleAdapterMessage(message);

            // Auto-fetch graph data when configurationDone succeeds
            if (
              message?.type === "response" &&
              message.command === "configurationDone" &&
              message.success
            ) {
              fetchGraphForActiveFile(session);
            }

            if (message?.type !== "event") {
              return;
            }

            if (message.event === "pyrungScanFrame") {
              const body = message.body || {};
              const trace = body.trace || {};
              historyPanel.updateHints(trace.tagHints || {});
              historyPanel.appendLiveChanges(body.changes || [], body.scanId);
              if (trace.tagValues) {
                dataView.updateTrace(
                  trace.tagValues,
                  trace.forces || {},
                  trace.tagTypes || {},
                  trace.tagGroups || {},
                  trace.tagHints || {}
                );
                graphPanel.updateTrace(trace.tagValues, trace.forces || {});
              }
              for (const m of body.monitors || []) {
                output.appendLine(
                  `[scan ${m.scanId}] ${m.tag}: ${m.previous} -> ${m.current}`
                );
              }
              for (const s of body.snapshots || []) {
                output.appendLine(`[scan ${s.scanId}] snapshot: ${s.label}`);
              }
              for (const o of body.outputs || []) {
                output.appendLine(o.replace(/\n$/, ""));
              }
            } else if (message.event === "pyrungTrace") {
              const body = message.body || {};
              historyPanel.updateHints(body.tagHints || {});
              if (body.tagValues) {
                dataView.updateTrace(
                  body.tagValues,
                  body.forces || {},
                  body.tagTypes || {},
                  body.tagGroups || {},
                  body.tagHints || {}
                );
                graphPanel.updateTrace(body.tagValues, body.forces || {});
              }
            } else if (message.event === "pyrungMonitor") {
              const body = message.body || {};
              output.appendLine(
                `[scan ${body.scanId}] ${body.tag}: ${body.previous} -> ${body.current}`
              );
            } else if (message.event === "pyrungSnapshot") {
              const body = message.body || {};
              output.appendLine(`[scan ${body.scanId}] snapshot: ${body.label}`);
            } else if (message.event === "stopped") {
              sessionExecutionState.set(session.id, "stopped");
              output.appendLine(
                `[state] ${session.id} -> stopped (${message.body?.reason || "unknown"})`
              );
              historyPanel.setSession(session);
              historyPanel.refresh();
              dataView.setSession(session);
            } else if (message.event === "continued") {
              sessionExecutionState.set(session.id, "running");
              output.appendLine(`[state] ${session.id} -> running (event: continued)`);
            } else if (message.event === "terminated" || message.event === "exited") {
              sessionExecutionState.delete(session.id);
              output.appendLine(`[state] ${session.id} -> terminated`);
              setMonitorStatus(0);
              historyPanel.setSession(null);
              dataView.setSession(null);
              graphPanel.dispose();
            }
          },
        };
      },
    })
  );

  context.subscriptions.push(
    vscode.window.onDidChangeVisibleTextEditors(() => decorator.renderVisibleEditors())
  );
  context.subscriptions.push(
    vscode.debug.onDidStartDebugSession((session) => {
      if (isPyrungSession(session)) {
        sessionExecutionState.set(session.id, "unknown");
        refreshMonitorStatus(session);
      }
    })
  );
  context.subscriptions.push(
    vscode.debug.onDidTerminateDebugSession((session) => {
      if (isPyrungSession(session)) {
        sessionExecutionState.delete(session.id);
        setMonitorStatus(0);
        historyPanel.setSession(null);
        dataView.setSession(null);
        graphPanel.dispose();
        _lastGraphFile = null;
      }
    })
  );
  context.subscriptions.push(
    vscode.debug.onDidChangeActiveDebugSession((session) => {
      if (isPyrungSession(session)) {
        refreshMonitorStatus(session);
      } else {
        setMonitorStatus(0);
      }
    })
  );
  context.subscriptions.push(vscode.commands.registerCommand("pyrung.addMonitor", addMonitor));
  context.subscriptions.push(vscode.commands.registerCommand("pyrung.removeMonitor", removeMonitor));
  context.subscriptions.push(vscode.commands.registerCommand("pyrung.findLabel", findLabel));
  context.subscriptions.push(vscode.commands.registerCommand("pyrung.monitorMenu", monitorMenu));
  context.subscriptions.push(
    vscode.commands.registerCommand("pyrung.openGraph", () => {
      const session = requireSession();
      if (!session) return;
      graphPanel.show(session);
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("pyrung.addToDataView", () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const selection = editor.selection;

      if (selection.isEmpty) {
        // Single cursor: add the tag under the cursor
        const wordRange = editor.document.getWordRangeAtPosition(
          selection.active,
          /[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])?(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])?)*/
        );
        if (!wordRange) return;
        const word = editor.document.getText(wordRange);
        dataView.addTag(lookupName(word));
      } else {
        // Selection: add tags only from lines inside Rung blocks
        const rungLines = new Set();
        let rungIndent = -1;
        for (let lineIdx = selection.start.line; lineIdx <= selection.end.line; lineIdx++) {
          const text = editor.document.lineAt(lineIdx).text;
          const indent = text.search(/\S/);
          if (indent === -1) continue;
          if (/^\s*with\s+Rung\s*\(/.test(text)) {
            rungIndent = indent;
            rungLines.add(lineIdx);
          } else if (rungIndent >= 0 && indent > rungIndent) {
            rungLines.add(lineIdx);
          } else {
            rungIndent = -1;
          }
        }

        const added = new Set();
        for (const lineIdx of rungLines) {
          const sourceLine = editor.document.lineAt(lineIdx).text;
          const code = stripCommentsAndStrings(sourceLine);
          const refs = extractReferences(code);
          for (const ref of refs) {
            if (!isLookupCandidate(code, ref.name, ref.startCol, ref.endCol)) continue;
            const name = lookupName(ref.name);
            if (added.has(name)) continue;
            added.add(name);
            dataView.addTag(name);
          }
        }
      }
    })
  );
};

exports.deactivate = function () {};
