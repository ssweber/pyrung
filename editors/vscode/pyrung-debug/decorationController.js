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
    borderColor: "editorWarning.foreground", // Yellow
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
      backgroundColor: new vscode.ThemeColor(DECORATION_SETTINGS.disabled.backgroundColor),
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

  conditionLinesForDocument(document) {
    const docPath = this._normalizePath(document.fileName);
    const lines = new Set();
    const trace = this._lastTrace;
    if (!trace || !docPath) {
      return lines;
    }

    const regions = Array.isArray(trace.regions) ? trace.regions : [];
    for (const region of regions) {
      const source = region && region.source && region.source.path ? this._normalizePath(region.source.path) : null;
      if (!source || source !== docPath) {
        continue;
      }
      const conditions = Array.isArray(region.conditions) ? region.conditions : [];
      for (const condition of conditions) {
        const condSource = condition && condition.source && condition.source.path
          ? this._normalizePath(condition.source.path)
          : source;
        if (!condSource || condSource !== docPath) {
          continue;
        }
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
    const stepRanges = [];
    const enabledRanges = [];
    const disabledRanges = [];
    const conditionLines = new Map();
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
          const text = this._conditionText(condition);
          const entry = conditionLines.get(line) || { texts: [], hasFalse: false, hasSkipped: false };
          entry.texts.push(text);
          if (status === "false") {
            entry.hasFalse = true;
          } else if (status === "skipped") {
            entry.hasSkipped = true;
          }
          conditionLines.set(line, entry);
        }
      }
    }

    for (const [line, entry] of conditionLines.entries()) {
      if (!entry.texts.length) {
        continue;
      }
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
            contentText: `  ${texts.join(" ; ")}`,
          },
        },
      });
    }
    return options;
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

module.exports = {
  PyrungDecorationController,
};