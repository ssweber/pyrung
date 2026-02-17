const path = require("path");
const vscode = require("vscode");

class PyrungAdapterFactory {
  createDebugAdapterDescriptor(session) {
    const config = session.configuration;
    const python = config.pythonPath || "python";
    return new vscode.DebugAdapterExecutable(python, ["-m", "pyrung.dap"]);
  }
}

class PyrungDecorationController {
  constructor() {
    this._lastTrace = null;
    this._stepDecoration = vscode.window.createTextEditorDecorationType({
      isWholeLine: true,
      backgroundColor: "rgba(255, 196, 0, 0.14)",
      borderWidth: "1px",
      borderStyle: "solid",
      borderColor: "rgba(255, 196, 0, 0.45)",
    });
    this._enabledDecoration = vscode.window.createTextEditorDecorationType({
      isWholeLine: true,
      backgroundColor: "rgba(40, 167, 69, 0.11)",
    });
    this._disabledDecoration = vscode.window.createTextEditorDecorationType({
      isWholeLine: true,
      backgroundColor: "rgba(128, 128, 128, 0.12)",
    });
    this._conditionTrueDecoration = vscode.window.createTextEditorDecorationType({
      after: {
        margin: "0 0 0 1.5em",
        color: "rgba(40, 167, 69, 0.95)",
      },
    });
    this._conditionFalseDecoration = vscode.window.createTextEditorDecorationType({
      after: {
        margin: "0 0 0 1.5em",
        color: "rgba(220, 53, 69, 0.95)",
      },
    });
    this._conditionSkippedDecoration = vscode.window.createTextEditorDecorationType({
      after: {
        margin: "0 0 0 1.5em",
        color: "rgba(128, 128, 128, 0.95)",
        fontStyle: "italic",
      },
    });
  }

  dispose() {
    this._stepDecoration.dispose();
    this._enabledDecoration.dispose();
    this._disabledDecoration.dispose();
    this._conditionTrueDecoration.dispose();
    this._conditionFalseDecoration.dispose();
    this._conditionSkippedDecoration.dispose();
  }

  handleAdapterMessage(message) {
    if (!message || message.type !== "event") {
      return;
    }
    if (message.event === "pyrungTrace") {
      this._lastTrace = message.body || null;
      this._renderVisibleEditors();
      return;
    }
    if (message.event === "stopped" && message.body && message.body.reason === "entry") {
      this.clear();
      return;
    }
    if (message.event === "terminated" || message.event === "exited") {
      this.clear();
    }
  }

  renderVisibleEditors() {
    this._renderVisibleEditors();
  }

  clear() {
    this._lastTrace = null;
    this._renderVisibleEditors();
  }

  _renderVisibleEditors() {
    for (const editor of vscode.window.visibleTextEditors) {
      this._applyToEditor(editor);
    }
  }

  _applyToEditor(editor) {
    const stepRanges = [];
    const enabledRanges = [];
    const disabledRanges = [];
    const conditionBuckets = {
      true: new Map(),
      false: new Map(),
      skipped: new Map(),
    };

    const trace = this._lastTrace;
    if (trace) {
      const docPath = this._normalizePath(editor.document.fileName);
      const step = trace.step || {};
      const stepSource = step.source && step.source.path ? this._normalizePath(step.source.path) : null;
      if (stepSource && stepSource === docPath) {
        const range = this._lineRange(editor.document, step.line, step.endLine);
        if (range) {
          stepRanges.push(range);
        }
      }

      const regions = Array.isArray(trace.regions) ? trace.regions : [];
      for (const region of regions) {
        const source = region && region.source && region.source.path ? this._normalizePath(region.source.path) : null;
        if (!source || source !== docPath) {
          continue;
        }
        const regionRange = this._lineRange(editor.document, region.line, region.endLine);
        if (regionRange) {
          if (region.enabledState === "enabled") {
            enabledRanges.push(regionRange);
          } else {
            disabledRanges.push(regionRange);
          }
        }

        const conditions = Array.isArray(region.conditions) ? region.conditions : [];
        for (const condition of conditions) {
          const condSource = condition && condition.source && condition.source.path
            ? this._normalizePath(condition.source.path)
            : source;
          if (!condSource || condSource !== docPath) {
            continue;
          }
          const line = this._safeLine(editor.document, condition.line);
          if (line === null) {
            continue;
          }
          const status = condition.status === "false" ? "false" : condition.status === "skipped" ? "skipped" : "true";
          const text = this._formatCondition(condition);
          const bucket = conditionBuckets[status];
          if (!bucket.has(line)) {
            bucket.set(line, []);
          }
          bucket.get(line).push(text);
        }
      }
    }

    editor.setDecorations(this._stepDecoration, stepRanges);
    editor.setDecorations(this._enabledDecoration, enabledRanges);
    editor.setDecorations(this._disabledDecoration, disabledRanges);
    editor.setDecorations(this._conditionTrueDecoration, this._annotationOptions(editor.document, conditionBuckets.true));
    editor.setDecorations(
      this._conditionFalseDecoration,
      this._annotationOptions(editor.document, conditionBuckets.false)
    );
    editor.setDecorations(
      this._conditionSkippedDecoration,
      this._annotationOptions(editor.document, conditionBuckets.skipped)
    );
  }

  _annotationOptions(document, lineMap) {
    const options = [];
    for (const [line, texts] of lineMap.entries()) {
      if (!texts.length) {
        continue;
      }
      const lineIdx = line - 1;
      const endCol = document.lineAt(lineIdx).text.length;
      options.push({
        range: new vscode.Range(lineIdx, endCol, lineIdx, endCol),
        renderOptions: {
          after: {
            contentText: `  ${texts.join(" | ")}`,
          },
        },
      });
    }
    return options;
  }

  _formatCondition(condition) {
    const expression = condition.expression || "condition";
    const status = condition.status || "unknown";
    if (status === "skipped") {
      return `${expression} -> skipped`;
    }

    const value = condition.value === undefined ? "?" : String(condition.value);
    const details = Array.isArray(condition.details) ? condition.details : [];
    if (!details.length) {
      return `${expression} -> ${value}`;
    }

    const detailText = details
      .map((detail) => `${detail.name}=${detail.value}`)
      .join(", ");
    return `${expression} -> ${value} (${detailText})`;
  }

  _normalizePath(filePath) {
    if (!filePath) {
      return null;
    }
    const normalized = path.normalize(filePath);
    if (process.platform === "win32") {
      return normalized.toLowerCase();
    }
    return normalized;
  }

  _safeLine(document, line) {
    const lineNumber = Number(line);
    if (!Number.isFinite(lineNumber)) {
      return null;
    }
    const clamped = Math.max(1, Math.min(document.lineCount, Math.trunc(lineNumber)));
    return clamped;
  }

  _lineRange(document, line, endLine) {
    const start = this._safeLine(document, line);
    if (start === null) {
      return null;
    }
    const end = this._safeLine(document, endLine === undefined || endLine === null ? line : endLine);
    if (end === null) {
      return null;
    }
    const startIdx = Math.min(start, end) - 1;
    const endIdx = Math.max(start, end) - 1;
    const endCol = document.lineAt(endIdx).text.length;
    return new vscode.Range(startIdx, 0, endIdx, endCol);
  }
}

exports.activate = function (context) {
  const decorator = new PyrungDecorationController();
  context.subscriptions.push(decorator);

  context.subscriptions.push(
    vscode.debug.registerDebugAdapterDescriptorFactory(
      "pyrung",
      new PyrungAdapterFactory()
    )
  );

  context.subscriptions.push(
    vscode.debug.registerDebugAdapterTrackerFactory("pyrung", {
      createDebugAdapterTracker() {
        return {
          onDidSendMessage: (message) => decorator.handleAdapterMessage(message),
        };
      },
    })
  );

  context.subscriptions.push(
    vscode.window.onDidChangeVisibleTextEditors(() => decorator.renderVisibleEditors())
  );
};

exports.deactivate = function () {};
