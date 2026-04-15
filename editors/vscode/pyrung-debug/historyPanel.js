const vscode = require("vscode");

class PyrungHistoryPanelProvider {
  constructor() {
    this._view = null;
    this._isReady = false;
    this._session = null;
    this._watchedTags = new Set();
    this._tagHints = {};
    this._entries = [];
    this._activeScanId = null;
    this._hasMore = false;
    this._pageSize = 50;
    this._liveTimer = null;
    this._liveIntervalMs = 500;
  }

  resolveWebviewView(webviewView) {
    this._view = webviewView;
    this._isReady = false;
    webviewView.webview.options = { enableScripts: true };

    webviewView.webview.onDidReceiveMessage(async (message) => {
      if (message.type === "ready") {
        this._isReady = true;
        this._postState();
        if (this._session && this._watchedTags.size) {
          await this.refresh();
        }
        return;
      }

      if (message.type === "addTag") {
        this.addTag(message.tag);
        return;
      }

      if (message.type === "removeTag" && typeof message.tag === "string") {
        this._watchedTags.delete(message.tag);
        if (!this._watchedTags.size) {
          this._entries = [];
          this._hasMore = false;
          this._activeScanId = null;
          this._postState();
          return;
        }
        this._postState();
        await this.refresh();
        return;
      }

      if (!this._session) {
        return;
      }

      if (message.type === "seek" && Number.isInteger(message.scanId)) {
        try {
          const result = await this._session.customRequest("pyrungSeek", {
            scanId: message.scanId,
          });
          this._activeScanId = result.scanId;
          this._postState();
        } catch (error) {
          this._postError(String(error));
        }
        return;
      }

      if (message.type === "fork" && Number.isInteger(message.scanId)) {
        try {
          const result = await this._session.customRequest("pyrungForkAt", {
            scanId: message.scanId,
          });
          this._activeScanId = result.scanId;
          await this.refresh();
        } catch (error) {
          this._postError(String(error));
        }
        return;
      }

      if (message.type === "loadOlder") {
        await this._loadOlder();
      }
    });

    webviewView.webview.html = this._html();

    webviewView.onDidDispose(() => {
      this._view = null;
      this._isReady = false;
    });
  }

  setSession(session) {
    this._cancelLiveRefresh();
    this._session = session;
    if (!session) {
      this._entries = [];
      this._hasMore = false;
      this._activeScanId = null;
    }
    this._postState();
  }

  updateHints(tagHints) {
    this._tagHints = tagHints || {};
    this._postState();
  }

  addTag(tagName) {
    if (typeof tagName !== "string" || !tagName.trim()) {
      return;
    }

    const cleaned = tagName.trim();
    if (this._watchedTags.has(cleaned)) {
      return;
    }

    this._watchedTags.add(cleaned);
    this._postState();
    if (this._session) {
      void this.refresh();
    }
  }

  async refresh() {
    this._cancelLiveRefresh();
    if (!this._session) {
      this._postState();
      return;
    }

    if (!this._watchedTags.size) {
      this._entries = [];
      this._hasMore = false;
      this._activeScanId = null;
      this._postState();
      return;
    }

    await this._fetchEntries({ append: false });
  }

  async _loadOlder() {
    if (!this._session || !this._watchedTags.size || !this._entries.length) {
      return;
    }
    await this._fetchEntries({
      append: true,
      beforeScan: this._entries[this._entries.length - 1].scanId,
    });
  }

  liveRefresh() {
    if (!this._session || !this._watchedTags.size) {
      return;
    }
    if (this._liveTimer) {
      return;
    }
    this._liveTimer = setTimeout(async () => {
      this._liveTimer = null;
      await this._fetchNewEntries();
    }, this._liveIntervalMs);
  }

  _cancelLiveRefresh() {
    if (this._liveTimer) {
      clearTimeout(this._liveTimer);
      this._liveTimer = null;
    }
  }

  async _fetchNewEntries() {
    if (!this._session || !this._watchedTags.size) {
      return;
    }

    const afterScan = this._entries.length ? this._entries[0].scanId : undefined;

    try {
      const result = await this._session.customRequest("pyrungTagChanges", {
        tags: Array.from(this._watchedTags),
        count: this._pageSize,
        afterScan,
      });
      const newEntries = Array.isArray(result?.entries) ? result.entries : [];
      if (!afterScan) {
        this._entries = newEntries;
        this._hasMore = newEntries.length === this._pageSize;
      } else if (newEntries.length) {
        this._entries = newEntries.concat(this._entries);
      } else {
        return;
      }
      this._postState();
    } catch (_error) {
      // Session may have ended between the trace event and the request.
    }
  }

  async _fetchEntries({ append, beforeScan } = {}) {
    if (!this._session) {
      return;
    }

    try {
      const result = await this._session.customRequest("pyrungTagChanges", {
        tags: Array.from(this._watchedTags),
        count: this._pageSize,
        beforeScan,
      });
      const nextEntries = Array.isArray(result?.entries) ? result.entries : [];
      this._entries = append ? this._entries.concat(nextEntries) : nextEntries;
      this._hasMore = nextEntries.length === this._pageSize;
      this._postState();
    } catch (_error) {
      // Session may have ended between the stopped event and the request.
    }
  }

  _postState() {
    if (!this._view || !this._isReady) {
      return;
    }

    this._view.webview.postMessage({
      type: "state",
      watchedTags: Array.from(this._watchedTags),
      entries: this._entries,
      tagHints: this._tagHints,
      activeScanId: this._activeScanId,
      hasSession: Boolean(this._session),
      hasMore: this._hasMore,
    });
  }

  _postError(text) {
    if (!this._view) {
      return;
    }
    this._view.webview.postMessage({ type: "error", text: String(text) });
  }

  _html() {
    return /* html */ `<!DOCTYPE html>
<html>
<head>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 8px;
    font-family: var(--vscode-font-family);
    font-size: var(--vscode-font-size);
    color: var(--vscode-foreground);
    background: var(--vscode-sideBar-background);
  }
  .toolbar {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-bottom: 10px;
  }
  .input-row {
    display: flex;
    gap: 6px;
  }
  .tag-input {
    flex: 1;
    min-width: 0;
    padding: 5px 7px;
    border: 1px solid var(--vscode-input-border, var(--vscode-widget-border, #444));
    background: var(--vscode-input-background);
    color: var(--vscode-input-foreground);
  }
  .tag-input:focus {
    outline: 1px solid var(--vscode-focusBorder);
    outline-offset: 0;
  }
  .add-btn,
  .load-btn,
  .fork-btn {
    border: 1px solid transparent;
    background: var(--vscode-button-secondaryBackground);
    color: var(--vscode-button-secondaryForeground);
    cursor: pointer;
  }
  .add-btn,
  .load-btn {
    padding: 5px 8px;
  }
  .add-btn:hover,
  .load-btn:hover,
  .fork-btn:hover {
    background: var(--vscode-button-secondaryHoverBackground);
  }
  .tag-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 8px;
    border-radius: 999px;
    background: var(--vscode-badge-background, rgba(128, 128, 128, 0.18));
    color: var(--vscode-badge-foreground, var(--vscode-foreground));
  }
  .chip-remove {
    border: none;
    background: none;
    color: inherit;
    cursor: pointer;
    padding: 0;
    opacity: 0.75;
  }
  .chip-remove:hover { opacity: 1; }
  .panel-message {
    color: var(--vscode-descriptionForeground);
    font-style: italic;
    padding: 8px 2px;
  }
  .entries {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .entry {
    border: 1px solid var(--vscode-widget-border, rgba(128, 128, 128, 0.35));
    border-radius: 8px;
    overflow: hidden;
    background: var(--vscode-editorWidget-background, rgba(128, 128, 128, 0.06));
  }
  .entry.active {
    border-color: var(--vscode-focusBorder);
    box-shadow: inset 0 0 0 1px var(--vscode-focusBorder);
  }
  .entry-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 10px 6px;
  }
  .entry-meta {
    display: flex;
    align-items: baseline;
    gap: 10px;
    min-width: 0;
  }
  .scan-id {
    font-family: var(--vscode-editor-font-family);
    font-weight: 600;
    white-space: nowrap;
  }
  .entry-time {
    color: var(--vscode-descriptionForeground);
    white-space: nowrap;
  }
  .entry-actions {
    display: inline-flex;
    gap: 4px;
    flex-shrink: 0;
  }
  .fork-btn {
    min-width: 2em;
    padding: 3px 5px;
    border-radius: 4px;
  }
  .entry-body {
    width: 100%;
    border-top: 1px solid var(--vscode-widget-border, rgba(128, 128, 128, 0.2));
    background: transparent;
    color: inherit;
    cursor: pointer;
    text-align: left;
    padding: 8px 10px 10px;
  }
  .entry-body:hover {
    background: rgba(128, 128, 128, 0.07);
  }
  .change-row + .change-row {
    margin-top: 6px;
  }
  .change-tag {
    font-family: var(--vscode-editor-font-family);
    font-weight: 600;
    margin-right: 6px;
  }
  .change-values {
    color: var(--vscode-foreground);
  }
  .error {
    min-height: 1.25em;
    margin-top: 8px;
    color: var(--vscode-errorForeground);
    font-size: 0.9em;
  }
</style>
</head>
<body>
  <div class="toolbar">
    <div class="input-row">
      <input id="tag-input" class="tag-input" type="text" placeholder="Tag name" />
      <button id="add-btn" class="add-btn">Add</button>
    </div>
    <div id="tag-chips" class="tag-chips"></div>
  </div>
  <div id="content"></div>
  <div id="error" class="error"></div>
<script>
  const vscode = acquireVsCodeApi();
  const tagInput = document.getElementById("tag-input");
  const addBtn = document.getElementById("add-btn");
  const tagChips = document.getElementById("tag-chips");
  const content = document.getElementById("content");
  const errorEl = document.getElementById("error");

  const state = {
    watchedTags: [],
    entries: [],
    tagHints: {},
    activeScanId: null,
    hasSession: false,
    hasMore: false,
  };

  function esc(value) {
    const div = document.createElement("div");
    div.textContent = String(value);
    return div.innerHTML;
  }

  function addCurrentTag() {
    const tag = tagInput.value.trim();
    if (!tag) return;
    vscode.postMessage({ type: "addTag", tag });
    tagInput.value = "";
    tagInput.focus();
  }

  addBtn.addEventListener("click", addCurrentTag);
  tagInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addCurrentTag();
    }
  });

  tagChips.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-tag]");
    if (!button) return;
    vscode.postMessage({ type: "removeTag", tag: button.getAttribute("data-remove-tag") });
  });

  content.addEventListener("click", (event) => {
    const forkButton = event.target.closest("[data-fork-scan]");
    if (forkButton) {
      const scanId = Number(forkButton.getAttribute("data-fork-scan"));
      if (Number.isInteger(scanId)) {
        vscode.postMessage({ type: "fork", scanId });
      }
      return;
    }

    const seekTarget = event.target.closest("[data-seek-scan]");
    if (!seekTarget) return;
    const scanId = Number(seekTarget.getAttribute("data-seek-scan"));
    if (Number.isInteger(scanId)) {
      vscode.postMessage({ type: "seek", scanId });
    }
  });

  function renderChips() {
    if (!state.watchedTags.length) {
      tagChips.innerHTML = "";
      return;
    }

    tagChips.innerHTML = state.watchedTags
      .map(
        (tag) =>
          '<span class="chip">' +
          esc(tag) +
          '<button class="chip-remove" data-remove-tag="' +
          esc(tag) +
          '" title="Remove tag">\u00d7</button></span>'
      )
      .join("");
  }

  function choiceLabel(tagName, value) {
    const hints = state.tagHints[tagName];
    const choices = hints && hints.choices;
    const key = String(value);
    if (!choices || !Object.prototype.hasOwnProperty.call(choices, key)) {
      return null;
    }
    return choices[key];
  }

  function formatChange(tagName, values) {
    const oldValue = Array.isArray(values) ? values[0] : "";
    const newValue = Array.isArray(values) ? values[1] : "";
    let text = esc(oldValue) + " \u2192 " + esc(newValue);
    const oldLabel = choiceLabel(tagName, oldValue);
    const newLabel = choiceLabel(tagName, newValue);
    if (oldLabel || newLabel) {
      text +=
        " (" +
        esc(oldLabel || oldValue) +
        " \u2192 " +
        esc(newLabel || newValue) +
        ")";
    }
    return text;
  }

  function formatElapsed(seconds) {
    const total = Number(seconds) || 0;
    if (total < 60) {
      return "+" + total.toFixed(total < 10 ? 3 : 1).replace(/\\.0+$/, "") + "s";
    }

    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours > 0) {
      return "+" + hours + "h " + minutes + "m " + Math.round(secs) + "s";
    }
    return "+" + minutes + "m " + secs.toFixed(1).replace(/\\.0$/, "") + "s";
  }

  function renderEntries() {
    if (!state.watchedTags.length) {
      content.innerHTML =
        '<div class="panel-message">Add tags above to track their changes.</div>';
      return;
    }

    if (!state.hasSession) {
      content.innerHTML =
        '<div class="panel-message">Start a pyrung debug session to load history.</div>';
      return;
    }

    if (!state.entries.length) {
      content.innerHTML =
        '<div class="panel-message">No retained changes for the selected tags yet.</div>';
      return;
    }

    const entriesHtml = state.entries
      .map((entry) => {
        const activeClass = state.activeScanId === entry.scanId ? " active" : "";
        const changeRows = Object.entries(entry.changes || {})
          .map(
            ([tagName, values]) =>
              '<div class="change-row"><span class="change-tag">' +
              esc(tagName) +
              ':</span><span class="change-values">' +
              formatChange(tagName, values) +
              "</span></div>"
          )
          .join("");

        return (
          '<div class="entry' +
          activeClass +
          '">' +
          '<div class="entry-header">' +
          '<div class="entry-meta">' +
          '<span class="scan-id">#' +
          esc(entry.scanId) +
          "</span>" +
          '<span class="entry-time">' +
          esc(formatElapsed(entry.timestamp)) +
          "</span>" +
          "</div>" +
          '<div class="entry-actions">' +
          '<button class="fork-btn" data-fork-scan="' +
          esc(entry.prevScanId) +
          '" title="Fork before this change">\u21a9</button>' +
          '<button class="fork-btn" data-fork-scan="' +
          esc(entry.scanId) +
          '" title="Fork at this change">\u21aa</button>' +
          "</div>" +
          "</div>" +
          '<div class="entry-body" data-seek-scan="' +
          esc(entry.scanId) +
          '">' +
          changeRows +
          "</div>" +
          "</div>"
        );
      })
      .join("");

    const loadOlderHtml = state.hasMore
      ? '<button class="load-btn" id="load-older-btn">Load older...</button>'
      : "";

    content.innerHTML =
      '<div class="entries">' + entriesHtml + "</div>" + (loadOlderHtml ? '<div style="margin-top:8px;">' + loadOlderHtml + "</div>" : "");

    const loadOlderBtn = document.getElementById("load-older-btn");
    if (loadOlderBtn) {
      loadOlderBtn.addEventListener("click", () => {
        vscode.postMessage({ type: "loadOlder" });
      });
    }
  }

  function render() {
    renderChips();
    renderEntries();
  }

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (msg.type === "state") {
      state.watchedTags = Array.isArray(msg.watchedTags) ? msg.watchedTags : [];
      state.entries = Array.isArray(msg.entries) ? msg.entries : [];
      state.tagHints = msg.tagHints || {};
      state.activeScanId = Number.isInteger(msg.activeScanId) ? msg.activeScanId : null;
      state.hasSession = Boolean(msg.hasSession);
      state.hasMore = Boolean(msg.hasMore);
      render();
      return;
    }

    if (msg.type === "error") {
      errorEl.textContent = msg.text || "";
      setTimeout(() => {
        if (errorEl.textContent === (msg.text || "")) {
          errorEl.textContent = "";
        }
      }, 5000);
    }
  });

  render();
  vscode.postMessage({ type: "ready" });
</script>
</body>
</html>`;
  }
}

module.exports = {
  PyrungHistoryPanelProvider,
};
