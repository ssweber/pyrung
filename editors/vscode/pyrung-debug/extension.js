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

function pyrungSessionById(sessionId) {
  if (!sessionId) {
    return null;
  }
  const session = vscode.debug.sessions.find((candidate) => candidate.id === sessionId);
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
  const rapidStatus = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 99);
  rapidStatus.command = "pyrung.toggleRapidStep";
  rapidStatus.hide();

  const RAPID_MODE_LABEL = {
    next: "next",
    stepIn: "stepIn",
    pyrungStepScan: "scan",
  };
  const RAPID_MANUAL_STOP_COMMANDS = new Set([
    "continue",
    "next",
    "stepIn",
    "pyrungStepScan",
    "stepOut",
    "pause",
    "disconnect",
    "terminate",
  ]);

  const sessionExecutionState = new Map();
  const rapidState = {
    enabled: false,
    modeCommand: "next",
    intervalMs: 100,
    sessionId: null,
    timer: null,
    generation: 0,
    waitingForStop: false,
    internalRequestBudget: new Map(),
  };

  const formatErrorMessage = (error) => {
    if (error instanceof Error && typeof error.message === "string" && error.message.trim()) {
      return error.message.trim();
    }
    return String(error);
  };

  const clearRapidTimer = () => {
    if (rapidState.timer !== null) {
      clearTimeout(rapidState.timer);
      rapidState.timer = null;
    }
  };

  const budgetInternalRequest = (command) => {
    const current = rapidState.internalRequestBudget.get(command) || 0;
    rapidState.internalRequestBudget.set(command, current + 1);
  };

  const consumeInternalRequestBudget = (command) => {
    const current = rapidState.internalRequestBudget.get(command) || 0;
    if (current <= 0) {
      return false;
    }
    if (current === 1) {
      rapidState.internalRequestBudget.delete(command);
    } else {
      rapidState.internalRequestBudget.set(command, current - 1);
    }
    return true;
  };

  const updateRapidStatus = (session = activePyrungSession()) => {
    if (!session) {
      rapidStatus.hide();
      return;
    }
    const modeLabel = RAPID_MODE_LABEL[rapidState.modeCommand] || RAPID_MODE_LABEL.next;
    if (!rapidState.enabled) {
      rapidStatus.text = `R:off ${modeLabel}@${rapidState.intervalMs}ms`;
    } else if (rapidState.waitingForStop) {
      rapidStatus.text = `R:pausing ${modeLabel}@${rapidState.intervalMs}ms`;
    } else {
      rapidStatus.text = `R:on ${modeLabel}@${rapidState.intervalMs}ms`;
    }
    rapidStatus.tooltip = `pyrung rapid step (${modeLabel} every ${rapidState.intervalMs}ms). Click to ${
      rapidState.enabled ? "stop" : "start"
    }.`;
    rapidStatus.show();
  };

  const stopRapidStep = ({ warningMessage } = {}) => {
    clearRapidTimer();
    rapidState.generation += 1;
    rapidState.enabled = false;
    rapidState.waitingForStop = false;
    rapidState.sessionId = null;
    rapidState.internalRequestBudget.clear();
    updateRapidStatus();
    if (warningMessage) {
      vscode.window.showWarningMessage(warningMessage);
    }
  };

  const scheduleRapidStep = (session, generation) => {
    if (!rapidState.enabled || rapidState.generation !== generation || rapidState.sessionId !== session.id) {
      return;
    }

    clearRapidTimer();
    rapidState.timer = setTimeout(async () => {
      if (!rapidState.enabled || rapidState.generation !== generation || rapidState.sessionId !== session.id) {
        return;
      }
      const modeCommand = rapidState.modeCommand;
      try {
        budgetInternalRequest(modeCommand);
        await session.customRequest(modeCommand, { threadId: 1 });
        if (!rapidState.enabled || rapidState.generation !== generation || rapidState.sessionId !== session.id) {
          return;
        }
        scheduleRapidStep(session, generation);
      } catch (error) {
        stopRapidStep({
          warningMessage: `Rapid step stopped (${modeCommand}): ${formatErrorMessage(error)}`,
        });
      }
    }, rapidState.intervalMs);
  };

  const startRapidStep = async () => {
    const session = requireSession();
    if (!session) {
      return;
    }

    clearRapidTimer();
    rapidState.generation += 1;
    const generation = rapidState.generation;
    rapidState.enabled = true;
    rapidState.sessionId = session.id;
    rapidState.waitingForStop = false;
    rapidState.internalRequestBudget.clear();

    const executionState = sessionExecutionState.get(session.id) || "unknown";
    if (executionState === "running") {
      rapidState.waitingForStop = true;
      updateRapidStatus(session);
      try {
        budgetInternalRequest("pause");
        await session.customRequest("pause", { threadId: 1 });
      } catch (error) {
        if (rapidState.enabled && rapidState.generation === generation) {
          stopRapidStep({
            warningMessage: `Rapid step could not pause: ${formatErrorMessage(error)}`,
          });
        }
        return;
      }

      if (!rapidState.enabled || rapidState.generation !== generation || rapidState.sessionId !== session.id) {
        return;
      }

      if ((sessionExecutionState.get(session.id) || "unknown") === "stopped") {
        rapidState.waitingForStop = false;
        updateRapidStatus(session);
        scheduleRapidStep(session, generation);
      }
      return;
    }

    updateRapidStatus(session);
    scheduleRapidStep(session, generation);
  };

  const toggleRapidStep = async () => {
    if (rapidState.enabled) {
      stopRapidStep();
      return;
    }
    await startRapidStep();
  };

  const configureRapidStep = async () => {
    const modeItems = [
      {
        label: "Step Over (next)",
        command: "next",
        description: rapidState.modeCommand === "next" ? "Current" : "",
      },
      {
        label: "Step Into (stepIn)",
        command: "stepIn",
        description: rapidState.modeCommand === "stepIn" ? "Current" : "",
      },
      {
        label: "Step Scan (full scan)",
        command: "pyrungStepScan",
        description: rapidState.modeCommand === "pyrungStepScan" ? "Current" : "",
      },
    ];
    const modePick = await vscode.window.showQuickPick(modeItems, {
      placeHolder: "Rapid step mode",
      ignoreFocusOut: true,
    });
    if (!modePick) {
      return;
    }

    const intervalItems = [50, 100, 200, 500, 1000].map((intervalMs) => ({
      label: `${intervalMs} ms`,
      intervalMs,
      description: rapidState.intervalMs === intervalMs ? "Current" : "",
    }));
    const intervalPick = await vscode.window.showQuickPick(intervalItems, {
      placeHolder: "Rapid step interval",
      ignoreFocusOut: true,
    });
    if (!intervalPick) {
      return;
    }

    rapidState.modeCommand = modePick.command;
    rapidState.intervalMs = intervalPick.intervalMs;

    if (rapidState.enabled) {
      const session = pyrungSessionById(rapidState.sessionId);
      if (!session) {
        stopRapidStep();
        return;
      }

      clearRapidTimer();
      rapidState.generation += 1;
      const generation = rapidState.generation;
      if (!rapidState.waitingForStop) {
        scheduleRapidStep(session, generation);
      }
      updateRapidStatus(session);
      return;
    }

    updateRapidStatus();
  };

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
  context.subscriptions.push(rapidStatus);
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
          sessionExecutionState.set(session.id, "unknown");
          refreshMonitorStatus(session);
          updateRapidStatus();
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

            if (command === "continue") {
              sessionExecutionState.set(session.id, "running");
            }

            if (!rapidState.enabled || rapidState.sessionId !== session.id) {
              return;
            }

            if (!RAPID_MANUAL_STOP_COMMANDS.has(command)) {
              return;
            }

            if (consumeInternalRequestBudget(command)) {
              return;
            }

            stopRapidStep();
          },
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
            } else if (message.event === "stopped") {
              sessionExecutionState.set(session.id, "stopped");
              if (
                rapidState.enabled &&
                rapidState.sessionId === session.id &&
                rapidState.waitingForStop
              ) {
                const trackedSession = pyrungSessionById(session.id);
                if (!trackedSession) {
                  stopRapidStep();
                  return;
                }
                rapidState.waitingForStop = false;
                updateRapidStatus(trackedSession);
                scheduleRapidStep(trackedSession, rapidState.generation);
              }
            } else if (message.event === "continued") {
              sessionExecutionState.set(session.id, "running");
            } else if (message.event === "terminated" || message.event === "exited") {
              sessionExecutionState.delete(session.id);
              if (rapidState.enabled && rapidState.sessionId === session.id) {
                stopRapidStep();
              } else {
                updateRapidStatus();
              }
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
        sessionExecutionState.set(session.id, "unknown");
        refreshMonitorStatus(session);
        updateRapidStatus();
      }
    })
  );
  context.subscriptions.push(
    vscode.debug.onDidTerminateDebugSession((session) => {
      if (isPyrungSession(session)) {
        sessionExecutionState.delete(session.id);
        if (rapidState.enabled && rapidState.sessionId === session.id) {
          stopRapidStep();
        } else {
          updateRapidStatus();
        }
        setMonitorStatus(0);
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
      updateRapidStatus();
    })
  );
  context.subscriptions.push(vscode.commands.registerCommand("pyrung.addMonitor", addMonitor));
  context.subscriptions.push(vscode.commands.registerCommand("pyrung.removeMonitor", removeMonitor));
  context.subscriptions.push(vscode.commands.registerCommand("pyrung.findLabel", findLabel));
  context.subscriptions.push(vscode.commands.registerCommand("pyrung.monitorMenu", monitorMenu));
  context.subscriptions.push(vscode.commands.registerCommand("pyrung.toggleRapidStep", toggleRapidStep));
  context.subscriptions.push(
    vscode.commands.registerCommand("pyrung.configureRapidStep", configureRapidStep)
  );
};

exports.deactivate = function () {};
