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
          const text = this._formatCondition(condition);
          const entry = conditionLines.get(line) || { texts: [], hasTrue: false, hasFalse: false, hasSkipped: false };
          entry.texts.push(text);
          if (status === "false") {
            entry.hasFalse = true;
          } else if (status === "skipped") {
            entry.hasSkipped = true;
          } else {
            entry.hasTrue = true;
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

  _formatCondition(condition) {
    const expression = condition.expression || "condition";
    const status = condition.status || "unknown";
    if (status === "skipped") {
      return `[SKIP] ${expression}`;
    }

    const statusLabel = status === "false" ? "F" : "T";
    const details = this._conditionDetailMap(condition.details);
    const compositeSummary = this._compositeConditionSummary(expression, details);
    if (compositeSummary) {
      return compositeSummary;
    }
    const summary = this._conditionDetailSummary(expression, details);
    if (!summary) {
      return `[${statusLabel}] ${expression}`;
    }
    return `[${statusLabel}] ${summary}`;
  }

  _compositeConditionSummary(expression, details) {
    if (!(details instanceof Map) || !details.has("terms")) {
      return "";
    }
    const composite = this._splitCompositeExpression(expression);
    if (!composite) {
      return "";
    }

    const evaluatedParts = this._splitTopLevelByOperator(String(details.get("terms")), composite.operator).map(
      (part) => this._parseEvaluatedCompositeTerm(part)
    );
    if (!evaluatedParts.length) {
      return "";
    }

    const rendered = [];
    for (let i = 0; i < composite.terms.length; i += 1) {
      const evaluated = i < evaluatedParts.length ? evaluatedParts[i] : null;
      if (!evaluated) {
        rendered.push(`[SKIP] ${composite.terms[i]}`);
        continue;
      }
      if (evaluated.status === "skipped") {
        rendered.push(`[SKIP] ${evaluated.text}`);
        continue;
      }
      const label = evaluated.status === "false" ? "F" : "T";
      rendered.push(`[${label}] ${evaluated.text}(${evaluated.status})`);
    }

    if (!rendered.length) {
      return "";
    }
    return rendered.join(" ; ");
  }

  _splitCompositeExpression(expression) {
    if (typeof expression !== "string") {
      return null;
    }

    const text = this._stripOuterParentheses(expression.trim());
    if (!text) {
      return null;
    }

    const terms = [];
    let term = "";
    let depth = 0;
    let operator = null;

    for (let i = 0; i < text.length; i += 1) {
      const char = text[i];
      if (char === "(") {
        depth += 1;
        term += char;
        continue;
      }
      if (char === ")") {
        depth = Math.max(0, depth - 1);
        term += char;
        continue;
      }
      if (depth === 0 && (char === "&" || char === "|")) {
        const token = term.trim();
        if (!token) {
          return null;
        }
        terms.push(token);
        term = "";
        if (operator === null) {
          operator = char;
        } else if (operator !== char) {
          return null;
        }
        continue;
      }
      term += char;
    }

    const tail = term.trim();
    if (!operator || !tail) {
      return null;
    }
    terms.push(tail);
    if (terms.length < 2) {
      return null;
    }
    return { operator, terms };
  }

  _splitTopLevelByOperator(text, operator) {
    if (typeof text !== "string" || (operator !== "&" && operator !== "|")) {
      return [];
    }

    const value = this._stripOuterParentheses(text.trim());
    if (!value) {
      return [];
    }

    const terms = [];
    let term = "";
    let depth = 0;
    for (let i = 0; i < value.length; i += 1) {
      const char = value[i];
      if (char === "(") {
        depth += 1;
        term += char;
        continue;
      }
      if (char === ")") {
        depth = Math.max(0, depth - 1);
        term += char;
        continue;
      }
      if (depth === 0 && char === operator) {
        const token = term.trim();
        if (token) {
          terms.push(token);
        }
        term = "";
        continue;
      }
      term += char;
    }
    const tail = term.trim();
    if (tail) {
      terms.push(tail);
    }
    return terms;
  }

  _stripOuterParentheses(text) {
    if (typeof text !== "string") {
      return "";
    }
    let value = text.trim();
    while (value.startsWith("(") && value.endsWith(")")) {
      let depth = 0;
      let wrapsWholeText = true;
      for (let i = 0; i < value.length; i += 1) {
        const char = value[i];
        if (char === "(") {
          depth += 1;
        } else if (char === ")") {
          depth -= 1;
          if (depth === 0 && i < value.length - 1) {
            wrapsWholeText = false;
            break;
          }
        }
      }
      if (!wrapsWholeText || depth !== 0) {
        break;
      }
      value = value.slice(1, -1).trim();
    }
    return value;
  }

  _parseEvaluatedCompositeTerm(text) {
    if (typeof text !== "string") {
      return null;
    }
    const match = text.trim().match(/^(.*)\((true|false|skipped)\)$/i);
    if (!match) {
      return null;
    }
    const rawStatus = match[2].toLowerCase();
    const status = rawStatus === "false" ? "false" : rawStatus === "skipped" ? "skipped" : "true";
    return { text: match[1].trim(), status };
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
    const comparison = this._comparisonParts(expression);
    if (details.has("left") && details.has("left_value")) {
      const leftValue = details.get("left_value");
      const leftLabel = this._leftLabel(details, comparison);
      const leftText = this._observedOperand(leftLabel, leftValue);
      if (comparison) {
        const rightText = this._rightOperandText(comparison, details);
        if (rightText) {
          return `${leftText} ${comparison.operator} ${rightText}`;
        }
      }
      if (details.has("right_value")) {
        return `${leftText}, rhs(${details.get("right_value")})`;
      }
      return leftText;
    }
    if (details.has("tag") && details.has("value")) {
      return this._observedOperand(String(details.get("tag")), details.get("value"));
    }
    if (details.has("current") || details.has("previous")) {
      const tag = details.has("tag") ? String(details.get("tag")) : "value";
      const current = details.has("current") ? details.get("current") : "?";
      const previous = details.has("previous") ? details.get("previous") : "?";
      return `${this._observedOperand(tag, current)} prev(${previous})`;
    }
    if (details.has("terms")) {
      return String(details.get("terms"));
    }
    return "";
  }

  _leftLabel(details, comparison) {
    if (details.has("left")) {
      return String(details.get("left"));
    }
    if (
      details.has("left_pointer_expr") &&
      details.has("left_pointer") &&
      details.has("left_pointer_value")
    ) {
      return this._pointerResolvedLabel(
        String(details.get("left_pointer_expr")),
        String(details.get("left_pointer")),
        details.get("left_pointer_value")
      );
    }
    if (comparison && comparison.left) {
      return comparison.left;
    }
    return "value";
  }

  _rightOperandText(comparison, details) {
    const rightLabel = this._rightLabel(details, comparison);
    if (details.has("right") && details.has("right_value")) {
      return this._observedOperand(rightLabel, details.get("right_value"));
    }
    if (details.has("right_value")) {
      if (this._isLiteralOperand(comparison.right)) {
        return comparison.right;
      }
      return this._observedOperand(rightLabel, details.get("right_value"));
    }
    if (details.has("right")) {
      return rightLabel;
    }
    return comparison.right;
  }

  _rightLabel(details, comparison) {
    if (details.has("right")) {
      return String(details.get("right"));
    }
    if (
      details.has("right_pointer_expr") &&
      details.has("right_pointer") &&
      details.has("right_pointer_value")
    ) {
      return this._pointerResolvedLabel(
        String(details.get("right_pointer_expr")),
        String(details.get("right_pointer")),
        details.get("right_pointer_value")
      );
    }
    return comparison.right;
  }

  _comparisonParts(expression) {
    if (typeof expression !== "string") {
      return null;
    }
    const match = expression.trim().match(/^(.+?)\s*(==|!=|<=|>=|<|>)\s*(.+)$/);
    if (!match) {
      return null;
    }
    return {
      left: this._unwrapIndirectRef(match[1].trim()),
      operator: match[2],
      right: this._unwrapIndirectRef(match[3].trim()),
    };
  }

  _unwrapIndirectRef(text) {
    const match = text.match(/^IndirectRef\((.+)\)$/);
    if (!match) {
      return text;
    }
    return match[1].trim();
  }

  _pointerResolvedLabel(pointerExpr, pointerName, pointerValue) {
    const token = `[${pointerName}]`;
    if (pointerExpr.includes(token)) {
      return pointerExpr.replace(token, `[${pointerName}(${pointerValue})]`);
    }
    const bracketMatch = pointerExpr.match(/^(.+?)\[([^\]]+)\]$/);
    if (bracketMatch) {
      return `${bracketMatch[1]}[${bracketMatch[2]}(${pointerValue})]`;
    }
    return `${pointerExpr}[${pointerName}(${pointerValue})]`;
  }

  _isLiteralOperand(text) {
    if (typeof text !== "string") {
      return false;
    }
    const value = text.trim();
    if (/^[-+]?\d+(\.\d+)?$/.test(value)) {
      return true;
    }
    if (/^(true|false|null|none)$/i.test(value)) {
      return true;
    }
    if ((value.startsWith("'") && value.endsWith("'")) || (value.startsWith('"') && value.endsWith('"'))) {
      return true;
    }
    return false;
  }

  _observedOperand(label, value) {
    return `${label}(${value})`;
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
