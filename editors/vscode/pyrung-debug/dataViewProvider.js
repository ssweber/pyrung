const vscode = require("vscode");

class PyrungDataViewProvider {
  constructor() {
    this._view = null;
    this._session = null;
    this._watchedTags = new Set();
    this._watchedGroups = new Set();
    this._latestTagGroups = {};
  }

  resolveWebviewView(webviewView) {
    this._view = webviewView;
    webviewView.webview.options = { enableScripts: true };
    webviewView.webview.html = this._html();

    webviewView.webview.onDidReceiveMessage(async (message) => {
      if (!this._session) return;
      if (message.type === "force" && message.tag) {
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
      } else if (message.type === "removeTag") {
        this._watchedTags.delete(message.tag);
      } else if (message.type === "removeGroup") {
        this._watchedGroups.delete(message.group);
      } else if (message.type === "promoteToGroup" && message.tag) {
        this._watchedTags.delete(message.tag);
        this._watchedGroups.add(message.tag);
      }
    });

    webviewView.onDidDispose(() => {
      this._view = null;
    });

    // Restore watched items if webview was re-created
    for (const tag of this._watchedTags) {
      this._postMessage({ type: "addTag", tag });
    }
    for (const group of this._watchedGroups) {
      this._postMessage({ type: "addGroup", group });
    }
  }

  setSession(session) {
    this._session = session;
    if (!session) {
      this._postMessage({ type: "reset" });
    }
  }

  addTag(tagName) {
    if (!tagName) return;
    if (tagName in this._latestTagGroups) {
      this.addGroup(tagName);
      return;
    }
    this._watchedTags.add(tagName);
    this._postMessage({ type: "addTag", tag: tagName });
  }

  addGroup(groupName) {
    if (!groupName) return;
    this._watchedGroups.add(groupName);
    this._postMessage({ type: "addGroup", group: groupName });
  }

  updateTrace(tagValues, forces, tagTypes, tagGroups) {
    this._latestTagGroups = tagGroups || {};
    if (!this._view) return;
    // Collect all relevant tags: individually watched + group members
    const relevantTags = new Set(this._watchedTags);
    for (const group of this._watchedGroups) {
      const members = tagGroups[group];
      if (members) {
        for (const m of members) relevantTags.add(m);
      }
    }

    const filteredValues = {};
    const filteredTypes = {};
    const filteredForces = {};
    for (const tag of relevantTags) {
      if (tag in tagValues) filteredValues[tag] = tagValues[tag];
      if (tag in tagTypes) filteredTypes[tag] = tagTypes[tag];
      if (tag in forces) filteredForces[tag] = forces[tag];
    }

    const filteredGroups = {};
    for (const group of this._watchedGroups) {
      if (group in tagGroups) filteredGroups[group] = tagGroups[group];
    }

    this._postMessage({
      type: "update",
      tagValues: filteredValues,
      tagTypes: filteredTypes,
      forces: filteredForces,
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
    cursor: pointer;
    user-select: none;
  }
  .group-header td:hover { opacity: 0.8; }
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
  .tag-type {
    color: var(--vscode-descriptionForeground);
    font-size: 0.85em;
    white-space: nowrap;
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
</style>
</head>
<body>
  <div class="toolbar" id="toolbar" style="display:none;">
    <button class="toolbar-btn" id="write-btn" title="Patch all pending new values (one-scan)">Write Values</button>
    <button class="toolbar-btn" id="clear-btn" title="Clear all pending new values">Clear</button>
  </div>
  <div id="content">
    <div class="empty">Right-click a tag in the editor and select "Add to Data View"</div>
  </div>
  <div id="error"></div>
<script>
  const vscode = acquireVsCodeApi();
  const toolbar = document.getElementById("toolbar");
  const writeBtn = document.getElementById("write-btn");
  const clearBtn = document.getElementById("clear-btn");
  const content = document.getElementById("content");
  const errorEl = document.getElementById("error");

  // Individual tags: tag -> entry
  const tagEntries = new Map();
  // Groups: groupName -> { headerRow, memberTags: Set, collapsed }
  const groupEntries = new Map();
  let rowCounter = 0;

  function parseNumeric(str) {
    const s = str.trim();
    if (s === "") return undefined;
    const num = Number(s);
    if (!isNaN(num) && s !== "") return num;
    return undefined;
  }

  function ensureTable() {
    let table = document.getElementById("tag-table");
    if (!table) {
      content.innerHTML =
        '<table class="tag-table" id="tag-table">' +
        "<thead><tr>" +
        '<th class="row-num">No.</th>' +
        "<th>Tag</th><th>Type</th><th>Value</th>" +
        "<th>New Value</th><th>Force</th><th></th>" +
        "</tr></thead>" +
        '<tbody id="tag-body"></tbody></table>';
      toolbar.style.display = "flex";
    }
    return document.getElementById("tag-body");
  }

  function setPendingValue(entry, value) {
    entry.pendingValue = value;
    // If already forced, immediately update the force to the new value
    if (entry.forced && value !== undefined) {
      vscode.postMessage({ type: "force", tag: entry.tagName, value });
    }
  }

  function buildNewValueCell(entry) {
    const cell = entry.newValueCell;
    cell.innerHTML = "";

    if (entry.tagType === "bool") {
      const trueBtn = document.createElement("button");
      trueBtn.className = "bool-btn";
      trueBtn.textContent = "True";
      trueBtn.addEventListener("click", () => {
        setPendingValue(entry, true);
        trueBtn.classList.add("selected");
        falseBtn.classList.remove("selected");
      });
      trueBtn.addEventListener("dblclick", () => {
        vscode.postMessage({ type: "patchSingle", tag: entry.tagName, value: true });
      });

      const falseBtn = document.createElement("button");
      falseBtn.className = "bool-btn";
      falseBtn.textContent = "False";
      falseBtn.addEventListener("click", () => {
        setPendingValue(entry, false);
        falseBtn.classList.add("selected");
        trueBtn.classList.remove("selected");
      });
      falseBtn.addEventListener("dblclick", () => {
        vscode.postMessage({ type: "patchSingle", tag: entry.tagName, value: false });
      });

      cell.appendChild(trueBtn);
      cell.appendChild(falseBtn);
      entry._trueBtn = trueBtn;
      entry._falseBtn = falseBtn;
      entry._input = null;
    } else {
      const input = document.createElement("input");
      input.className = "tag-input";
      input.type = "text";
      input.placeholder = entry.tagType || "value";
      input.addEventListener("input", () => {
        entry.pendingValue = parseNumeric(input.value);
      });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          const val = parseNumeric(input.value);
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
      entry._trueBtn = null;
      entry._falseBtn = null;
    }
  }

  function createTagRow(tag, opts) {
    const isGroupMember = opts && opts.groupMember;
    rowCounter++;

    const tbody = ensureTable();
    const row = document.createElement("tr");
    row.className = "tag-row" + (isGroupMember ? " group-member" : "");

    const numCell = document.createElement("td");
    numCell.className = "row-num";
    numCell.textContent = String(rowCounter).padStart(3, "0");

    const nameCell = document.createElement("td");
    nameCell.className = "tag-name";
    // For group members, show just the field part after the group prefix
    const displayName = (isGroupMember && opts.displayName) ? opts.displayName : tag;
    nameCell.textContent = displayName;
    nameCell.title = tag;

    const typeCell = document.createElement("td");
    typeCell.className = "tag-type";
    typeCell.textContent = "--";

    const valueCell = document.createElement("td");
    valueCell.className = "tag-value";
    valueCell.textContent = "--";

    const newValueCell = document.createElement("td");
    newValueCell.className = "new-value-cell";

    const forceCell = document.createElement("td");
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
    forceCell.appendChild(forceBtn);

    const removeCell = document.createElement("td");
    if (!isGroupMember) {
      const removeBtn = document.createElement("button");
      removeBtn.className = "btn-remove";
      removeBtn.textContent = "\u00d7";
      removeBtn.title = "Remove from Data View";
      removeBtn.addEventListener("click", () => {
        tagEntries.delete(tag);
        row.remove();
        checkEmpty();
        vscode.postMessage({ type: "removeTag", tag });
      });
      removeCell.appendChild(removeBtn);
    }

    row.appendChild(numCell);
    row.appendChild(nameCell);
    row.appendChild(typeCell);
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
      newValueCell, forceBtn,
      tagType: null, pendingValue: undefined, forced: false,
      _trueBtn: null, _falseBtn: null, _input: null,
    };
    tagEntries.set(tag, entry);

    // Placeholder input until type known
    const input = document.createElement("input");
    input.className = "tag-input";
    input.type = "text";
    input.placeholder = "value";
    input.addEventListener("input", () => { entry.pendingValue = parseNumeric(input.value); });
    newValueCell.appendChild(input);
    entry._input = input;

    return entry;
  }

  function addTag(tag) {
    if (tagEntries.has(tag)) return;
    createTagRow(tag, {});
  }

  function addGroup(groupName) {
    if (groupEntries.has(groupName)) return;

    const tbody = ensureTable();
    const headerRow = document.createElement("tr");
    headerRow.className = "group-header";

    const chevronCell = document.createElement("td");
    chevronCell.className = "row-num";
    const chevron = document.createElement("span");
    chevron.className = "group-chevron";
    chevron.textContent = "\u25bc";
    chevronCell.appendChild(chevron);

    const nameCell = document.createElement("td");
    nameCell.colSpan = 5;
    nameCell.textContent = groupName;

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
        checkEmpty();
      }
      vscode.postMessage({ type: "removeGroup", group: groupName });
    });
    removeCell.appendChild(removeBtn);

    headerRow.appendChild(chevronCell);
    headerRow.appendChild(nameCell);
    headerRow.appendChild(removeCell);
    tbody.appendChild(headerRow);

    const ge = { headerRow, chevron, memberTags: new Set(), collapsed: false };
    groupEntries.set(groupName, ge);

    headerRow.addEventListener("click", () => {
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

      const entry = createTagRow(memberTag, { groupMember: true, displayName, insertAfter });
      if (ge.collapsed) entry.row.style.display = "none";
    }
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
      content.innerHTML = '<div class="empty">Right-click a tag in the editor and select "Add to Data View"</div>';
      toolbar.style.display = "none";
      rowCounter = 0;
    }
  }

  function clearPendingValues() {
    for (const entry of tagEntries.values()) {
      entry.pendingValue = undefined;
      if (entry._trueBtn) entry._trueBtn.classList.remove("selected");
      if (entry._falseBtn) entry._falseBtn.classList.remove("selected");
      if (entry._input) entry._input.value = "";
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

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (msg.type === "update") {
      // Auto-promote individual tags that are actually group names
      for (const groupName of Object.keys(msg.tagGroups || {})) {
        if (tagEntries.has(groupName) && !groupEntries.has(groupName)) {
          const entry = tagEntries.get(groupName);
          entry.row.remove();
          tagEntries.delete(groupName);
          addGroup(groupName);
          vscode.postMessage({ type: "promoteToGroup", tag: groupName });
        }
      }
      // Expand group members from tagGroups
      for (const [groupName, members] of Object.entries(msg.tagGroups || {})) {
        ensureGroupMembers(groupName, members);
      }
      // Update values, types, forces
      for (const [tag, entry] of tagEntries.entries()) {
        if (tag in msg.tagTypes && entry.tagType !== msg.tagTypes[tag]) {
          entry.tagType = msg.tagTypes[tag];
          entry.typeEl.textContent = entry.tagType;
          buildNewValueCell(entry);
        }
        if (tag in msg.tagValues) {
          entry.valueEl.textContent = msg.tagValues[tag];
        }
        updateForceState(entry, tag in msg.forces);
      }
      errorEl.textContent = "";
    } else if (msg.type === "addTag") {
      addTag(msg.tag);
    } else if (msg.type === "addGroup") {
      addGroup(msg.group);
    } else if (msg.type === "reset") {
      for (const entry of tagEntries.values()) {
        entry.valueEl.textContent = "--";
        updateForceState(entry, false);
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
