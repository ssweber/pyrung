class PyrungHistoryPanelProvider {
  constructor({ getExecutionState, log } = {}) {
    this._view = null;
    this._isReady = false;
    this._session = null;
    this._watchedTags = new Set();
    this._tagHints = {};
    this._entries = [];
    this._activeScanId = null;
    this._hasMore = false;
    this._pageSize = 50;
    this._mode = "tags";
    this._chainResult = null;
    this._chainError = null;
    this._fetchGeneration = 0;
    this._getExecutionState =
      typeof getExecutionState === "function" ? getExecutionState : () => "unknown";
    this._log = typeof log === "function" ? log : null;
  }

  resolveWebviewView(webviewView) {
    this._view = webviewView;
    this._isReady = false;
    webviewView.webview.options = { enableScripts: true };

    webviewView.webview.onDidReceiveMessage(async (message) => {
      if (message.type === "ready") {
        this._isReady = true;
        this._debug("webview ready");
        this._postState();
        if (this._session && this._watchedTags.size) {
          this._debug("ready -> refresh()");
          await this.refresh();
        }
        return;
      }

      if (message.type === "addTag") {
        this._debug(`webview addTag(${message.tag})`);
        this.addTag(message.tag);
        return;
      }

      if (message.type === "removeTag" && typeof message.tag === "string") {
        this._debug(`removeTag(${message.tag})`);
        this._watchedTags.delete(message.tag);
        if (!this._watchedTags.size) {
          this._entries = [];
          this._hasMore = false;
          this._activeScanId = null;
          this._postState();
          return;
        }
        this._syncEntriesToWatchedTags();
        this._postState();
        await this.refresh();
        return;
      }

      if (
        message.type === "setMode" &&
        (message.mode === "tags" || message.mode === "chain")
      ) {
        this._mode = message.mode;
        this._postState();
        return;
      }

      if (message.type === "runCausal" && typeof message.query === "string") {
        await this._runCausal(message.query);
        return;
      }

      if (message.type === "suggestTags" && typeof message.query === "string") {
        await this._suggestTags(message.context, message.query);
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
          this._chainResult = null;
          this._chainError = null;
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
    this._fetchGeneration += 1;
    this._session = session;
    this._debug(`setSession(${session ? session.id : "null"})`);
    if (!session) {
      this._tagHints = {};
      this._entries = [];
      this._hasMore = false;
      this._activeScanId = null;
      this._chainResult = null;
      this._chainError = null;
    }
    this._postState();
  }

  updateHints(tagHints) {
    const all = tagHints || {};
    const filtered = {};
    for (const tag of this._watchedTags) {
      if (Object.prototype.hasOwnProperty.call(all, tag)) {
        filtered[tag] = all[tag];
      }
    }
    if (JSON.stringify(this._tagHints) === JSON.stringify(filtered)) {
      return;
    }
    this._tagHints = filtered;
    this._postState();
  }

  _executionState() {
    if (!this._session) {
      return "unknown";
    }
    try {
      const executionState = this._getExecutionState(this._session);
      return executionState === "running" || executionState === "stopped"
        ? executionState
        : "unknown";
    } catch (_error) {
      return "unknown";
    }
  }

  _canFetchHistory() {
    return Boolean(this._session) && this._executionState() === "stopped";
  }

  _syncEntriesToWatchedTags() {
    if (!this._entries.length) {
      return;
    }

    this._entries = this._entries
      .map((entry) => {
        const changes = {};
        for (const [tag, delta] of Object.entries(entry.changes || {})) {
          if (this._watchedTags.has(tag)) {
            changes[tag] = delta;
          }
        }
        if (!Object.keys(changes).length) {
          return null;
        }
        return {
          ...entry,
          changes,
        };
      })
      .filter(Boolean);
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
    this._debug(
      `addTag(${cleaned}) watched=${Array.from(this._watchedTags).join(",")} state=${this._executionState()}`
    );
    this._postState();
    if (this._canFetchHistory()) {
      this._debug(`addTag(${cleaned}) -> refresh()`);
      void this.refresh();
    }
  }

  async refresh() {
    if (!this._session) {
      this._debug("refresh skipped: no session");
      this._postState();
      return;
    }

    if (!this._watchedTags.size) {
      this._debug("refresh skipped: no watched tags");
      this._entries = [];
      this._hasMore = false;
      this._activeScanId = null;
      this._postState();
      return;
    }

    if (!this._canFetchHistory()) {
      this._debug(`refresh skipped: executionState=${this._executionState()}`);
      this._postState();
      return;
    }

    this._debug(`refresh fetching watched=${Array.from(this._watchedTags).join(",")}`);
    await this._fetchEntries({ append: false });
  }

  async _loadOlder() {
    if (!this._canFetchHistory() || !this._watchedTags.size || !this._entries.length) {
      return;
    }
    await this._fetchEntries({
      append: true,
      beforeScan: this._entries[this._entries.length - 1].scanId,
    });
  }

  appendLiveChanges(changes, scanId) {
    if (!this._watchedTags.size || !changes.length) {
      return;
    }
    const relevant = changes.filter((c) => this._watchedTags.has(c.tag));
    if (!relevant.length) {
      return;
    }
    const changesMap = {};
    for (const c of relevant) {
      changesMap[c.tag] = [c.previous, c.current];
    }
    const prevScanId = this._entries.length
      ? this._entries[0].scanId
      : scanId - 1;
    const entry = {
      scanId,
      prevScanId,
      timestamp: null,
      changes: changesMap,
    };
    this._entries = [entry, ...this._entries.slice(0, this._pageSize - 1)];
    this._postState();
  }


  async _suggestTags(context, query) {
    const ctx = context === "chain" ? "chain" : "tags";
    const trimmed = typeof query === "string" ? query.trim() : "";
    if (!trimmed || !this._session) {
      this._postSuggestions(ctx, trimmed, [], {});
      return;
    }
    try {
      const result = await this._session.customRequest("pyrungQuery", {
        query: trimmed,
      });
      const tags = Array.isArray(result?.tags) ? result.tags.slice(0, 10) : [];
      const roles = result?.roles || {};
      this._postSuggestions(ctx, trimmed, tags, roles);
    } catch (_error) {
      this._postSuggestions(ctx, trimmed, [], {});
    }
  }

  _postSuggestions(context, query, tags, roles) {
    if (!this._view || !this._isReady) return;
    this._view.webview.postMessage({
      type: "tagSuggestions",
      context,
      query,
      tags,
      roles,
    });
  }

  async _runCausal(query) {
    const trimmed = typeof query === "string" ? query.trim() : "";
    if (!trimmed) {
      this._chainResult = null;
      this._chainError = null;
      this._postState();
      return;
    }
    if (!this._session) {
      this._chainResult = null;
      this._chainError = "No active pyrung debug session.";
      this._postState();
      return;
    }

    try {
      const result = await this._session.customRequest("pyrungCausal", {
        query: trimmed,
      });
      this._chainResult = result || null;
      this._chainError = null;
    } catch (error) {
      this._chainResult = null;
      this._chainError = String(error?.message || error);
    }
    this._postState();
  }

  async _fetchEntries({ append, beforeScan } = {}) {
    if (!this._canFetchHistory()) {
      return;
    }

    const generation = this._fetchGeneration;
    try {
      this._debug(
        `_fetchEntries(append=${append ? "true" : "false"}, beforeScan=${beforeScan ?? "none"}) watched=${Array.from(this._watchedTags).join(",")}`
      );
      const result = await this._session.customRequest("pyrungTagChanges", {
        tags: Array.from(this._watchedTags),
        count: this._pageSize,
        beforeScan,
      });
      if (this._fetchGeneration !== generation) {
        return;
      }
      const nextEntries = Array.isArray(result?.entries) ? result.entries : [];
      this._entries = append ? this._entries.concat(nextEntries) : nextEntries;
      this._hasMore = nextEntries.length === this._pageSize;
      this._debug(`_fetchEntries received ${nextEntries.length} entries`);
      this._postState();
    } catch (error) {
      this._debug(`_fetchEntries failed: ${String(error?.message || error)}`);
      // Session may have ended between the stopped event and the request.
    }
  }

  _debug(message) {
    if (!this._log) {
      return;
    }
    this._log(message);
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
      executionState: this._executionState(),
      hasMore: this._hasMore,
      mode: this._mode,
      chainResult: this._chainResult,
      chainError: this._chainError,
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
  .mode-tabs {
    display: flex;
    gap: 4px;
    margin-bottom: 10px;
    border-bottom: 1px solid var(--vscode-widget-border, rgba(128, 128, 128, 0.3));
  }
  .mode-tab {
    flex: 1;
    padding: 6px 8px;
    background: transparent;
    color: var(--vscode-descriptionForeground);
    border: none;
    border-bottom: 2px solid transparent;
    cursor: pointer;
    font-family: inherit;
    font-size: inherit;
  }
  .mode-tab:hover {
    color: var(--vscode-foreground);
  }
  .mode-tab.active {
    color: var(--vscode-foreground);
    border-bottom-color: var(--vscode-focusBorder);
  }
  .composer {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-bottom: 10px;
  }
  .composer-row {
    display: flex;
    gap: 6px;
    align-items: stretch;
  }
  .composer select,
  .composer input {
    padding: 5px 7px;
    border: 1px solid var(--vscode-input-border, var(--vscode-widget-border, #444));
    background: var(--vscode-input-background);
    color: var(--vscode-input-foreground);
    font-family: inherit;
    font-size: inherit;
  }
  .composer select:focus,
  .composer input:focus {
    outline: 1px solid var(--vscode-focusBorder);
    outline-offset: 0;
  }
  .composer-tag {
    flex: 1;
    min-width: 0;
  }
  .composer-scan {
    width: 6.5em;
  }
  .composer-value {
    flex: 1;
    min-width: 0;
  }
  .composer-row.hidden {
    display: none;
  }
  .composer-preview {
    font-family: var(--vscode-editor-font-family);
    font-size: 0.9em;
    color: var(--vscode-descriptionForeground);
    padding: 4px 2px;
    min-height: 1.2em;
    word-break: break-all;
  }
  .run-btn {
    border: 1px solid transparent;
    background: var(--vscode-button-background);
    color: var(--vscode-button-foreground);
    cursor: pointer;
    padding: 5px 12px;
  }
  .run-btn:hover:not(:disabled) {
    background: var(--vscode-button-hoverBackground);
  }
  .run-btn:disabled {
    opacity: 0.5;
    cursor: default;
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
  .change-row {
    display: flex;
    align-items: baseline;
    gap: 6px;
  }
  .change-row + .change-row {
    margin-top: 6px;
  }
  .change-text {
    flex: 1;
    min-width: 0;
  }
  .explain-btn {
    flex-shrink: 0;
    padding: 0 4px;
    background: transparent;
    border: 1px solid transparent;
    color: var(--vscode-descriptionForeground);
    cursor: pointer;
    font-size: 0.95em;
    border-radius: 4px;
    opacity: 0.6;
  }
  .explain-btn:hover {
    opacity: 1;
    background: rgba(128, 128, 128, 0.15);
    color: var(--vscode-foreground);
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
  .suggestions {
    display: flex;
    flex-direction: column;
    margin-top: 4px;
    border: 1px solid var(--vscode-widget-border, rgba(128, 128, 128, 0.3));
    background: var(--vscode-dropdown-background, var(--vscode-editorWidget-background));
    max-height: 220px;
    overflow-y: auto;
    border-radius: 4px;
  }
  .suggestions[hidden] {
    display: none;
  }
  .suggestion {
    padding: 4px 8px;
    cursor: pointer;
    display: flex;
    gap: 6px;
    align-items: center;
    font-family: var(--vscode-editor-font-family);
  }
  .suggestion:hover,
  .suggestion.focused {
    background: var(--vscode-list-hoverBackground, rgba(128, 128, 128, 0.15));
  }
  .suggestion-role {
    font-size: 0.75em;
    width: 1.3em;
    text-align: center;
    color: var(--vscode-descriptionForeground);
    font-weight: 600;
  }
  .suggestion-role.input { color: #4A90D9; }
  .suggestion-role.pivot { color: #D9A441; }
  .suggestion-role.terminal { color: #5CB85C; }
  .suggestion-role.isolated { color: #888; }
  .suggestion-none {
    padding: 4px 8px;
    color: var(--vscode-descriptionForeground);
    font-style: italic;
    font-size: 0.9em;
  }
  .chain {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .chain-effect {
    border: 1px solid var(--vscode-widget-border, rgba(128, 128, 128, 0.35));
    border-radius: 8px;
    padding: 10px 12px;
    background: var(--vscode-editorWidget-background, rgba(128, 128, 128, 0.06));
  }
  .chain-effect-row {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
  }
  .mode-badge {
    display: inline-block;
    padding: 1px 8px;
    border-radius: 999px;
    font-size: 0.8em;
    font-weight: 600;
    letter-spacing: 0.03em;
    text-transform: uppercase;
  }
  .mode-badge.recorded {
    background: var(--vscode-testing-iconPassed, #3fb950);
    color: var(--vscode-editor-background);
  }
  .mode-badge.projected {
    background: var(--vscode-charts-blue, #3794ff);
    color: var(--vscode-editor-background);
  }
  .mode-badge.unreachable {
    background: var(--vscode-errorForeground, #f85149);
    color: var(--vscode-editor-background);
  }
  .recovers-banner {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    border-radius: 6px;
    font-weight: 600;
  }
  .recovers-banner.ok {
    background: rgba(63, 185, 80, 0.15);
    color: var(--vscode-testing-iconPassed, #3fb950);
  }
  .recovers-banner.fail {
    background: rgba(248, 81, 73, 0.15);
    color: var(--vscode-errorForeground, #f85149);
  }
  .chain-meta {
    color: var(--vscode-descriptionForeground);
    font-size: 0.9em;
  }
  .chain-step {
    border: 1px solid var(--vscode-widget-border, rgba(128, 128, 128, 0.35));
    border-radius: 8px;
    overflow: hidden;
    background: var(--vscode-editorWidget-background, rgba(128, 128, 128, 0.06));
  }
  .chain-step-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 10px;
    border-bottom: 1px solid var(--vscode-widget-border, rgba(128, 128, 128, 0.2));
  }
  .chain-step-meta {
    display: flex;
    align-items: baseline;
    gap: 8px;
    min-width: 0;
    flex-wrap: wrap;
  }
  .rung-tag {
    font-family: var(--vscode-editor-font-family);
    font-weight: 600;
    color: var(--vscode-symbolIcon-functionForeground, var(--vscode-foreground));
  }
  .chain-step-body {
    padding: 8px 10px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .cause-list,
  .enabling-list {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .cause-label,
  .enabling-label {
    font-size: 0.85em;
    color: var(--vscode-descriptionForeground);
    letter-spacing: 0.04em;
    text-transform: uppercase;
    margin-right: 6px;
  }
  .proximate-item,
  .root-item {
    padding: 2px 0;
    cursor: pointer;
  }
  .proximate-item:hover,
  .root-item:hover {
    color: var(--vscode-textLink-activeForeground);
  }
  .enabling-item {
    padding: 2px 0;
    color: var(--vscode-descriptionForeground);
    cursor: pointer;
  }
  .enabling-item:hover {
    color: var(--vscode-foreground);
  }
  details.enabling-details > summary {
    cursor: pointer;
    color: var(--vscode-descriptionForeground);
    font-size: 0.85em;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    list-style: revert;
  }
  .chain-roots {
    border: 1px dashed var(--vscode-widget-border, rgba(128, 128, 128, 0.3));
    border-radius: 8px;
    padding: 8px 12px;
  }
  .chain-roots-title {
    font-size: 0.85em;
    color: var(--vscode-descriptionForeground);
    letter-spacing: 0.04em;
    text-transform: uppercase;
    margin-bottom: 6px;
  }
  .root-group + .root-group {
    margin-top: 8px;
  }
  .blocker {
    border: 1px solid rgba(248, 81, 73, 0.4);
    border-radius: 6px;
    padding: 8px 10px;
    background: rgba(248, 81, 73, 0.06);
  }
  .blocker + .blocker {
    margin-top: 6px;
  }
  .blocker-reason {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 0.75em;
    font-family: var(--vscode-editor-font-family);
    background: rgba(248, 81, 73, 0.2);
    color: var(--vscode-errorForeground, #f85149);
    margin-left: 6px;
  }
  .sub-blockers {
    margin-top: 6px;
    margin-left: 12px;
    padding-left: 8px;
    border-left: 2px solid rgba(248, 81, 73, 0.3);
  }
  #chain-pane { position: relative; }
  .paused-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--vscode-editor-background, rgba(30,30,30,0.85));
    opacity: 0.85;
    z-index: 10;
    font-size: 0.9em;
    color: var(--vscode-descriptionForeground);
  }
  .paused-overlay.hidden { display: none; }
</style>
</head>
<body>
  <div class="mode-tabs" role="tablist">
    <button class="mode-tab" data-mode="tags" role="tab">Tags</button>
    <button class="mode-tab" data-mode="chain" role="tab">Chain</button>
  </div>

  <div id="tags-pane">
    <div class="toolbar">
      <div class="input-row">
        <input id="tag-input" class="tag-input" type="text" placeholder="Tag name" autocomplete="off" />
        <button id="add-btn" class="add-btn">Add</button>
      </div>
      <div id="tag-suggestions" class="suggestions" hidden></div>
      <div id="tag-chips" class="tag-chips"></div>
    </div>
    <div id="content"></div>
  </div>

  <div id="chain-pane" hidden>
    <div id="chain-paused-overlay" class="paused-overlay hidden">Runs while paused</div>
    <div class="composer">
      <div class="composer-row">
        <select id="chain-cmd" title="Causal query command">
          <option value="cause">cause</option>
          <option value="effect">effect</option>
          <option value="recovers">recovers</option>
        </select>
        <input id="chain-tag" class="composer-tag" type="text" placeholder="Tag name" autocomplete="off" />
      </div>
      <div id="chain-suggestions" class="suggestions" hidden></div>
      <div class="composer-row" id="chain-args-row">
        <input id="chain-scan" class="composer-scan" type="number" placeholder="@scan" />
        <input id="chain-value" class="composer-value" type="text" placeholder=":value" />
      </div>
      <div class="composer-preview" id="chain-preview"></div>
      <div class="composer-row">
        <button id="chain-run" class="run-btn" disabled>Run</button>
      </div>
    </div>
    <div id="chain-content"></div>
  </div>

  <div id="error" class="error"></div>
<script>
  const vscode = acquireVsCodeApi();
  const tagInput = document.getElementById("tag-input");
  const addBtn = document.getElementById("add-btn");
  const tagChips = document.getElementById("tag-chips");
  const content = document.getElementById("content");
  const errorEl = document.getElementById("error");
  const tagsPane = document.getElementById("tags-pane");
  const chainPane = document.getElementById("chain-pane");
  const modeTabs = document.querySelectorAll(".mode-tab");
  const chainCmd = document.getElementById("chain-cmd");
  const chainTag = document.getElementById("chain-tag");
  const chainScan = document.getElementById("chain-scan");
  const chainValue = document.getElementById("chain-value");
  const chainArgsRow = document.getElementById("chain-args-row");
  const chainPreview = document.getElementById("chain-preview");
  const chainRun = document.getElementById("chain-run");
  const chainContent = document.getElementById("chain-content");
  const tagSuggestionsEl = document.getElementById("tag-suggestions");
  const chainSuggestionsEl = document.getElementById("chain-suggestions");

  const state = {
    watchedTags: [],
    entries: [],
    tagHints: {},
    activeScanId: null,
    hasSession: false,
    executionState: "unknown",
    hasMore: false,
    mode: "tags",
    chainResult: null,
    chainError: null,
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

  addBtn.addEventListener("click", () => {
    addCurrentTag();
    hideSuggestions("tags");
  });
  tagInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addCurrentTag();
      hideSuggestions("tags");
    } else if (event.key === "Escape") {
      hideSuggestions("tags");
    }
  });

  function makeDebouncer(delay) {
    let timer = null;
    return (fn) => {
      clearTimeout(timer);
      timer = setTimeout(fn, delay);
    };
  }
  const debounceTagsSuggest = makeDebouncer(150);
  const debounceChainSuggest = makeDebouncer(150);

  function hideSuggestions(context) {
    const el = context === "chain" ? chainSuggestionsEl : tagSuggestionsEl;
    el.hidden = true;
    el.innerHTML = "";
  }

  function requestSuggestions(context, value) {
    const q = (value || "").trim();
    if (!q) {
      hideSuggestions(context);
      return;
    }
    const runner = context === "chain" ? debounceChainSuggest : debounceTagsSuggest;
    runner(() => {
      vscode.postMessage({ type: "suggestTags", context, query: q });
    });
  }

  tagInput.addEventListener("input", () => requestSuggestions("tags", tagInput.value));
  chainTag.addEventListener("input", () => requestSuggestions("chain", chainTag.value));

  tagChips.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-tag]");
    if (!button) return;
    vscode.postMessage({ type: "removeTag", tag: button.getAttribute("data-remove-tag") });
  });

  document.body.addEventListener("click", (event) => {
    const suggestion = event.target.closest("[data-suggest-tag]");
    if (suggestion) {
      const tag = suggestion.getAttribute("data-suggest-tag") || "";
      const ctx = suggestion.getAttribute("data-suggest-context") || "tags";
      if (!tag) return;
      if (ctx === "chain") {
        chainTag.value = tag;
        hideSuggestions("chain");
        renderComposer();
        chainTag.focus();
      } else {
        vscode.postMessage({ type: "addTag", tag });
        tagInput.value = "";
        hideSuggestions("tags");
        tagInput.focus();
      }
      return;
    }

    const explainButton = event.target.closest("[data-explain-tag]");
    if (explainButton) {
      event.stopPropagation();
      const tag = explainButton.getAttribute("data-explain-tag") || "";
      const scanId = Number(explainButton.getAttribute("data-explain-scan"));
      if (tag) {
        chainCmd.value = "cause";
        chainTag.value = tag;
        chainScan.value = Number.isInteger(scanId) ? String(scanId) : "";
        chainValue.value = "";
        renderComposer();
        vscode.postMessage({ type: "setMode", mode: "chain" });
        const query = buildCausalQuery();
        if (query) {
          vscode.postMessage({ type: "runCausal", query });
        }
      }
      return;
    }

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
              '<div class="change-row">' +
              '<div class="change-text"><span class="change-tag">' +
              esc(tagName) +
              ':</span><span class="change-values">' +
              formatChange(tagName, values) +
              "</span></div>" +
              '<button class="explain-btn" data-explain-tag="' +
              esc(tagName) +
              '" data-explain-scan="' +
              esc(entry.scanId) +
              '" title="Explain cause of this change">\u{1F4A1}</button>' +
              "</div>"
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

  function buildCausalQuery() {
    const cmd = chainCmd.value;
    const tag = chainTag.value.trim();
    if (!tag) return "";
    if (cmd === "recovers") {
      return "recovers:" + tag;
    }
    const scan = chainScan.value.trim();
    const value = chainValue.value.trim();
    // DAP grammar accepts @scan OR :value, not both. Scan wins when both set.
    if (scan) return cmd + ":" + tag + "@" + scan;
    if (value) return cmd + ":" + tag + ":" + value;
    return cmd + ":" + tag;
  }

  function renderComposer() {
    const cmd = chainCmd.value;
    chainArgsRow.classList.toggle("hidden", cmd === "recovers");
    const query = buildCausalQuery();
    chainPreview.textContent = query || "\u2014";
    chainRun.disabled = !query;
  }

  function formatValue(tagName, value) {
    const label = choiceLabel(tagName, value);
    if (label) return esc(value) + " (" + esc(label) + ")";
    return esc(value);
  }

  function formatTransition(t) {
    return (
      '<span class="change-tag">' +
      esc(t.tag) +
      ":</span> " +
      formatValue(t.tag, t.from) +
      " \u2192 " +
      formatValue(t.tag, t.to) +
      ' <span class="scan-id">@#' +
      esc(t.scan) +
      "</span>"
    );
  }

  function forkButtons(scanId) {
    const prev = Math.max(0, Number(scanId) - 1);
    return (
      '<div class="entry-actions">' +
      '<button class="fork-btn" data-fork-scan="' +
      esc(prev) +
      '" title="Fork before this transition">\u21a9</button>' +
      '<button class="fork-btn" data-fork-scan="' +
      esc(scanId) +
      '" title="Fork at this transition">\u21aa</button>' +
      "</div>"
    );
  }

  function renderEffectCard(chain, command) {
    const e = chain.effect;
    const modeClass = chain.mode;
    const modeLabel = esc(chain.mode);
    let title;
    if (chain.mode === "unreachable" || chain.mode === "projected") {
      title =
        '<span class="change-tag">' +
        esc(e.tag) +
        ":</span> \u2192 " +
        formatValue(e.tag, e.to);
    } else {
      title = formatTransition(e);
    }
    const meta = [];
    if (Number.isFinite(chain.duration_scans) && chain.duration_scans > 0) {
      meta.push("span " + chain.duration_scans + " scans");
    }
    if (Number.isFinite(chain.confidence) && chain.confidence < 1) {
      meta.push("confidence " + chain.confidence.toFixed(2));
    }
    const metaHtml = meta.length
      ? '<div class="chain-meta">' + esc(meta.join(" \u00b7 ")) + "</div>"
      : "";
    const cmdLabel = '<span class="chain-meta">' + esc(command) + "</span>";
    return (
      '<div class="chain-effect">' +
      '<div class="chain-effect-row">' +
      '<span class="mode-badge ' +
      modeClass +
      '">' +
      modeLabel +
      "</span>" +
      cmdLabel +
      "<span>" +
      title +
      "</span>" +
      "</div>" +
      metaHtml +
      "</div>"
    );
  }

  function renderProximateCause(t) {
    return (
      '<li class="proximate-item" data-seek-scan="' +
      esc(t.scan) +
      '" title="Seek to this scan">' +
      formatTransition(t) +
      "</li>"
    );
  }

  function renderEnabling(ec) {
    const held =
      ec.held_since_scan === null || ec.held_since_scan === undefined
        ? ""
        : ' <span class="scan-id">held since #' + esc(ec.held_since_scan) + "</span>";
    const seekAttr =
      Number.isInteger(ec.held_since_scan)
        ? ' data-seek-scan="' + esc(ec.held_since_scan) + '"'
        : "";
    return (
      '<li class="enabling-item"' +
      seekAttr +
      ">" +
      '<span class="change-tag">' +
      esc(ec.tag) +
      ":</span> = " +
      formatValue(ec.tag, ec.value) +
      held +
      "</li>"
    );
  }

  function renderStep(step) {
    const t = step.transition;
    const proximate = (step.proximate_causes || [])
      .map(renderProximateCause)
      .join("");
    const proximateHtml = proximate
      ? '<div><span class="cause-label">Proximate</span><ul class="cause-list">' +
        proximate +
        "</ul></div>"
      : "";
    const enabling = (step.enabling_conditions || [])
      .map(renderEnabling)
      .join("");
    const enablingHtml = enabling
      ? '<details class="enabling-details"><summary>Enabling (' +
        (step.enabling_conditions || []).length +
        ')</summary><ul class="enabling-list">' +
        enabling +
        "</ul></details>"
      : "";
    return (
      '<div class="chain-step">' +
      '<div class="chain-step-header">' +
      '<div class="chain-step-meta">' +
      '<span class="rung-tag">Rung #' +
      esc(step.rung_index) +
      "</span>" +
      "<span>" +
      formatTransition(t) +
      "</span>" +
      "</div>" +
      forkButtons(t.scan) +
      "</div>" +
      '<div class="chain-step-body">' +
      proximateHtml +
      enablingHtml +
      "</div>" +
      "</div>"
    );
  }

  function renderRootGroup(label, transitions) {
    if (!transitions || !transitions.length) return "";
    const items = transitions
      .map(
        (t) =>
          '<li class="root-item" data-seek-scan="' +
          esc(t.scan) +
          '" title="Seek to this scan">' +
          formatTransition(t) +
          "</li>"
      )
      .join("");
    return (
      '<div class="root-group">' +
      '<div class="chain-roots-title">' +
      esc(label) +
      "</div>" +
      '<ul class="cause-list">' +
      items +
      "</ul>" +
      "</div>"
    );
  }

  function renderBlocker(blocker) {
    const subs = (blocker.sub_blockers || []).map(renderBlocker).join("");
    const subsHtml = subs ? '<div class="sub-blockers">' + subs + "</div>" : "";
    return (
      '<div class="blocker">' +
      '<div><span class="rung-tag">Rung #' +
      esc(blocker.rung_index) +
      "</span> needs " +
      '<span class="change-tag">' +
      esc(blocker.blocked_tag) +
      "</span> = " +
      formatValue(blocker.blocked_tag, blocker.needed_value) +
      '<span class="blocker-reason">' +
      esc(blocker.reason) +
      "</span></div>" +
      subsHtml +
      "</div>"
    );
  }

  function renderChainBody(chain) {
    if (chain.mode === "unreachable") {
      const blockers = (chain.blockers || []).map(renderBlocker).join("");
      return blockers
        ? '<div class="chain-roots"><div class="chain-roots-title">Blockers</div>' +
            blockers +
            "</div>"
        : '<div class="panel-message">No blockers reported.</div>';
    }
    const steps = (chain.steps || []).map(renderStep).join("");
    const conjunctive = renderRootGroup(
      "Caused jointly by " + (chain.conjunctive_roots || []).length + " event(s)",
      chain.conjunctive_roots
    );
    const ambiguous = renderRootGroup(
      (chain.ambiguous_roots || []).length + " candidate cause(s) \u2014 ambiguous",
      chain.ambiguous_roots
    );
    const footer =
      conjunctive || ambiguous
        ? '<div class="chain-roots">' + conjunctive + ambiguous + "</div>"
        : "";
    return steps + footer;
  }

  function renderChainResult() {
    if (state.chainError) {
      chainContent.innerHTML =
        '<div class="panel-message" style="color:var(--vscode-errorForeground);">' +
        esc(state.chainError) +
        "</div>";
      return;
    }
    if (!state.chainResult) {
      chainContent.innerHTML =
        '<div class="panel-message">Compose a query and click Run.</div>';
      return;
    }

    const result = state.chainResult;
    const command = result.command || "cause";
    const chain = result.chain;

    if (!chain) {
      const msg =
        command === "recovers"
          ? "No clear path reachable from current state."
          : "No chain found for this query.";
      chainContent.innerHTML =
        '<div class="panel-message">' + esc(msg) + "</div>";
      return;
    }

    let banner = "";
    if (command === "recovers") {
      banner = result.ok
        ? '<div class="recovers-banner ok">\u2713 Recovers</div>'
        : '<div class="recovers-banner fail">\u2717 Does not recover</div>';
    }

    chainContent.innerHTML =
      '<div class="chain">' +
      banner +
      renderEffectCard(chain, command) +
      renderChainBody(chain) +
      "</div>";
  }

  const chainOverlay = document.getElementById("chain-paused-overlay");

  function renderMode() {
    modeTabs.forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.mode === state.mode);
    });
    const chainActive = state.mode === "chain";
    tagsPane.hidden = chainActive;
    chainPane.hidden = !chainActive;
    const isRunning = state.executionState === "running";
    chainOverlay.classList.toggle("hidden", !isRunning);
  }

  modeTabs.forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.mode;
      if (mode !== state.mode) {
        vscode.postMessage({ type: "setMode", mode });
      }
    });
  });

  chainCmd.addEventListener("change", renderComposer);
  [chainTag, chainScan, chainValue].forEach((input) => {
    input.addEventListener("input", renderComposer);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !chainRun.disabled) {
        event.preventDefault();
        chainRun.click();
      }
    });
  });
  chainRun.addEventListener("click", () => {
    const query = buildCausalQuery();
    if (!query) return;
    hideSuggestions("chain");
    vscode.postMessage({ type: "runCausal", query });
  });

  function render() {
    renderMode();
    renderChips();
    renderEntries();
    renderComposer();
    renderChainResult();
  }

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (msg.type === "state") {
      state.watchedTags = Array.isArray(msg.watchedTags) ? msg.watchedTags : [];
      state.entries = Array.isArray(msg.entries) ? msg.entries : [];
      state.tagHints = msg.tagHints || {};
      state.activeScanId = Number.isInteger(msg.activeScanId) ? msg.activeScanId : null;
      state.hasSession = Boolean(msg.hasSession);
      state.executionState = msg.executionState || "unknown";
      state.hasMore = Boolean(msg.hasMore);
      state.mode = msg.mode === "chain" ? "chain" : "tags";
      state.chainResult = msg.chainResult || null;
      state.chainError = typeof msg.chainError === "string" ? msg.chainError : null;
      render();
      return;
    }

    if (msg.type === "tagSuggestions") {
      renderSuggestions(
        msg.context === "chain" ? "chain" : "tags",
        msg.query || "",
        Array.isArray(msg.tags) ? msg.tags : [],
        msg.roles || {}
      );
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

  function renderSuggestions(context, query, tags, roles) {
    const containerEl = context === "chain" ? chainSuggestionsEl : tagSuggestionsEl;
    const inputEl = context === "chain" ? chainTag : tagInput;
    if (inputEl.value.trim() !== query) {
      // Stale response — user has moved on.
      return;
    }
    if (!query) {
      hideSuggestions(context);
      return;
    }
    if (!tags.length) {
      containerEl.innerHTML = '<div class="suggestion-none">No matches</div>';
      containerEl.hidden = false;
      return;
    }
    containerEl.innerHTML = tags
      .map((tag) => {
        const role = roles[tag] || "";
        const badge = role
          ? '<span class="suggestion-role ' +
            esc(role) +
            '">' +
            esc(role.charAt(0).toUpperCase()) +
            "</span>"
          : '<span class="suggestion-role"></span>';
        return (
          '<div class="suggestion" data-suggest-tag="' +
          esc(tag) +
          '" data-suggest-context="' +
          esc(context) +
          '">' +
          badge +
          "<span>" +
          esc(tag) +
          "</span></div>"
        );
      })
      .join("");
    containerEl.hidden = false;
  }

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
