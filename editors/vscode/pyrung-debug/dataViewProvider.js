const fs = require("fs");
const path = require("path");
const vscode = require("vscode");

class PyrungDataViewProvider {
  constructor(options = {}) {
    this._view = null;
    this._session = null;
    this._graphData = null;
    this._watchedTags = new Set();
    this._watchedGroups = new Set();
    this._watchedItems = [];
    this._latestTagGroups = {};
    this._latestTagHints = {};
    this._onWatchHistory =
      typeof options.onWatchHistory === "function" ? options.onWatchHistory : null;
    this._sortableScript = this._loadSortableScript();
  }

  _loadSortableScript() {
    try {
      return fs.readFileSync(path.join(__dirname, "vendor", "Sortable.min.js"), "utf8");
    } catch (error) {
      console.error("Failed to load SortableJS vendor bundle:", error);
      return "";
    }
  }

  _itemKey(type, name) {
    return `${type}:${name}`;
  }

  _addWatchedItem(type, name) {
    if (!name) return;
    const key = this._itemKey(type, name);
    if (this._watchedItems.some((item) => this._itemKey(item.type, item.name) === key)) {
      return;
    }
    this._watchedItems.push({ type, name });
  }

  _removeWatchedItem(type, name) {
    const key = this._itemKey(type, name);
    this._watchedItems = this._watchedItems.filter(
      (item) => this._itemKey(item.type, item.name) !== key
    );
  }

  _replaceWatchedItem(oldType, oldName, newType, newName) {
    const oldKey = this._itemKey(oldType, oldName);
    const newKey = this._itemKey(newType, newName);
    const nextItems = [];
    let replaced = false;
    for (const item of this._watchedItems) {
      const itemKey = this._itemKey(item.type, item.name);
      if (itemKey === oldKey && !replaced) {
        nextItems.push({ type: newType, name: newName });
        replaced = true;
        continue;
      }
      if (itemKey === oldKey || itemKey === newKey) {
        continue;
      }
      nextItems.push(item);
    }
    if (!replaced) {
      nextItems.push({ type: newType, name: newName });
    }
    this._watchedItems = nextItems;
  }

  _reorderWatchedItems(items) {
    const currentItems = new Map(
      this._watchedItems.map((item) => [this._itemKey(item.type, item.name), item])
    );
    const reordered = [];
    for (const item of items || []) {
      if (!item || (item.type !== "tag" && item.type !== "group") || !item.name) {
        continue;
      }
      const key = this._itemKey(item.type, item.name);
      const current = currentItems.get(key);
      if (!current) continue;
      reordered.push(current);
      currentItems.delete(key);
    }
    this._watchedItems = reordered.concat(Array.from(currentItems.values()));
  }

  resolveWebviewView(webviewView) {
    this._view = webviewView;
    webviewView.webview.options = { enableScripts: true };
    webviewView.webview.html = this._html();

    webviewView.webview.onDidReceiveMessage(async (message) => {
      if (message.type === "removeTag") {
        this._watchedTags.delete(message.tag);
        this._removeWatchedItem("tag", message.tag);
      } else if (message.type === "removeGroup") {
        this._watchedGroups.delete(message.group);
        this._removeWatchedItem("group", message.group);
      } else if (message.type === "promoteToGroup" && message.tag) {
        this._watchedTags.delete(message.tag);
        this._watchedGroups.add(message.tag);
        this._replaceWatchedItem("tag", message.tag, "group", message.tag);
      } else if (message.type === "reorderItems" && Array.isArray(message.items)) {
        this._reorderWatchedItems(message.items);
      } else if (message.type === "watchHistory" && message.tag) {
        if (this._onWatchHistory) {
          try {
            await this._onWatchHistory(message.tag);
          } catch (error) {
            this._postError(`History failed: ${error}`);
          }
        }
      } else if (!this._session) {
        return;
      } else if (message.type === "force" && message.tag) {
        try {
          await this._session.customRequest("pyrungForce", {
            tag: message.tag,
            value: message.value,
          });
        } catch (error) {
          this._postError(`Force failed: ${error}`);
        }
      } else if (message.type === "unforce" && message.tag) {
        try {
          await this._session.customRequest("pyrungUnforce", { tag: message.tag });
        } catch (error) {
          this._postError(`Unforce failed: ${error}`);
        }
      } else if (message.type === "writeAll") {
        try {
          const patches = message.patches;
          if (patches && Object.keys(patches).length > 0) {
            await this._session.customRequest("pyrungPatch", { patches });
          }
        } catch (error) {
          this._postError(`Write failed: ${error}`);
        }
      } else if (message.type === "patchSingle" && message.tag) {
        try {
          await this._session.customRequest("pyrungPatch", {
            tag: message.tag,
            value: message.value,
          });
        } catch (error) {
          this._postError(`Patch failed: ${error}`);
        }
      } else if (message.type === "addTagFromQuery" && message.tag) {
        this.addTag(message.tag);
      } else if (message.type === "query" && message.query && this._session) {
        try {
          const result = await this._session.customRequest("pyrungQuery", {
            query: message.query,
          });
          this._postMessage({ type: "queryResults", query: message.query, ...result });
        } catch (error) {
          this._postMessage({ type: "queryResults", query: message.query, tags: [], roles: {} });
        }
      }
    });

    webviewView.onDidDispose(() => {
      this._view = null;
    });

    // Restore watched items if webview was re-created
    for (const item of this._watchedItems) {
      if (item.type === "group") {
        this._postMessage({ type: "addGroup", group: item.name });
      } else {
        this._postMessage({ type: "addTag", tag: item.name });
      }
    }

    // Send cached graph data if available
    if (this._graphData) {
      this._postMessage({ type: "graphData", data: this._graphData });
    }
  }

  setSession(session) {
    this._session = session;
    if (!session) {
      this._graphData = null;
      this._postMessage({ type: "reset" });
    }
  }

  updateGraph(graphData) {
    this._graphData = graphData;
    this._postMessage({ type: "graphData", data: graphData });
  }

  addTag(tagName) {
    if (!tagName) return;
    if (tagName in this._latestTagGroups) {
      this.addGroup(tagName);
      return;
    }
    if (this._watchedTags.has(tagName)) return;
    this._watchedTags.add(tagName);
    this._addWatchedItem("tag", tagName);
    this._postMessage({ type: "addTag", tag: tagName });
  }

  addGroup(groupName) {
    if (!groupName) return;
    if (this._watchedGroups.has(groupName)) return;
    if (this._watchedTags.has(groupName)) {
      this._watchedTags.delete(groupName);
      this._replaceWatchedItem("tag", groupName, "group", groupName);
    } else {
      this._addWatchedItem("group", groupName);
    }
    this._watchedGroups.add(groupName);
    this._postMessage({ type: "addGroup", group: groupName });
  }

  updateTrace(tagValues, forces, tagTypes, tagGroups, tagHints) {
    const latestTagGroups = tagGroups || {};
    const latestTagHints = tagHints || {};
    this._latestTagGroups = latestTagGroups;
    this._latestTagHints = latestTagHints;
    if (!this._view) return;
    // Collect all relevant tags: individually watched + group members
    const relevantTags = new Set(this._watchedTags);
    for (const group of this._watchedGroups) {
      const members = latestTagGroups[group];
      if (members) {
        for (const m of members) relevantTags.add(m);
      }
    }

    const filteredValues = {};
    const filteredTypes = {};
    const filteredForces = {};
    const filteredHints = {};
    for (const tag of relevantTags) {
      if (tag in tagValues) filteredValues[tag] = tagValues[tag];
      if (tag in tagTypes) filteredTypes[tag] = tagTypes[tag];
      if (tag in forces) filteredForces[tag] = forces[tag];
      if (tag in latestTagHints) filteredHints[tag] = latestTagHints[tag];
    }

    const filteredGroups = {};
    for (const group of this._watchedGroups) {
      if (group in latestTagGroups) filteredGroups[group] = latestTagGroups[group];
    }

    this._postMessage({
      type: "update",
      tagValues: filteredValues,
      tagTypes: filteredTypes,
      forces: filteredForces,
      tagHints: filteredHints,
      tagGroups: filteredGroups,
    });
  }

  _postError(text) {
    this._postMessage({ type: "error", text: String(text) });
  }

  _postMessage(message) {
    if (this._view) {
      this._view.webview.postMessage(message);
    }
  }

  _html() {
    const sortableScript = this._sortableScript
      ? `<script>${this._sortableScript.replace(/<\/script/gi, "<\\/script")}</script>`
      : "";
    return /* html */ `<!DOCTYPE html>
<html>
<head>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: var(--vscode-font-family);
    font-size: var(--vscode-font-size);
    color: var(--vscode-foreground);
    padding: 6px;
  }
  .toolbar {
    display: flex;
    gap: 4px;
    margin-bottom: 6px;
    align-items: center;
  }
  .toolbar-btn {
    background: var(--vscode-button-secondaryBackground);
    color: var(--vscode-button-secondaryForeground);
    border: none;
    padding: 3px 8px;
    cursor: pointer;
    font-size: 0.85em;
  }
  .toolbar-btn:hover {
    background: var(--vscode-button-secondaryHoverBackground);
  }
  .tag-table {
    width: 100%;
    border-collapse: collapse;
  }
  .tag-table th {
    text-align: left;
    padding: 2px 4px;
    border-bottom: 1px solid var(--vscode-widget-border, #444);
    font-weight: 500;
    font-size: 0.85em;
    color: var(--vscode-descriptionForeground);
    white-space: nowrap;
  }
  .tag-row td {
    padding: 2px 4px;
    vertical-align: middle;
    font-family: var(--vscode-editor-font-family);
    font-size: var(--vscode-editor-font-size);
    border-bottom: 1px solid var(--vscode-widget-border, rgba(128,128,128,0.2));
  }
  .tag-row.forced td {
    background: rgba(255, 200, 0, 0.08);
  }
  .group-header td {
    padding: 4px 4px 2px;
    font-weight: bold;
    font-size: 0.9em;
    border-bottom: 1px solid var(--vscode-widget-border, rgba(128,128,128,0.3));
  }
  .group-header td:hover { opacity: 0.8; }
  .drag-handle {
    cursor: grab;
    user-select: none;
    white-space: nowrap;
  }
  .drag-handle::before {
    content: "\\2261";
    display: inline-block;
    margin-right: 0.35em;
    opacity: 0.6;
  }
  .sortable-chosen .drag-handle,
  .sortable-drag .drag-handle {
    cursor: grabbing;
  }
  .sortable-ghost td { opacity: 0.35; }
  .group-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.25em;
    cursor: pointer;
    user-select: none;
  }
  .group-chevron {
    display: inline-block;
    width: 1em;
    text-align: center;
    font-size: 0.8em;
  }
  .group-member .tag-name { padding-left: 1em; }
  .row-num {
    color: var(--vscode-descriptionForeground);
    text-align: right;
    padding-right: 6px;
    font-size: 0.85em;
    min-width: 2em;
  }
  .tag-name { white-space: nowrap; }
  .tag-name-label { display: inline-block; }
  .readonly-badge, .public-badge {
    display: none;
    margin-left: 0.5em;
    color: var(--vscode-descriptionForeground);
    font-size: 0.8em;
    letter-spacing: 0.04em;
  }
  .readonly-badge.visible, .public-badge.visible { display: inline-block; }
  .filter-bar {
    display: flex;
    align-items: center;
    padding: 2px 8px;
    font-size: 0.85em;
    color: var(--vscode-descriptionForeground);
    gap: 4px;
  }
  .filter-bar label {
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 3px;
    user-select: none;
  }
  .filter-bar label.disabled {
    opacity: 0.5;
    cursor: default;
  }
  .tag-type {
    color: var(--vscode-descriptionForeground);
    font-size: 0.85em;
    white-space: nowrap;
  }
  .tag-range {
    font-size: 0.8em;
    white-space: nowrap;
  }
  .tag-range.default-range {
    color: var(--vscode-descriptionForeground);
    opacity: 0.5;
  }
  .tag-range.declared-range {
    color: var(--vscode-foreground);
    opacity: 0.85;
  }
  .tag-value {
    font-weight: bold;
    white-space: nowrap;
    min-width: 3.5em;
  }
  .new-value-cell { white-space: nowrap; }
  .bool-btn {
    background: var(--vscode-button-secondaryBackground);
    color: var(--vscode-button-secondaryForeground);
    border: 1px solid transparent;
    padding: 1px 5px;
    cursor: pointer;
    font-size: 0.85em;
    font-family: var(--vscode-editor-font-family);
  }
  .bool-btn:hover { background: var(--vscode-button-secondaryHoverBackground); }
  .bool-btn.selected {
    border-color: var(--vscode-focusBorder);
    font-weight: bold;
  }
  .tag-input {
    width: 5em;
    background: var(--vscode-input-background);
    color: var(--vscode-input-foreground);
    border: 1px solid var(--vscode-input-border, var(--vscode-widget-border, #444));
    padding: 1px 3px;
    font-family: var(--vscode-editor-font-family);
    font-size: var(--vscode-editor-font-size);
  }
  .tag-input:focus { outline: 1px solid var(--vscode-focusBorder); }
  .tag-select {
    min-width: 9em;
    max-width: 16em;
    background: var(--vscode-input-background);
    color: var(--vscode-input-foreground);
    border: 1px solid var(--vscode-input-border, var(--vscode-widget-border, #444));
    padding: 1px 3px;
    font-family: var(--vscode-editor-font-family);
    font-size: var(--vscode-editor-font-size);
  }
  .tag-select:focus { outline: 1px solid var(--vscode-focusBorder); }
  .force-btn {
    background: var(--vscode-button-secondaryBackground);
    color: var(--vscode-button-secondaryForeground);
    border: 1px solid transparent;
    padding: 1px 5px;
    cursor: pointer;
    font-size: 0.85em;
    min-width: 5em;
  }
  .force-btn:hover { background: var(--vscode-button-secondaryHoverBackground); }
  .force-btn.active {
    background: var(--vscode-inputValidation-warningBackground, rgba(255,200,0,0.2));
    border-color: var(--vscode-inputValidation-warningBorder, #cca700);
    font-weight: bold;
  }
  .force-btn.readonly-locked {
    opacity: 0.35;
    pointer-events: none;
  }
  .lock-btn {
    background: none;
    border: none;
    cursor: pointer;
    font-size: 0.85em;
    padding: 1px 3px;
    opacity: 0.6;
    line-height: 1;
  }
  .lock-btn:hover { opacity: 1; }
  .tag-row.readonly-locked .new-value-cell > * {
    opacity: 0.35;
    pointer-events: none;
  }
  .row-actions {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .history-btn {
    background: var(--vscode-button-secondaryBackground);
    color: var(--vscode-button-secondaryForeground);
    border: 1px solid transparent;
    padding: 1px 5px;
    cursor: pointer;
    font-size: 0.95em;
    line-height: 1.1;
    min-width: 2.25em;
  }
  .history-btn:hover { background: var(--vscode-button-secondaryHoverBackground); }
  .btn-remove {
    opacity: 0.4;
    cursor: pointer;
    background: none;
    border: none;
    color: var(--vscode-foreground);
    padding: 1px 3px;
    font-size: 0.8em;
  }
  .btn-remove:hover { opacity: 1; }
  .empty {
    color: var(--vscode-descriptionForeground);
    font-style: italic;
    padding: 8px 0;
  }
  .error {
    color: var(--vscode-errorForeground);
    font-size: 0.9em;
    padding: 4px 0;
  }
  /* Query search bar */
  .search-bar {
    display: flex;
    gap: 4px;
    margin-bottom: 6px;
    align-items: center;
  }
  .search-input {
    flex: 1;
    background: var(--vscode-input-background);
    color: var(--vscode-input-foreground);
    border: 1px solid var(--vscode-input-border, var(--vscode-widget-border, #444));
    padding: 3px 6px;
    font-family: var(--vscode-editor-font-family);
    font-size: var(--vscode-editor-font-size);
  }
  .search-input:focus { outline: 1px solid var(--vscode-focusBorder); }
  .search-input::placeholder { color: var(--vscode-input-placeholderForeground); }
  /* Query results */
  .query-results {
    margin-bottom: 6px;
    max-height: 150px;
    overflow-y: auto;
  }
  .query-result-item {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 2px 4px;
    cursor: pointer;
    font-family: var(--vscode-editor-font-family);
    font-size: var(--vscode-editor-font-size);
  }
  .query-result-item:hover {
    background: var(--vscode-list-hoverBackground);
  }
  .query-role {
    font-size: 0.75em;
    padding: 0 4px;
    border-radius: 3px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    white-space: nowrap;
  }
  .query-role.input { color: #4A90D9; }
  .query-role.pivot { color: #D9A441; }
  .query-role.terminal { color: #5CB85C; }
  .query-role.isolated { color: #888; }
  /* Edge expansion */
  .edge-row td {
    padding: 1px 4px 1px 2em;
    font-size: 0.85em;
    color: var(--vscode-descriptionForeground);
    border: none;
  }
  .edge-row .edge-arrow { opacity: 0.6; }
  .edge-row .edge-tag {
    cursor: pointer;
    text-decoration: underline;
    text-decoration-style: dotted;
  }
  .edge-row .edge-tag:hover {
    color: var(--vscode-foreground);
  }
  .edge-row.decay-1 { opacity: 0.6; }
  .edge-row.decay-2 { opacity: 0.35; }
</style>
</head>
<body>
  <div class="search-bar" id="search-bar">
    <input class="search-input" id="search-input" type="text" placeholder="Search tags... (i: p: t: upstream: downstream:)" />
  </div>
  <div class="query-results" id="query-results"></div>
  <div class="toolbar" id="toolbar" style="display:none;">
    <button class="toolbar-btn" id="write-btn" title="Patch all pending new values (one-scan)">Write Values</button>
    <button class="toolbar-btn" id="clear-btn" title="Clear all pending new values">Clear</button>
  </div>
  <div class="filter-bar" id="filter-bar">
    <label id="public-filter-label" class="disabled" title="Start debugger to enable"><input type="checkbox" id="public-filter" disabled /> Public</label>
  </div>
  <div id="content">
    <div class="empty">Right-click a tag in the editor and select "Add to Data View"</div>
  </div>
  <div id="error"></div>
${sortableScript}
<script>
  const vscode = acquireVsCodeApi();
  const toolbar = document.getElementById("toolbar");
  const writeBtn = document.getElementById("write-btn");
  const clearBtn = document.getElementById("clear-btn");
  const content = document.getElementById("content");
  const errorEl = document.getElementById("error");
  const searchInput = document.getElementById("search-input");
  const queryResultsEl = document.getElementById("query-results");
  const publicFilter = document.getElementById("public-filter");
  const publicFilterLabel = document.getElementById("public-filter-label");
  let publicFilterActive = false;

  // Individual tags: tag -> entry
  const tagEntries = new Map();
  // Groups: groupName -> { headerRow, memberTags: Set, collapsed }
  const groupEntries = new Map();
  let sortable = null;
  let hiddenDraggedRows = [];

  // Graph data for edge expansion
  let graphData = null;
  // Neighbor index: { tagName: { upstream: [name,...], downstream: [name,...] } }
  let neighborIndex = {};
  // Recent focus ring for decay: [{tag, edgeRows}]
  const focusRing = [];
  const FOCUS_RING_MAX = 3;

  function buildNeighborIndex(data) {
    const index = {};
    if (!data || !data.rungNodes) return index;
    for (const rn of data.rungNodes) {
      const reads = [].concat(rn.conditionReads || [], rn.dataReads || []);
      const writes = rn.writes || [];
      // For each written tag, its upstream is all reads of this rung
      for (const w of writes) {
        if (!index[w]) index[w] = { upstream: new Set(), downstream: new Set() };
        for (const r of reads) index[w].upstream.add(r);
      }
      // For each read tag, its downstream is all writes of this rung
      for (const r of reads) {
        if (!index[r]) index[r] = { upstream: new Set(), downstream: new Set() };
        for (const w of writes) index[r].downstream.add(w);
      }
    }
    // Convert sets to sorted arrays
    for (const key of Object.keys(index)) {
      index[key].upstream = Array.from(index[key].upstream).sort();
      index[key].downstream = Array.from(index[key].downstream).sort();
    }
    return index;
  }

  // --- Search / query ---
  let queryTimer = null;
  searchInput.addEventListener("input", () => {
    clearTimeout(queryTimer);
    const q = searchInput.value.trim();
    if (!q) {
      queryResultsEl.innerHTML = "";
      return;
    }
    queryTimer = setTimeout(() => {
      vscode.postMessage({ type: "query", query: q });
    }, 250);
  });

  function renderQueryResults(tags, roles) {
    queryResultsEl.innerHTML = "";
    if (!tags || tags.length === 0) {
      if (searchInput.value.trim()) {
        queryResultsEl.textContent = "No matches";
      }
      return;
    }
    for (const tag of tags) {
      const item = document.createElement("div");
      item.className = "query-result-item";
      const role = roles[tag] || "";
      if (role) {
        const badge = document.createElement("span");
        badge.className = "query-role " + role;
        badge.textContent = role.charAt(0).toUpperCase();
        item.appendChild(badge);
      }
      const nameSpan = document.createElement("span");
      nameSpan.textContent = tag;
      item.appendChild(nameSpan);
      item.title = "Click to add to Data View";
      item.addEventListener("click", () => {
        vscode.postMessage({ type: "addTagFromQuery", tag });
      });
      queryResultsEl.appendChild(item);
    }
  }

  // --- Edge expansion ---
  function clearEdgeRows(decayLevel) {
    const rows = document.querySelectorAll(".edge-row" + (decayLevel != null ? ".decay-" + decayLevel : ""));
    rows.forEach((r) => r.remove());
  }

  function insertEdgeRows(tagName, afterRow) {
    const neighbors = neighborIndex[tagName];
    if (!neighbors) return [];
    const rows = [];
    const tbody = afterRow.parentNode;
    let insertPoint = afterRow;

    if (neighbors.upstream.length > 0) {
      const row = document.createElement("tr");
      row.className = "edge-row";
      row.dataset.edgeOwner = tagName;
      const td = document.createElement("td");
      td.colSpan = 8;
      const arrow = document.createElement("span");
      arrow.className = "edge-arrow";
      arrow.textContent = "\u2190 ";
      td.appendChild(arrow);
      for (let i = 0; i < neighbors.upstream.length; i++) {
        if (i > 0) td.appendChild(document.createTextNode(", "));
        const link = document.createElement("span");
        link.className = "edge-tag";
        link.textContent = neighbors.upstream[i];
        link.addEventListener("click", () => {
          handleEdgeTagClick(neighbors.upstream[i]);
        });
        td.appendChild(link);
      }
      row.appendChild(td);
      insertPoint.after(row);
      insertPoint = row;
      rows.push(row);
    }

    if (neighbors.downstream.length > 0) {
      const row = document.createElement("tr");
      row.className = "edge-row";
      row.dataset.edgeOwner = tagName;
      const td = document.createElement("td");
      td.colSpan = 8;
      const arrow = document.createElement("span");
      arrow.className = "edge-arrow";
      arrow.textContent = "\u2192 ";
      td.appendChild(arrow);
      for (let i = 0; i < neighbors.downstream.length; i++) {
        if (i > 0) td.appendChild(document.createTextNode(", "));
        const link = document.createElement("span");
        link.className = "edge-tag";
        link.textContent = neighbors.downstream[i];
        link.addEventListener("click", () => {
          handleEdgeTagClick(neighbors.downstream[i]);
        });
        td.appendChild(link);
      }
      row.appendChild(td);
      insertPoint.after(row);
      rows.push(row);
    }
    return rows;
  }

  function handleEdgeTagClick(tagName) {
    // Add to watch list if not already there, then select it
    if (!tagEntries.has(tagName)) {
      addTag(tagName);
      vscode.postMessage({ type: "addTagFromQuery", tag: tagName });
    }
    selectTagForEdges(tagName);
  }

  function selectTagForEdges(tagName) {
    const entry = tagEntries.get(tagName);
    if (!entry || !entry.row) return;

    // Age existing focus ring entries
    for (const focus of focusRing) {
      for (const row of focus.edgeRows) {
        if (row.classList.contains("decay-1")) {
          row.classList.remove("decay-1");
          row.classList.add("decay-2");
        } else if (!row.classList.contains("decay-2")) {
          row.classList.add("decay-1");
        }
      }
    }
    // Remove oldest if at capacity
    while (focusRing.length >= FOCUS_RING_MAX) {
      const oldest = focusRing.shift();
      for (const row of oldest.edgeRows) row.remove();
    }
    // Remove existing edge rows for this tag (if re-selected)
    const existing = focusRing.findIndex((f) => f.tag === tagName);
    if (existing >= 0) {
      const old = focusRing.splice(existing, 1)[0];
      for (const row of old.edgeRows) row.remove();
    }

    const edgeRows = insertEdgeRows(tagName, entry.row);
    if (edgeRows.length > 0) {
      focusRing.push({ tag: tagName, edgeRows });
    }
  }

  function normalizeHintKey(value) {
    if (value === undefined || value === null) return "";
    return String(value);
  }

  function parseTypedValue(tagType, raw) {
    const text = String(raw ?? "").trim();
    if (text === "") return undefined;
    if (tagType === "char") return text;

    const num = Number(text);
    if (Number.isNaN(num)) return undefined;
    if (tagType === "int" || tagType === "dint" || tagType === "word") {
      return Number.isInteger(num) ? num : undefined;
    }
    return num;
  }

  function choiceLabelForValue(entry, rawValue) {
    const choices = (entry.tagHints && entry.tagHints.choices) || null;
    if (!choices) return null;
    const key = normalizeHintKey(rawValue);
    return Object.prototype.hasOwnProperty.call(choices, key) ? choices[key] : null;
  }

  function updateValueDisplay(entry, rawValue) {
    entry.rawValue = rawValue;
    const label = choiceLabelForValue(entry, rawValue);
    entry.valueEl.textContent = label ? label + " (" + rawValue + ")" : rawValue;
  }

  function ensureTable() {
    if (!document.getElementById("tag-table")) {
      content.innerHTML =
        '<table class="tag-table" id="tag-table">' +
        "<thead><tr>" +
        '<th class="row-num">No.</th>' +
        "<th>Tag</th><th>Type</th><th>Range</th><th>Value</th>" +
        "<th>New Value</th><th>Actions</th><th></th>" +
        "</tr></thead>" +
        '<tbody id="tag-body"></tbody></table>';
      toolbar.style.display = "flex";
    }
    const tbody = document.getElementById("tag-body");
    initializeSortable(tbody);
    return tbody;
  }

  function destroySortable() {
    if (!sortable) return;
    sortable.destroy();
    sortable = null;
  }

  function refreshRowNumbers() {
    const tbody = document.getElementById("tag-body");
    if (!tbody) return;

    let index = 1;
    for (const row of Array.from(tbody.children)) {
      if (row.classList.contains("group-header")) continue;
      if (row.style.display === "none") continue;
      const numCell = row.querySelector(".row-num");
      if (!numCell) continue;
      numCell.textContent = String(index).padStart(3, "0");
      index += 1;
    }
  }

  function syncGroupBlockPositions(groupName) {
    const tbody = document.getElementById("tag-body");
    if (!tbody) return;

    const groups = groupName ? [groupName] : Array.from(groupEntries.keys());
    for (const name of groups) {
      const entry = groupEntries.get(name);
      if (!entry) continue;

      let anchor = entry.headerRow;
      for (const memberTag of entry.memberTags) {
        const memberEntry = tagEntries.get(memberTag);
        if (!memberEntry) continue;
        const targetSibling = anchor.nextElementSibling;
        if (targetSibling !== memberEntry.row) {
          tbody.insertBefore(memberEntry.row, targetSibling);
        }
        anchor = memberEntry.row;
      }
    }
  }

  function currentTopLevelOrder() {
    const tbody = document.getElementById("tag-body");
    if (!tbody) return [];

    return Array.from(tbody.children)
      .filter((row) => row.classList.contains("sortable-item"))
      .map((row) => ({
        type: row.dataset.itemType,
        name: row.dataset.itemName,
      }));
  }

  function postCurrentOrder() {
    vscode.postMessage({ type: "reorderItems", items: currentTopLevelOrder() });
  }

  function initializeSortable(tbody) {
    if (sortable || !tbody || typeof Sortable !== "function") return;

    sortable = Sortable.create(tbody, {
      animation: 150,
      draggable: ".sortable-item",
      handle: ".drag-handle",
      ghostClass: "sortable-ghost",
      chosenClass: "sortable-chosen",
      dragClass: "sortable-drag",
      onStart: (event) => {
        hiddenDraggedRows = [];
        if (event.item.dataset.itemType !== "group") return;

        const entry = groupEntries.get(event.item.dataset.itemName);
        if (!entry) return;

        for (const memberTag of entry.memberTags) {
          const memberEntry = tagEntries.get(memberTag);
          if (!memberEntry) continue;
          hiddenDraggedRows.push({
            row: memberEntry.row,
            display: memberEntry.row.style.display,
          });
          memberEntry.row.style.display = "none";
        }
      },
      onEnd: () => {
        syncGroupBlockPositions();
        for (const item of hiddenDraggedRows) {
          item.row.style.display = item.display;
        }
        hiddenDraggedRows = [];
        refreshRowNumbers();
        postCurrentOrder();
      },
    });
  }

  function setPendingValue(entry, value) {
    entry.pendingValue = value;
    syncPendingControl(entry);
    // If already forced, immediately update the force to the new value
    if (entry.forced && value !== undefined) {
      vscode.postMessage({ type: "force", tag: entry.tagName, value });
    }
  }

  function syncChoiceSelect(entry) {
    const select = entry._select;
    if (!select) return;

    const choices = (entry.tagHints && entry.tagHints.choices) || {};
    const pendingKey = entry.pendingValue !== undefined ? normalizeHintKey(entry.pendingValue) : null;
    const rawKey = normalizeHintKey(entry.rawValue);
    const selectedKey = pendingKey !== null ? pendingKey : rawKey;
    const hasSelectedChoice = Object.prototype.hasOwnProperty.call(choices, selectedKey);

    select.innerHTML = "";
    if (!hasSelectedChoice && entry.rawValue !== undefined && entry.rawValue !== "--") {
      const rawOption = document.createElement("option");
      rawOption.value = "";
      rawOption.textContent = "Current: " + entry.rawValue;
      select.appendChild(rawOption);
    }

    for (const [key, label] of Object.entries(choices)) {
      const option = document.createElement("option");
      option.value = key;
      option.textContent = label + " (" + key + ")";
      select.appendChild(option);
    }

    if (hasSelectedChoice) {
      select.value = selectedKey;
    } else {
      select.value = "";
    }
  }

  function syncPendingControl(entry) {
    if (entry._trueBtn) entry._trueBtn.classList.toggle("selected", entry.pendingValue === true);
    if (entry._falseBtn) entry._falseBtn.classList.toggle("selected", entry.pendingValue === false);
    if (entry._select) syncChoiceSelect(entry);
  }

  function applyReadonlyHint(entry) {
    const isReadonly = !!(entry.tagHints && entry.tagHints.readonly);
    entry._readonlyBadge.classList.toggle("visible", isReadonly);
    entry._lockBtn.style.display = isReadonly ? "" : "none";
    if (isReadonly && !entry._readonlyUnlocked) {
      setReadonlyLocked(entry, true);
    } else {
      setReadonlyLocked(entry, false);
    }
  }

  function setReadonlyLocked(entry, locked) {
    entry.row.classList.toggle("readonly-locked", locked);
    entry.forceBtn.classList.toggle("readonly-locked", locked);
    entry._lockBtn.textContent = locked ? "\ud83d\udd12" : "\ud83d\udd13";
    entry._lockBtn.title = locked
      ? "Unlock editing for this read-only tag"
      : "Re-lock this read-only tag";
  }

  function applyPublicHint(entry) {
    const isPublic = !!(entry.tagHints && entry.tagHints.public);
    entry._publicBadge.classList.toggle("visible", isPublic);
  }

  function formatRange(min, max) {
    function fmt(v) {
      if (typeof v !== "number") return String(v);
      if (Math.abs(v) >= 1e10) return v.toExponential(1);
      return String(v);
    }
    return fmt(min) + ".." + fmt(max);
  }

  function applyRangeHint(entry) {
    const hints = entry.tagHints || {};
    const cell = entry.rangeEl;
    if (hints.min == null && hints.max == null) {
      cell.textContent = "";
      cell.className = "tag-range";
      cell.title = "";
      return;
    }
    const isDefault = !!hints.rangeDefault;
    const text = formatRange(hints.min, hints.max);
    cell.textContent = text;
    cell.className = "tag-range " + (isDefault ? "default-range" : "declared-range");
    cell.title = isDefault ? "Type default range" : "Declared range constraint";
  }

  let hintsReceived = false;

  function applyPublicFilter() {
    const filtering = publicFilterActive && hintsReceived;
    for (const entry of tagEntries.values()) {
      const isPublic = !!(entry.tagHints && entry.tagHints.public);
      const groupCollapsed = isGroupMemberCollapsed(entry);
      if (filtering && !isPublic) {
        entry.row.style.display = "none";
      } else if (groupCollapsed) {
        entry.row.style.display = "none";
      } else {
        entry.row.style.display = "";
      }
    }
    // Hide group headers whose visible members are all hidden
    for (const [groupName, ge] of groupEntries.entries()) {
      if (!filtering) {
        ge.headerRow.style.display = "";
        continue;
      }
      let anyVisible = false;
      for (const memberTag of ge.memberTags) {
        const entry = tagEntries.get(memberTag);
        if (entry && entry.tagHints && entry.tagHints.public) {
          anyVisible = true;
          break;
        }
      }
      ge.headerRow.style.display = anyVisible ? "" : "none";
    }
    refreshRowNumbers();
  }

  function isGroupMemberCollapsed(entry) {
    for (const [, ge] of groupEntries.entries()) {
      if (ge.collapsed && ge.memberTags.has(entry.tagName)) return true;
    }
    return false;
  }

  function buildNewValueCell(entry) {
    const cell = entry.newValueCell;
    cell.innerHTML = "";
    entry._trueBtn = null;
    entry._falseBtn = null;
    entry._input = null;
    entry._select = null;

    if (entry.tagType === "bool") {
      const trueBtn = document.createElement("button");
      trueBtn.className = "bool-btn";
      trueBtn.textContent = "True";
      trueBtn.addEventListener("click", () => {
        setPendingValue(entry, true);
      });
      trueBtn.addEventListener("dblclick", () => {
        vscode.postMessage({ type: "patchSingle", tag: entry.tagName, value: true });
      });

      const falseBtn = document.createElement("button");
      falseBtn.className = "bool-btn";
      falseBtn.textContent = "False";
      falseBtn.addEventListener("click", () => {
        setPendingValue(entry, false);
      });
      falseBtn.addEventListener("dblclick", () => {
        vscode.postMessage({ type: "patchSingle", tag: entry.tagName, value: false });
      });

      cell.appendChild(trueBtn);
      cell.appendChild(falseBtn);
      entry._trueBtn = trueBtn;
      entry._falseBtn = falseBtn;
      syncPendingControl(entry);
      return;
    }

    const choices = (entry.tagHints && entry.tagHints.choices) || null;
    if (choices) {
      const select = document.createElement("select");
      select.className = "tag-select";
      select.addEventListener("change", () => {
        if (select.value === "") {
          entry.pendingValue = undefined;
          syncPendingControl(entry);
          return;
        }
        const value = parseTypedValue(entry.tagType, select.value);
        if (value === undefined) return;
        setPendingValue(entry, value);
        vscode.postMessage({ type: "patchSingle", tag: entry.tagName, value });
      });
      cell.appendChild(select);
      entry._select = select;
      syncChoiceSelect(entry);
      return;
    }

    const input = document.createElement("input");
    input.className = "tag-input";
    input.type = "text";
    input.placeholder = entry.tagType || "value";
    input.addEventListener("input", () => {
      entry.pendingValue = parseTypedValue(entry.tagType, input.value);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const val = parseTypedValue(entry.tagType, input.value);
        if (val === undefined) return;
        if (entry.forced) {
          vscode.postMessage({ type: "force", tag: entry.tagName, value: val });
        } else {
          vscode.postMessage({ type: "patchSingle", tag: entry.tagName, value: val });
        }
      }
    });
    cell.appendChild(input);
    entry._input = input;
  }

  function createTagRow(tag, opts) {
    const isGroupMember = opts && opts.groupMember;
    const tbody = ensureTable();
    const row = document.createElement("tr");
    row.className = "tag-row" + (isGroupMember ? " group-member" : " sortable-item");
    if (!isGroupMember) {
      row.dataset.itemType = "tag";
      row.dataset.itemName = tag;
    } else if (opts && opts.groupName) {
      row.dataset.groupName = opts.groupName;
    }

    const numCell = document.createElement("td");
    numCell.className = "row-num";
    numCell.textContent = "";
    if (!isGroupMember) {
      numCell.classList.add("drag-handle");
      numCell.title = "Drag to reorder";
    }

    const nameCell = document.createElement("td");
    nameCell.className = "tag-name";
    // For group members, show just the field part after the group prefix
    const displayName = (isGroupMember && opts.displayName) ? opts.displayName : tag;
    const nameLabel = document.createElement("span");
    nameLabel.className = "tag-name-label";
    nameLabel.textContent = displayName;
    const readonlyBadge = document.createElement("span");
    readonlyBadge.className = "readonly-badge";
    readonlyBadge.textContent = "RO";
    readonlyBadge.title = "Read-only hint";
    const publicBadge = document.createElement("span");
    publicBadge.className = "public-badge";
    publicBadge.textContent = "P";
    publicBadge.title = "Public — part of the intended API surface";
    nameCell.appendChild(nameLabel);
    nameCell.appendChild(readonlyBadge);
    nameCell.appendChild(publicBadge);
    nameCell.title = tag;
    nameCell.style.cursor = "pointer";
    nameCell.addEventListener("click", () => {
      selectTagForEdges(tag);
    });

    const typeCell = document.createElement("td");
    typeCell.className = "tag-type";
    typeCell.textContent = "--";

    const rangeCell = document.createElement("td");
    rangeCell.className = "tag-range";

    const valueCell = document.createElement("td");
    valueCell.className = "tag-value";
    valueCell.textContent = "--";

    const newValueCell = document.createElement("td");
    newValueCell.className = "new-value-cell";

    const forceCell = document.createElement("td");
    const actionButtons = document.createElement("div");
    actionButtons.className = "row-actions";
    const forceBtn = document.createElement("button");
    forceBtn.className = "force-btn";
    forceBtn.textContent = "Force";
    forceBtn.title = "Toggle force override";
    forceBtn.addEventListener("click", () => {
      const entry = tagEntries.get(tag);
      if (!entry) return;
      if (entry.forced) {
        vscode.postMessage({ type: "unforce", tag });
      } else {
        const val = entry.pendingValue;
        if (val === undefined) return;
        vscode.postMessage({ type: "force", tag, value: val });
      }
    });
    const historyBtn = document.createElement("button");
    historyBtn.className = "history-btn";
    historyBtn.textContent = "\u23f1";
    historyBtn.title = "Watch tag history";
    historyBtn.addEventListener("click", () => {
      vscode.postMessage({ type: "watchHistory", tag });
    });
    const lockBtn = document.createElement("button");
    lockBtn.className = "lock-btn";
    lockBtn.style.display = "none";
    lockBtn.addEventListener("click", () => {
      const entry = tagEntries.get(tag);
      if (!entry) return;
      entry._readonlyUnlocked = !entry._readonlyUnlocked;
      setReadonlyLocked(entry, !entry._readonlyUnlocked);
    });
    actionButtons.appendChild(forceBtn);
    actionButtons.appendChild(historyBtn);
    actionButtons.appendChild(lockBtn);
    forceCell.appendChild(actionButtons);

    const removeCell = document.createElement("td");
    if (!isGroupMember) {
      const removeBtn = document.createElement("button");
      removeBtn.className = "btn-remove";
      removeBtn.textContent = "\u00d7";
      removeBtn.title = "Remove from Data View";
      removeBtn.addEventListener("click", () => {
        tagEntries.delete(tag);
        row.remove();
        refreshRowNumbers();
        checkEmpty();
        vscode.postMessage({ type: "removeTag", tag });
      });
      removeCell.appendChild(removeBtn);
    }

    row.appendChild(numCell);
    row.appendChild(nameCell);
    row.appendChild(typeCell);
    row.appendChild(rangeCell);
    row.appendChild(valueCell);
    row.appendChild(newValueCell);
    row.appendChild(forceCell);
    row.appendChild(removeCell);

    if (opts && opts.insertAfter) {
      opts.insertAfter.after(row);
    } else {
      tbody.appendChild(row);
    }

    const entry = {
      tagName: tag, row, valueEl: valueCell, typeEl: typeCell,
      rangeEl: rangeCell,
      newValueCell, forceBtn, tagHints: {},
      tagType: null, pendingValue: undefined, forced: false, rawValue: "--",
      _trueBtn: null, _falseBtn: null, _input: null, _select: null,
      _readonlyBadge: readonlyBadge,
      _publicBadge: publicBadge,
      _lockBtn: lockBtn,
      _readonlyUnlocked: false,
    };
    tagEntries.set(tag, entry);

    // Placeholder input until type known
    const input = document.createElement("input");
    input.className = "tag-input";
    input.type = "text";
    input.placeholder = "value";
    input.addEventListener("input", () => { entry.pendingValue = parseTypedValue(entry.tagType, input.value); });
    newValueCell.appendChild(input);
    entry._input = input;
    refreshRowNumbers();

    return entry;
  }

  function addTag(tag) {
    if (tagEntries.has(tag)) return;
    createTagRow(tag, {});
  }

  function addGroup(groupName, opts) {
    if (groupEntries.has(groupName)) return;

    const tbody = ensureTable();
    const headerRow = document.createElement("tr");
    headerRow.className = "group-header sortable-item";
    headerRow.dataset.itemType = "group";
    headerRow.dataset.itemName = groupName;

    const chevronCell = document.createElement("td");
    chevronCell.className = "row-num drag-handle";
    chevronCell.title = "Drag to reorder";

    const nameCell = document.createElement("td");
    nameCell.colSpan = 6;
    const groupToggle = document.createElement("span");
    groupToggle.className = "group-toggle";
    groupToggle.title = "Collapse/expand group";
    const chevron = document.createElement("span");
    chevron.className = "group-chevron";
    chevron.textContent = "\u25bc";
    groupToggle.appendChild(chevron);
    groupToggle.appendChild(document.createTextNode(groupName));
    nameCell.appendChild(groupToggle);

    const removeCell = document.createElement("td");
    const removeBtn = document.createElement("button");
    removeBtn.className = "btn-remove";
    removeBtn.textContent = "\u00d7";
    removeBtn.title = "Remove group from Data View";
    removeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const ge = groupEntries.get(groupName);
      if (ge) {
        for (const memberTag of ge.memberTags) {
          const entry = tagEntries.get(memberTag);
          if (entry) entry.row.remove();
          tagEntries.delete(memberTag);
        }
        headerRow.remove();
        groupEntries.delete(groupName);
        refreshRowNumbers();
        checkEmpty();
      }
      vscode.postMessage({ type: "removeGroup", group: groupName });
    });
    removeCell.appendChild(removeBtn);

    headerRow.appendChild(chevronCell);
    headerRow.appendChild(nameCell);
    headerRow.appendChild(removeCell);
    if (opts && opts.insertBefore) {
      tbody.insertBefore(headerRow, opts.insertBefore);
    } else {
      tbody.appendChild(headerRow);
    }
    refreshRowNumbers();

    const ge = { headerRow, chevron, memberTags: new Set(), collapsed: false };
    groupEntries.set(groupName, ge);

    groupToggle.addEventListener("click", () => {
      ge.collapsed = !ge.collapsed;
      chevron.textContent = ge.collapsed ? "\u25b6" : "\u25bc";
      for (const memberTag of ge.memberTags) {
        const entry = tagEntries.get(memberTag);
        if (entry) entry.row.style.display = ge.collapsed ? "none" : "";
      }
    });
  }

  function ensureGroupMembers(groupName, memberTags) {
    const ge = groupEntries.get(groupName);
    if (!ge) return;

    for (const memberTag of memberTags) {
      if (ge.memberTags.has(memberTag)) continue;

      // Remove duplicate individual entry if it exists
      if (tagEntries.has(memberTag) && !ge.memberTags.has(memberTag)) {
        const old = tagEntries.get(memberTag);
        old.row.remove();
        tagEntries.delete(memberTag);
        vscode.postMessage({ type: "removeTag", tag: memberTag });
      }

      ge.memberTags.add(memberTag);

      // Derive display name: strip group prefix
      let displayName = memberTag;
      if (memberTag.startsWith(groupName)) {
        const suffix = memberTag.slice(groupName.length);
        // Remove leading digit(s) and underscore: "DetTimer1_Done" -> "1.Done", "DetTimer_Done" -> ".Done"
        displayName = "." + suffix.replace(/^\d*_/, "");
      }

      // Find the last row belonging to this group to insert after
      let insertAfter = ge.headerRow;
      for (const existing of ge.memberTags) {
        const e = tagEntries.get(existing);
        if (e) insertAfter = e.row;
      }

      const entry = createTagRow(memberTag, {
        groupMember: true,
        groupName,
        displayName,
        insertAfter,
      });
      if (ge.collapsed) entry.row.style.display = "none";
    }

    syncGroupBlockPositions(groupName);
    refreshRowNumbers();
  }

  function updateForceState(entry, isForced) {
    if (entry.forced === isForced) return;
    entry.forced = isForced;
    entry.forceBtn.textContent = isForced ? "Unforce" : "Force";
    entry.forceBtn.classList.toggle("active", isForced);
    entry.row.classList.toggle("forced", isForced);
  }

  function checkEmpty() {
    if (tagEntries.size === 0 && groupEntries.size === 0) {
      destroySortable();
      content.innerHTML = '<div class="empty">Right-click a tag in the editor and select "Add to Data View"</div>';
      toolbar.style.display = "none";
    }
  }

  function clearPendingValues() {
    for (const entry of tagEntries.values()) {
      entry.pendingValue = undefined;
      if (entry._input) entry._input.value = "";
      syncPendingControl(entry);
    }
  }

  writeBtn.addEventListener("click", () => {
    const patches = {};
    for (const [tag, entry] of tagEntries.entries()) {
      if (entry.pendingValue !== undefined) {
        patches[tag] = entry.pendingValue;
      }
    }
    if (Object.keys(patches).length > 0) {
      vscode.postMessage({ type: "writeAll", patches });
    }
  });

  clearBtn.addEventListener("click", clearPendingValues);

  publicFilter.addEventListener("change", () => {
    publicFilterActive = publicFilter.checked;
    applyPublicFilter();
  });

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (msg.type === "update") {
      if (!hintsReceived && msg.tagHints && Object.keys(msg.tagHints).length > 0) {
        hintsReceived = true;
        publicFilter.disabled = false;
        publicFilterLabel.classList.remove("disabled");
        publicFilterLabel.title = "Show only tags declared public";
      }
      // Auto-promote individual tags that are actually group names
      for (const groupName of Object.keys(msg.tagGroups || {})) {
        if (tagEntries.has(groupName) && !groupEntries.has(groupName)) {
          const entry = tagEntries.get(groupName);
          const nextSibling = entry.row.nextElementSibling;
          entry.row.remove();
          tagEntries.delete(groupName);
          addGroup(groupName, { insertBefore: nextSibling });
          vscode.postMessage({ type: "promoteToGroup", tag: groupName });
        }
      }
      // Expand group members from tagGroups
      for (const [groupName, members] of Object.entries(msg.tagGroups || {})) {
        ensureGroupMembers(groupName, members);
      }
      // Update values, types, forces
      for (const [tag, entry] of tagEntries.entries()) {
        const nextHints = msg.tagHints && tag in msg.tagHints ? msg.tagHints[tag] : {};
        const hintsChanged = JSON.stringify(entry.tagHints) !== JSON.stringify(nextHints);
        if (hintsChanged) {
          entry.tagHints = nextHints;
          applyReadonlyHint(entry);
          applyPublicHint(entry);
          applyRangeHint(entry);
        }
        if (tag in msg.tagTypes && entry.tagType !== msg.tagTypes[tag]) {
          entry.tagType = msg.tagTypes[tag];
          entry.typeEl.textContent = entry.tagType;
          buildNewValueCell(entry);
        } else if (hintsChanged) {
          buildNewValueCell(entry);
        }
        if (tag in msg.tagValues) {
          updateValueDisplay(entry, msg.tagValues[tag]);
          syncPendingControl(entry);
        }
        updateForceState(entry, tag in msg.forces);
      }
      if (publicFilterActive) applyPublicFilter();
      errorEl.textContent = "";
    } else if (msg.type === "queryResults") {
      renderQueryResults(msg.tags, msg.roles);
    } else if (msg.type === "graphData") {
      graphData = msg.data;
      neighborIndex = buildNeighborIndex(msg.data);
    } else if (msg.type === "addTag") {
      addTag(msg.tag);
    } else if (msg.type === "addGroup") {
      addGroup(msg.group);
    } else if (msg.type === "reset") {
      hintsReceived = false;
      publicFilter.disabled = true;
      publicFilter.checked = false;
      publicFilterActive = false;
      publicFilterLabel.classList.add("disabled");
      publicFilterLabel.title = "Start debugger to enable";
      graphData = null;
      neighborIndex = {};
      focusRing.length = 0;
      document.querySelectorAll(".edge-row").forEach((r) => r.remove());
      for (const entry of tagEntries.values()) {
        entry.valueEl.textContent = "--";
        entry.rawValue = "--";
        entry.row.style.display = "";
        updateForceState(entry, false);
      }
      for (const ge of groupEntries.values()) {
        ge.headerRow.style.display = "";
      }
    } else if (msg.type === "error") {
      errorEl.textContent = msg.text;
      setTimeout(() => { errorEl.textContent = ""; }, 5000);
    }
  });
</script>
</body>
</html>`;
  }
}

module.exports = {
  PyrungDataViewProvider,
};
