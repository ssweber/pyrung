const vscode = require("vscode");

const { PyrungAdapterFactory } = require("./adapterFactory");
const { PyrungDecorationController } = require("./decorationController");
const { PyrungInlineValuesProvider } = require("./inlineValuesProvider");

function isPyrungSession(session) {
  return Boolean(session) && session.type === "pyrung";
}

function activePyrungSession() {
  const session = vscode.debug.activeDebugSession;
  return isPyrungSession(session) ? session : null;
}

exports.activate = function (context) {
  const decorator = new PyrungDecorationController();
  const inlineValuesProvider = new PyrungInlineValuesProvider({
    getConditionLinesForDocument: (document) => decorator.conditionLinesForDocument(document),
  });
  const output = vscode.window.createOutputChannel("pyrung: Debug Events");
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

  context.subscriptions.push(
    vscode.debug.registerDebugAdapterTrackerFactory("pyrung", {
      createDebugAdapterTracker(session) {
        if (isPyrungSession(session)) {
          refreshMonitorStatus(session);
        }
        return {
          onDidSendMessage: (message) => {
            decorator.handleAdapterMessage(message);

            if (message?.type !== "event") {
              return;
            }

            if (message.event === "pyrungMonitor") {
              const body = message.body || {};
              output.appendLine(
                `[scan ${body.scanId}] ${body.tag}: ${body.previous} -> ${body.current}`
              );
            } else if (message.event === "pyrungSnapshot") {
              const body = message.body || {};
              output.appendLine(`[scan ${body.scanId}] snapshot: ${body.label}`);
            } else if (message.event === "terminated" || message.event === "exited") {
              setMonitorStatus(0);
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
        refreshMonitorStatus(session);
      }
    })
  );
  context.subscriptions.push(
    vscode.debug.onDidTerminateDebugSession((session) => {
      if (isPyrungSession(session)) {
        setMonitorStatus(0);
      }
    })
  );
  context.subscriptions.push(vscode.commands.registerCommand("pyrung.addMonitor", addMonitor));
  context.subscriptions.push(vscode.commands.registerCommand("pyrung.removeMonitor", removeMonitor));
  context.subscriptions.push(vscode.commands.registerCommand("pyrung.findLabel", findLabel));
  context.subscriptions.push(vscode.commands.registerCommand("pyrung.monitorMenu", monitorMenu));
};

exports.deactivate = function () {};
