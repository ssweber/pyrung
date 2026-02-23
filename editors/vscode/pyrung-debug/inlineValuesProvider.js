const vscode = require("vscode");

const IGNORED_INLINE_IDENTIFIERS = new Set([
  "False",
  "None",
  "Program",
  "Rung",
  "True",
  "all_of",
  "any_of",
  "and",
  "as",
  "branch",
  "break",
  "call",
  "class",
  "continue",
  "copy",
  "count_down",
  "count_up",
  "def",
  "elif",
  "else",
  "except",
  "fall",
  "finally",
  "for",
  "from",
  "if",
  "import",
  "in",
  "is",
  "nc",
  "not",
  "or",
  "out",
  "pass",
  "raise",
  "reset",
  "return",
  "rise",
  "setpoint",
  "subroutine",
  "try",
  "while",
  "with",
]);

class PyrungInlineValuesProvider {
  constructor(options = {}) {
    this._getConditionLinesForDocument =
      typeof options.getConditionLinesForDocument === "function"
        ? options.getConditionLinesForDocument
        : () => new Set();
  }

  provideInlineValues(document, viewPort, context, _token) {
    const activeSession = vscode.debug.activeDebugSession;
    if (!activeSession || activeSession.type !== "pyrung" || !context || !context.stoppedLocation) {
      return [];
    }

    const values = [];
    const seen = new Set();
    const conditionLines = this._getConditionLinesForDocument(document);
    const startLine = Math.max(0, viewPort.start.line);
    const endLine = Math.min(document.lineCount - 1, viewPort.end.line);

    for (let lineIdx = startLine; lineIdx <= endLine; lineIdx += 1) {
      if (conditionLines.has(lineIdx + 1)) {
        continue;
      }

      const sourceLine = document.lineAt(lineIdx).text;
      const code = this._stripCommentsAndStrings(sourceLine);
      const references = this._extractReferences(code);
      for (const reference of references) {
        const key = `${lineIdx}:${reference.startCol}:${reference.name}`;
        if (
          !seen.has(key) &&
          this._isLookupCandidate(code, reference.name, reference.startCol, reference.endCol)
        ) {
          values.push(
            new vscode.InlineValueVariableLookup(
              new vscode.Range(lineIdx, reference.startCol, lineIdx, reference.endCol),
              reference.name,
              true
            )
          );
          seen.add(key);
        }
      }
    }

    return values;
  }

  _stripCommentsAndStrings(line) {
    let text = line;
    text = text.replace(/(["'])(?:\\.|(?!\1).)*\1/g, " ");
    text = text.replace(/#.*$/, "");
    return text;
  }

  _extractReferences(line) {
    const references = [];
    const regex =
      /\b[A-Za-z_][A-Za-z0-9_]*(?:\[[0-9]+\])?(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\[[0-9]+\])?)*\b/g;
    let match = regex.exec(line);
    while (match) {
      const name = match[0];
      const startCol = match.index;
      references.push({
        name,
        startCol,
        endCol: startCol + name.length,
      });
      match = regex.exec(line);
    }
    return references;
  }

  _rootIdentifier(name) {
    const match = /^[A-Za-z_][A-Za-z0-9_]*/.exec(name);
    return match ? match[0] : name;
  }

  _isLookupCandidate(line, name, startCol, endCol) {
    const root = this._rootIdentifier(name);
    if (IGNORED_INLINE_IDENTIFIERS.has(root)) {
      return false;
    }

    const trailing = line.slice(endCol).trimStart();
    if (trailing.startsWith("(")) {
      return false;
    }
    if (
      trailing.startsWith("=") &&
      !trailing.startsWith("==") &&
      !trailing.startsWith("!=") &&
      !trailing.startsWith("<=") &&
      !trailing.startsWith(">=")
    ) {
      return false;
    }

    return true;
  }
}

module.exports = {
  PyrungInlineValuesProvider,
};
