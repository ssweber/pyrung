const path = require("path");
const vscode = require("vscode");

// --- Configuration ---
// Centralized settings to easily adjust colors, borders, and opacities
const DECORATION_SETTINGS = {
  step: {
    isWholeLine: true,
    borderWidth: "0 0 0 10px",
    borderStyle: "double",
    borderColor: "debugIcon.stepOverForeground",
    overviewRulerColor: "debugIcon.stepOverForeground",
    overviewRulerLane: vscode.OverviewRulerLane.Full,
  },
  enabled: {
    isWholeLine: true,
    borderWidth: "0 0 0 3px",
    borderStyle: "solid",
    borderColor: "testing.iconPassed", // Green
  },
  disabled: {
    isWholeLine: true,
    borderWidth: "0 0 0 3px",
    borderStyle: "solid",
    borderColor: "editorGhostText.foreground",
    opacity: "0.75",
  },
  conditionTrue: {
    margin: "0 0 0 2em",
    color: "testing.iconPassed",
    fontWeight: "500",
  },
  conditionFalse: {
    margin: "0 0 0 2em",
    color: "testing.iconFailed",
    fontWeight: "500",
  },
  conditionSkipped: {
    margin: "0 0 0 2em",
    color: "testing.iconSkipped",
    fontStyle: "italic",
  },
};

class PyrungDecorationController {
  constructor() {
    this._lastTrace = null;
    this._pathCache = new Map();

    this._stepDecoration = vscode.window.createTextEditorDecorationType({
      ...DECORATION_SETTINGS.step,
      borderColor: new vscode.ThemeColor(DECORATION_SETTINGS.step.borderColor),
      overviewRulerColor: new vscode.ThemeColor(DECORATION_SETTINGS.step.overviewRulerColor),
    });

    this._enabledDecoration = vscode.window.createTextEditorDecorationType({
      ...DECORATION_SETTINGS.enabled,
      borderColor: new vscode.ThemeColor(DECORATION_SETTINGS.enabled.borderColor),
    });

    this._disabledDecoration = vscode.window.createTextEditorDecorationType({
      ...DECORATION_SETTINGS.disabled,
      borderColor: new vscode.ThemeColor(DECORATION_SETTINGS.disabled.borderColor),
    });

    this._conditionTrueDecoration = vscode.window.createTextEditorDecorationType({
      after: {
        ...DECORATION_SETTINGS.conditionTrue,
        color: new vscode.ThemeColor(DECORATION_SETTINGS.conditionTrue.color),
      },
    });

    this._conditionFalseDecoration = vscode.window.createTextEditorDecorationType({
      after: {
        ...DECORATION_SETTINGS.conditionFalse,
        color: new vscode.ThemeColor(DECORATION_SETTINGS.conditionFalse.color),
      },
    });

    this._conditionSkippedDecoration = vscode.window.createTextEditorDecorationType({
      after: {
        ...DECORATION_SETTINGS.conditionSkipped,
        color: new vscode.ThemeColor(DECORATION_SETTINGS.conditionSkipped.color),
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
    if (message?.type !== "event") {
      return;
    }

    switch (message.event) {
      case "pyrungTrace":
        this._lastTrace = message.body || null;
        this._renderVisibleEditors();
        break;
      case "stopped":
        if (message.body?.reason === "entry") {
          this.clear();
        }
        break;
      case "terminated":
      case "exited":
        this.clear();
        break;
    }
  }

  renderVisibleEditors() {
    this._renderVisibleEditors();
  }

  clear() {
    this._lastTrace = null;
    this._pathCache.clear();
    this._renderVisibleEditors();
  }

  conditionLinesForDocument(document) {
    const docPath = this._normalizePath(document.fileName);
    const lines = new Set();
    const trace = this._lastTrace;

    if (!trace || !docPath) {
      return lines;
    }

    const regions = trace.regions || [];
    for (const region of regions) {
      const regionSourcePath = region.source?.path;
      const source = regionSourcePath ? this._normalizePath(regionSourcePath) : null;

      if (source !== docPath) continue;

      const conditions = region.conditions || [];
      for (const condition of conditions) {
        const condSourcePath = condition.source?.path;
        const condSource = (condSourcePath && condSourcePath !== regionSourcePath)
          ? this._normalizePath(condSourcePath)
          : source;

        if (condSource !== docPath) continue;

        const line = Number(condition.line);
        if (Number.isFinite(line)) {
          lines.add(Math.trunc(line));
        }
      }
    }
    return lines;
  }

  _renderVisibleEditors() {
    for (const editor of vscode.window.visibleTextEditors) {
      this._applyToEditor(editor);
    }
  }

  _applyToEditor(editor) {
    const trace = this._lastTrace;
    if (!trace) {
      editor.setDecorations(this._stepDecoration, []);
      editor.setDecorations(this._enabledDecoration, []);
      editor.setDecorations(this._disabledDecoration, []);
      editor.setDecorations(this._conditionTrueDecoration, []);
      editor.setDecorations(this._conditionFalseDecoration, []);
      editor.setDecorations(this._conditionSkippedDecoration, []);
      return;
    }

    const docPath = this._normalizePath(editor.document.fileName);
    const stepRanges = [];
    const enabledRanges = [];
    const disabledRanges = [];
    const conditionLines = new Map();
    const conditionBuckets = {
      true: new Map(),
      false: new Map(),
      skipped: new Map(),
    };

    const stepSourcePath = trace.step?.source?.path;
    const stepSource = stepSourcePath ? this._normalizePath(stepSourcePath) : null;

    if (stepSource === docPath) {
      const range = this._lineRange(editor.document, trace.step.line, trace.step.endLine);
      if (range) stepRanges.push(range);
    }

    const regions = trace.regions || [];
    for (const region of regions) {
      const regionSourcePath = region.source?.path;
      const source = regionSourcePath ? this._normalizePath(regionSourcePath) : null;
      if (source !== docPath) continue;

      const range = this._lineRange(editor.document, region.line, region.endLine);
      if (range) {
        if (region.enabledState === "enabled") {
          enabledRanges.push(range);
        } else {
          disabledRanges.push(range);
        }
      }

      const conditions = region.conditions || [];
      for (const condition of conditions) {
        const condSourcePath = condition.source?.path;
        const condSource = (condSourcePath && condSourcePath !== regionSourcePath)
          ? this._normalizePath(condSourcePath)
          : source;

        if (condSource !== docPath) continue;

        const line = this._safeLine(editor.document, condition.line);
        if (line === null) continue;

        const status = condition.status || "true";
        const entry = conditionLines.get(line) || { texts: [], hasFalse: false, hasSkipped: false };

        entry.texts.push(this._conditionText(condition));

        if (status === "false") {
          entry.hasFalse = true;
        } else if (status === "skipped") {
          entry.hasSkipped = true;
        }

        conditionLines.set(line, entry);
      }
    }

    for (const [line, entry] of conditionLines.entries()) {
      if (!entry.texts.length) continue;

      let bucket = conditionBuckets.true;
      if (entry.hasFalse) {
        bucket = conditionBuckets.false;
      } else if (entry.hasSkipped) {
        bucket = conditionBuckets.skipped;
      }
      bucket.set(line, entry.texts);
    }

    editor.setDecorations(this._stepDecoration, stepRanges);
    editor.setDecorations(this._enabledDecoration, enabledRanges);
    editor.setDecorations(this._disabledDecoration, disabledRanges);
    editor.setDecorations(this._conditionTrueDecoration, this._annotationOptions(conditionBuckets.true));
    editor.setDecorations(this._conditionFalseDecoration, this._annotationOptions(conditionBuckets.false));
    editor.setDecorations(this._conditionSkippedDecoration, this._annotationOptions(conditionBuckets.skipped));
  }

  _conditionText(condition) {
    const annotation = typeof condition.annotation === "string" ? condition.annotation.trim() : "";
    if (annotation) {
      return annotation;
    }

    const expression = condition.expression || "condition";
    const status = condition.status || "unknown";
    if (status === "skipped") {
      return `[SKIP] ${expression}`;
    }
    const label = status === "false" ? "F" : "T";
    return `[${label}] ${expression}`;
  }

  _annotationOptions(lineMap) {
    const options = [];
    for (const [line, texts] of lineMap.entries()) {
      if (!texts.length) continue;

      const lineIdx = line - 1;

      // Use Number.MAX_VALUE to snap to the end of the line without querying the document
      options.push({
        range: new vscode.Range(lineIdx, Number.MAX_VALUE, lineIdx, Number.MAX_VALUE),
        renderOptions: {
          after: {
            contentText: `  ${texts.join(" ; ")}`,
          },
        },
      });
    }
    return options;
  }

  _normalizePath(filePath) {
    if (!filePath) return null;

    if (this._pathCache.has(filePath)) {
      return this._pathCache.get(filePath);
    }

    const normalized = path.normalize(filePath);
    const finalPath = process.platform === "win32" ? normalized.toLowerCase() : normalized;

    this._pathCache.set(filePath, finalPath);
    return finalPath;
  }

  _safeLine(document, line) {
    const lineNumber = Number(line);
    if (!Number.isFinite(lineNumber)) {
      return null;
    }
    return Math.max(1, Math.min(document.lineCount, Math.trunc(lineNumber)));
  }

  _lineRange(document, line, endLine) {
    const start = this._safeLine(document, line);
    if (start === null) return null;

    const end = this._safeLine(document, endLine ?? line);
    if (end === null) return null;

    const startIdx = Math.min(start, end) - 1;
    const endIdx = Math.max(start, end) - 1;

    // Return a single range. VS Code automatically clamps Number.MAX_VALUE to the end of the line.
    return new vscode.Range(startIdx, 0, endIdx, Number.MAX_VALUE);
  }
}

module.exports = {
  PyrungDecorationController,
};