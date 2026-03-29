const vscode = require("vscode");

class PyrungHistorySliderProvider {
  constructor() {
    this._view = null;
    this._session = null;
  }

  resolveWebviewView(webviewView) {
    this._view = webviewView;
    webviewView.webview.options = { enableScripts: true };
    webviewView.webview.html = this._html();

    webviewView.webview.onDidReceiveMessage(async (message) => {
      if (message.type === "seek" && this._session) {
        try {
          const result = await this._session.customRequest("pyrungSeek", {
            scanId: message.scanId,
          });
          this._postTags(result.tags || {}, result.scanId, result.timestamp);
        } catch (error) {
          this._view?.webview.postMessage({ type: "error", text: String(error) });
        }
      }
    });

    webviewView.onDidDispose(() => {
      this._view = null;
    });
  }

  setSession(session) {
    this._session = session;
    if (!session) {
      this._postReset();
    }
  }

  async refresh() {
    if (!this._view || !this._session) {
      return;
    }
    try {
      const info = await this._session.customRequest("pyrungHistoryInfo", {});
      this._view.webview.postMessage({
        type: "range",
        min: info.minScanId,
        max: info.maxScanId,
        value: info.playhead,
        count: info.count,
      });
    } catch (_error) {
      // session may have ended
    }
  }

  _postTags(tags, scanId, timestamp) {
    if (!this._view) {
      return;
    }
    this._view.webview.postMessage({ type: "tags", tags, scanId, timestamp });
  }

  _postReset() {
    if (!this._view) {
      return;
    }
    this._view.webview.postMessage({ type: "reset" });
  }

  _html() {
    return /* html */ `<!DOCTYPE html>
<html>
<head>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: var(--vscode-font-family);
    font-size: var(--vscode-font-size);
    color: var(--vscode-foreground);
    padding: 8px;
  }
  .slider-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
  }
  input[type="range"] {
    flex: 1;
    accent-color: var(--vscode-focusBorder);
  }
  .label {
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
    min-width: 7em;
    text-align: right;
  }
  .info {
    color: var(--vscode-descriptionForeground);
    font-size: 0.9em;
    margin-bottom: 6px;
  }
  .tags {
    max-height: 300px;
    overflow-y: auto;
    border-top: 1px solid var(--vscode-widget-border, #444);
    padding-top: 4px;
  }
  .tag-row {
    display: flex;
    justify-content: space-between;
    padding: 1px 0;
    font-family: var(--vscode-editor-font-family);
    font-size: var(--vscode-editor-font-size);
  }
  .tag-name { opacity: 0.8; }
  .tag-value { font-weight: bold; }
  .empty {
    color: var(--vscode-descriptionForeground);
    font-style: italic;
    padding: 8px 0;
  }
</style>
</head>
<body>
  <div class="slider-row">
    <input type="range" id="slider" min="0" max="0" value="0" disabled />
    <span class="label" id="label">--</span>
  </div>
  <div class="info" id="info"></div>
  <div class="tags" id="tags">
    <div class="empty">Step through scans to populate history</div>
  </div>
<script>
  const vscode = acquireVsCodeApi();
  const slider = document.getElementById("slider");
  const label = document.getElementById("label");
  const info = document.getElementById("info");
  const tags = document.getElementById("tags");

  let debounceTimer = null;

  slider.addEventListener("input", () => {
    const scanId = parseInt(slider.value, 10);
    label.textContent = scanId;
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      vscode.postMessage({ type: "seek", scanId });
    }, 30);
  });

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (msg.type === "range") {
      slider.min = msg.min;
      slider.max = msg.max;
      slider.value = msg.value;
      slider.disabled = msg.min === msg.max;
      label.textContent = msg.value;
      info.textContent = msg.count + " scans retained";
    } else if (msg.type === "tags") {
      label.textContent = msg.scanId;
      const sorted = Object.keys(msg.tags).sort();
      if (sorted.length === 0) {
        tags.innerHTML = '<div class="empty">No tags</div>';
      } else {
        tags.innerHTML = sorted
          .map(
            (k) =>
              '<div class="tag-row"><span class="tag-name">' +
              esc(k) +
              '</span><span class="tag-value">' +
              esc(msg.tags[k]) +
              "</span></div>"
          )
          .join("");
      }
    } else if (msg.type === "reset") {
      slider.min = 0;
      slider.max = 0;
      slider.value = 0;
      slider.disabled = true;
      label.textContent = "--";
      info.textContent = "";
      tags.innerHTML = '<div class="empty">No active session</div>';
    } else if (msg.type === "error") {
      info.textContent = msg.text;
    }
  });

  function esc(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
  }
</script>
</body>
</html>`;
  }
}

exports.PyrungHistorySliderProvider = PyrungHistorySliderProvider;
