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
        stepRanges.push(...this._lineRanges(editor.document, step.line, step.endLine));
      }

      const regions = Array.isArray(trace.regions) ? trace.regions : [];
      for (const region of regions) {
        const source = region && region.source && region.source.path ? this._normalizePath(region.source.path) : null;
        if (!source || source !== docPath) {
          continue;
        }
        const regionRanges = this._lineRanges(editor.document, region.line, region.endLine);
        if (region.enabledState === "enabled") {
          enabledRanges.push(...regionRanges);
        } else {
          disabledRanges.push(...regionRanges);
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
            contentText: `  ${texts.join(" & ")}`,
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
      return `[SKIP] ${expression}`;
    }

    const statusLabel = status === "false" ? "F" : "T";
    const details = this._conditionDetailMap(condition.details);
    const summary = this._conditionDetailSummary(expression, details);
    if (!summary) {
      return `[${statusLabel}] ${expression}`;
    }
    return `[${statusLabel}] ${expression} (${summary})`;
  }

  _conditionDetailMap(details) {
    const map = new Map();
    if (!Array.isArray(details)) {
      return map;
    }
    for (const detail of details) {
      if (!detail || typeof detail.name !== "string") {
        continue;
      }
      map.set(detail.name, this._normalizeValue(detail.value));
    }
    return map;
  }

  _conditionDetailSummary(expression, details) {
    if (details.has("left") && details.has("left_value")) {
      const left = String(details.get("left"));
      const leftValue = details.get("left_value");
      if (details.has("right_value")) {
        const rightValue = details.get("right_value");
        return `${left}=${leftValue}, rhs=${rightValue}`;
      }
      return `${left}=${leftValue}`;
    }
    if (details.has("tag") && details.has("value")) {
      const tag = String(details.get("tag"));
      const value = details.get("value");
      if (tag === expression) {
        return `value=${value}`;
      }
      return `${tag}=${value}`;
    }
    if (details.has("current") || details.has("previous")) {
      const current = details.has("current") ? details.get("current") : "?";
      const previous = details.has("previous") ? details.get("previous") : "?";
      return `current=${current}, previous=${previous}`;
    }
    if (details.has("terms")) {
      return String(details.get("terms"));
    }
    return "";
  }

  _normalizeValue(value) {
    if (value === undefined || value === null) {
      return "?";
    }
    const text = String(value);
    if (text === "True") {
      return "true";
    }
    if (text === "False") {
      return "false";
    }
    return text;
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

  _lineRanges(document, line, endLine) {
    const start = this._safeLine(document, line);
    if (start === null) {
      return [];
    }
    const end = this._safeLine(document, endLine === undefined || endLine === null ? line : endLine);
    if (end === null) {
      return [];
    }
    const startIdx = Math.min(start, end) - 1;
    const endIdx = Math.max(start, end) - 1;
    const ranges = [];
    for (let lineIdx = startIdx; lineIdx <= endIdx; lineIdx += 1) {
      const endPos = document.lineAt(lineIdx).range.end;
      ranges.push(new vscode.Range(lineIdx, 0, lineIdx, endPos.character));
    }
    return ranges;
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
