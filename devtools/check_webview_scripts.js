// Syntax-check embedded <script> blocks inside JS template literals.
//
// VS Code webview providers build HTML via template literals. A mistake
// like `\"` (which collapses to a bare `"` in a template literal) can
// produce a JavaScript syntax error that silently kills the entire
// webview at runtime.  `node -c` only checks the outer module syntax
// and cannot catch these bugs.
//
// Usage:  node devtools/check_webview_scripts.js [file ...]
//
// With no arguments, checks all *.js files under editors/vscode/pyrung-debug/.

"use strict";

const fs = require("fs");
const path = require("path");

const DEFAULT_DIR = path.resolve(
  __dirname,
  "..",
  "editors",
  "vscode",
  "pyrung-debug"
);

function findScriptBlocks(source) {
  const blocks = [];
  const re = /<script>([\s\S]*?)<\/script>/g;
  let match;
  while ((match = re.exec(source)) !== null) {
    const raw = match[1];
    // Skip blocks that contain template expressions — they need
    // runtime values we don't have.
    if (/\$\{/.test(raw)) continue;
    const lineNumber =
      source.substring(0, match.index).split("\n").length;
    blocks.push({ raw, lineNumber });
  }
  return blocks;
}

function evaluateTemplateEscapes(raw) {
  // The raw text lives inside a JS template literal in the source file.
  // Wrap it back in a template literal and evaluate to resolve escapes
  // (`\\` → `\`, `\"` → `"`, `\uXXXX` → char, etc.).
  return new Function("return `" + raw + "`")();
}

function checkFile(filePath) {
  const source = fs.readFileSync(filePath, "utf8");
  const blocks = findScriptBlocks(source);
  let errors = 0;

  for (const { raw, lineNumber } of blocks) {
    let script;
    try {
      script = evaluateTemplateEscapes(raw);
    } catch (e) {
      console.error(
        `${filePath}:${lineNumber}: template-escape evaluation failed: ${e.message}`
      );
      errors++;
      continue;
    }

    try {
      new Function(script);
    } catch (e) {
      // Map the error back to an approximate source line.
      const errorLine = extractErrorLine(e);
      const approxLine = errorLine !== null ? lineNumber + errorLine : lineNumber;
      console.error(
        `${filePath}:${approxLine}: webview <script> syntax error: ${e.message}`
      );
      errors++;
    }
  }
  return errors;
}

function extractErrorLine(error) {
  // V8 SyntaxError messages sometimes include a line offset for
  // code parsed via new Function().
  const match =
    error.stack && error.stack.match(/<anonymous>:(\d+)/);
  if (match) return parseInt(match[1], 10) - 2; // new Function adds a wrapper line
  return null;
}

// --- main ---

let files = process.argv.slice(2);
if (files.length === 0) {
  files = fs
    .readdirSync(DEFAULT_DIR)
    .filter((f) => f.endsWith(".js"))
    .map((f) => path.join(DEFAULT_DIR, f));
}

let totalErrors = 0;
for (const file of files) {
  totalErrors += checkFile(file);
}

if (totalErrors > 0) {
  process.exit(1);
}
