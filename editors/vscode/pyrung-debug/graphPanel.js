const fs = require("fs");
const path = require("path");
const vscode = require("vscode");

class PyrungGraphPanelProvider {
  constructor(options = {}) {
    this._panel = null;
    this._isReady = false;
    this._graphData = null;
    this._latestTrace = null;
    this._session = null;
    this._extensionUri = null;
    this._onAddToDataView =
      typeof options.onAddToDataView === "function" ? options.onAddToDataView : null;
    this._onAddToHistory =
      typeof options.onAddToHistory === "function" ? options.onAddToHistory : null;
    this._cytoscapeScript = "";
    this._dagreScript = "";
    this._cytoscapeDagreScript = "";
    this._loadVendorScripts();
  }

  _loadVendorScripts() {
    const vendorDir = path.join(__dirname, "vendor");
    try {
      this._dagreScript = fs.readFileSync(path.join(vendorDir, "dagre.min.js"), "utf8");
    } catch (e) {
      console.error("Failed to load dagre vendor bundle:", e);
    }
    try {
      this._cytoscapeScript = fs.readFileSync(path.join(vendorDir, "cytoscape.min.js"), "utf8");
    } catch (e) {
      console.error("Failed to load Cytoscape vendor bundle:", e);
    }
    try {
      this._cytoscapeDagreScript = fs.readFileSync(
        path.join(vendorDir, "cytoscape-dagre.min.js"),
        "utf8"
      );
    } catch (e) {
      console.error("Failed to load cytoscape-dagre vendor bundle:", e);
    }
  }

  show(session) {
    this._session = session;
    if (this._panel) {
      this._panel.reveal(vscode.ViewColumn.Beside);
      return;
    }
    this._panel = vscode.window.createWebviewPanel(
      "pyrung.graphView",
      "pyrung: Graph View",
      vscode.ViewColumn.Beside,
      { enableScripts: true, retainContextWhenHidden: true }
    );
    this._isReady = false;
    this._panel.webview.html = this._html();

    this._panel.webview.onDidReceiveMessage(async (message) => {
      if (message.type === "ready") {
        this._isReady = true;
        if (this._graphData) {
          this._postGraph();
        }
        if (this._latestTrace) {
          this._postTrace();
        }
        return;
      }

      if (message.type === "goToSource") {
        const { sourceFile, sourceLine } = message;
        if (!sourceFile) return;
        try {
          const uri = vscode.Uri.file(sourceFile);
          const line = typeof sourceLine === "number" ? Math.max(0, sourceLine - 1) : 0;
          const range = new vscode.Range(line, 0, line, 0);
          await vscode.window.showTextDocument(uri, {
            selection: range,
            preserveFocus: false,
          });
        } catch (e) {
          console.error("Failed to open source:", e);
        }
        return;
      }

      if (message.type === "addToDataView") {
        if (this._onAddToDataView) this._onAddToDataView(message.tag);
        return;
      }

      if (message.type === "addToHistory") {
        if (this._onAddToHistory) this._onAddToHistory(message.tag);
        return;
      }

      // Slice is computed locally inside the webview — no DAP round-trip needed.
    });

    this._panel.onDidDispose(() => {
      this._panel = null;
      this._isReady = false;
    });
  }

  updateGraph(graphData) {
    this._graphData = graphData;
    this._postGraph();
  }

  updateTrace(tagValues, forces) {
    this._latestTrace = { tagValues, forces };
    this._postTrace();
  }

  dispose() {
    if (this._panel) {
      this._panel.dispose();
      this._panel = null;
    }
    this._isReady = false;
    this._graphData = null;
    this._latestTrace = null;
    this._session = null;
  }

  _postGraph() {
    if (!this._panel || !this._isReady || !this._graphData) return;
    this._panel.webview.postMessage({ type: "graph", data: this._graphData });
  }

  _postTrace() {
    if (!this._panel || !this._isReady || !this._latestTrace) return;
    this._panel.webview.postMessage({
      type: "trace",
      tagValues: this._latestTrace.tagValues,
      forces: this._latestTrace.forces,
    });
  }

  _html() {
    return /* html */ `<!DOCTYPE html>
<html>
<head>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { width: 100%; height: 100%; overflow: hidden; }
  body {
    font-family: var(--vscode-font-family);
    font-size: var(--vscode-font-size);
    color: var(--vscode-foreground);
    background: var(--vscode-editor-background);
    display: flex;
    flex-direction: column;
  }

  /* ---- Toolbar ---- */
  .toolbar {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 10px;
    border-bottom: 1px solid var(--vscode-widget-border, rgba(128,128,128,0.35));
    flex-shrink: 0;
    flex-wrap: wrap;
  }
  .search-input {
    padding: 4px 7px;
    min-width: 120px;
    border: 1px solid var(--vscode-input-border, var(--vscode-widget-border, #444));
    background: var(--vscode-input-background);
    color: var(--vscode-input-foreground);
    font-size: inherit;
  }
  .search-input:focus { outline: 1px solid var(--vscode-focusBorder); outline-offset: 0; }
  .role-btn {
    padding: 3px 8px;
    border: 1px solid var(--vscode-button-secondaryBackground);
    background: var(--vscode-button-secondaryBackground);
    color: var(--vscode-button-secondaryForeground);
    cursor: pointer;
    font-size: 0.85em;
    border-radius: 3px;
  }
  .role-btn:hover { background: var(--vscode-button-secondaryHoverBackground); }
  .role-btn.active {
    background: var(--vscode-button-background);
    color: var(--vscode-button-foreground);
    border-color: var(--vscode-button-background);
  }
  .toolbar-spacer { flex: 1; }
  .reset-btn {
    padding: 3px 8px;
    border: 1px solid transparent;
    background: var(--vscode-button-secondaryBackground);
    color: var(--vscode-button-secondaryForeground);
    cursor: pointer;
    font-size: 0.85em;
    border-radius: 3px;
  }
  .reset-btn:hover { background: var(--vscode-button-secondaryHoverBackground); }

  /* ---- Main area ---- */
  .main {
    display: flex;
    flex: 1;
    min-height: 0;
  }
  #cy {
    flex: 1;
    min-width: 0;
  }

  /* ---- Info sidebar ---- */
  .info-panel {
    width: 170px;
    border-left: 1px solid var(--vscode-widget-border, rgba(128,128,128,0.35));
    padding: 10px;
    overflow-y: auto;
    flex-shrink: 0;
    display: none;
    font-size: 0.9em;
  }
  .info-panel.visible { display: block; }
  .info-title {
    font-weight: 600;
    font-size: 1.1em;
    margin-bottom: 6px;
    word-break: break-all;
  }
  .info-row {
    margin-bottom: 4px;
    color: var(--vscode-descriptionForeground);
  }
  .info-row b { color: var(--vscode-foreground); }
  .info-section {
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px solid var(--vscode-widget-border, rgba(128,128,128,0.2));
  }
  .info-section-title {
    font-weight: 600;
    margin-bottom: 4px;
  }
  .info-list {
    list-style: none;
    padding: 0;
  }
  .info-list li {
    padding: 1px 0;
    font-family: var(--vscode-editor-font-family);
    font-size: 0.95em;
  }

  /* ---- Context menu ---- */
  .ctx-menu {
    position: absolute;
    background: var(--vscode-menu-background, var(--vscode-editorWidget-background));
    color: var(--vscode-menu-foreground, var(--vscode-foreground));
    border: 1px solid var(--vscode-menu-border, var(--vscode-widget-border, #555));
    border-radius: 4px;
    padding: 4px 0;
    z-index: 9999;
    min-width: 160px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3);
    display: none;
  }
  .ctx-menu.visible { display: block; }
  .ctx-item {
    padding: 5px 16px;
    cursor: pointer;
    white-space: nowrap;
  }
  .ctx-item:hover {
    background: var(--vscode-menu-selectionBackground, var(--vscode-list-activeSelectionBackground));
    color: var(--vscode-menu-selectionForeground, var(--vscode-list-activeSelectionForeground));
  }

  /* ---- Tooltip ---- */
  .graph-tooltip {
    position: absolute;
    background: var(--vscode-editorHoverWidget-background, var(--vscode-editorWidget-background));
    color: var(--vscode-editorHoverWidget-foreground, var(--vscode-foreground));
    border: 1px solid var(--vscode-editorHoverWidget-border, var(--vscode-widget-border, #555));
    border-radius: 4px;
    padding: 6px 10px;
    font-size: 0.85em;
    pointer-events: none;
    z-index: 9998;
    display: none;
    max-width: 300px;
    word-break: break-word;
  }
  .graph-tooltip.visible { display: block; }

  /* ---- Slice buttons in info panel ---- */
  .slice-btns {
    display: flex;
    gap: 4px;
    margin-bottom: 8px;
  }
  .slice-btn {
    flex: 1;
    padding: 4px 6px;
    border: 1px solid var(--vscode-button-secondaryBackground);
    background: var(--vscode-button-secondaryBackground);
    color: var(--vscode-button-secondaryForeground);
    cursor: pointer;
    font-size: 0.8em;
    border-radius: 3px;
    white-space: nowrap;
  }
  .slice-btn:hover { background: var(--vscode-button-secondaryHoverBackground); }

  /* ---- Slice banner ---- */
  .slice-banner {
    display: none;
    align-items: center;
    gap: 8px;
    padding: 4px 10px;
    background: var(--vscode-editorWidget-background, rgba(60,60,60,0.9));
    border-bottom: 1px solid var(--vscode-widget-border, rgba(128,128,128,0.35));
    font-size: 0.85em;
    flex-shrink: 0;
  }
  .slice-banner.visible { display: flex; }
  .slice-banner-text { flex: 1; }
  .slice-banner-close {
    background: none;
    border: none;
    color: var(--vscode-foreground);
    cursor: pointer;
    font-size: 1.1em;
    padding: 0 4px;
  }
  .slice-banner-close:hover { color: var(--vscode-errorForeground, #f44); }

  /* ---- Grid layout banner ---- */
  .grid-banner {
    display: none;
    padding: 4px 10px;
    background: var(--vscode-editorWidget-background, rgba(60,60,60,0.9));
    border-bottom: 1px solid var(--vscode-widget-border, rgba(128,128,128,0.35));
    font-size: 0.85em;
    flex-shrink: 0;
    color: var(--vscode-descriptionForeground);
  }
  .grid-banner.visible { display: block; }
</style>
</head>
<body>
  <div class="toolbar">
    <input id="search" class="search-input" type="text" placeholder="Search tags..." />
    <button class="role-btn active" data-role="input" title="Inputs">I</button>
    <button class="role-btn active" data-role="pivot" title="Pivots">P</button>
    <button class="role-btn active" data-role="terminal" title="Terminals">T</button>
    <button class="role-btn active" data-role="isolated" title="Isolated">X</button>
    <span class="toolbar-spacer"></span>
    <button id="rung-order-btn" class="role-btn active" title="Sort layout by rung order (ladder top-down)">Rung Order</button>
    <button id="reset-btn" class="reset-btn" title="Reset layout, pins, and hidden nodes">Reset</button>
  </div>
  <div id="slice-banner" class="slice-banner">
    <span id="slice-banner-text" class="slice-banner-text"></span>
    <button id="slice-banner-close" class="slice-banner-close" title="Clear slice">\u2715</button>
  </div>
  <div id="grid-banner" class="grid-banner"></div>
  <div class="main">
    <div id="cy"></div>
    <div id="info-panel" class="info-panel"></div>
  </div>
  <div id="ctx-menu" class="ctx-menu"></div>
  <div id="tooltip" class="graph-tooltip"></div>

<script>${this._dagreScript}</script>
<script>${this._cytoscapeScript}</script>
<script>${this._cytoscapeDagreScript}</script>
<script>
(function() {
  const vscodeApi = acquireVsCodeApi();
  const cyContainer = document.getElementById("cy");
  const infoPanel = document.getElementById("info-panel");
  const ctxMenu = document.getElementById("ctx-menu");
  const tooltip = document.getElementById("tooltip");
  const searchInput = document.getElementById("search");
  const resetBtn = document.getElementById("reset-btn");
  const rungOrderBtn = document.getElementById("rung-order-btn");
  const roleButtons = document.querySelectorAll(".role-btn[data-role]");
  const sliceBanner = document.getElementById("slice-banner");
  const sliceBannerText = document.getElementById("slice-banner-text");
  const sliceBannerClose = document.getElementById("slice-banner-close");
  const gridBanner = document.getElementById("grid-banner");

  // ---- State ----
  let cy = null;
  let graphData = null;
  let tagValues = {};
  let forces = {};
  let selectedNode = null;
  let sliceHighlight = null; // {tag, upstream:[], downstream:[]}
  const activeRoles = new Set(["input", "pivot", "terminal", "isolated"]);
  let searchNeedle = "";
  let pinnedPositions = {};
  let hiddenTags = new Set();
  let rungOrderEnabled = true;

  // Load workspace state
  const savedState = vscodeApi.getState() || {};
  if (savedState.pinnedPositions) pinnedPositions = savedState.pinnedPositions;
  if (Array.isArray(savedState.hiddenTags)) hiddenTags = new Set(savedState.hiddenTags);

  function saveWorkspaceState() {
    vscodeApi.setState({
      pinnedPositions,
      hiddenTags: Array.from(hiddenTags),
    });
  }

  // ---- Abbreviation-aware search (ported from Python TagNameMatcher) ----
  const VOWELS = new Set("aeiou");

  function splitWords(text) {
    return text.split(/[_\\s]+|(?<=[a-z])(?=[A-Z])/).filter(w => w.length >= 1);
  }

  function consonantsAbbr(word) {
    const lower = word.toLowerCase();
    const result = [lower[0]];
    for (let i = 1; i < lower.length; i++) {
      if (!VOWELS.has(lower[i])) result.push(lower[i]);
    }
    const final = [result[0]];
    for (let i = 1; i < result.length; i++) {
      if (result[i] !== final[final.length - 1]) final.push(result[i]);
    }
    return final.join("");
  }

  function reducedConsonantsAbbr(word) {
    const lower = word.toLowerCase();
    const result = [lower[0]];
    for (let i = 1; i < lower.length; i++) {
      const ch = lower[i];
      if (VOWELS.has(ch)) continue;
      if (VOWELS.has(lower[i - 1]) && i + 1 < lower.length && !VOWELS.has(lower[i + 1])) continue;
      result.push(ch);
    }
    const final = [result[0]];
    for (let i = 1; i < result.length; i++) {
      if (result[i] !== final[final.length - 1]) final.push(result[i]);
    }
    return final.join("");
  }

  function abbreviations(word) {
    const lower = word.toLowerCase();
    if (new Set(lower).size <= 1) return [lower];
    if (lower.length <= 3) return [lower];
    const hasVowelAfterFirst = Array.from(lower.slice(1)).some(c => VOWELS.has(c));
    if (!hasVowelAfterFirst) return [lower];
    const variants = [];
    const c = consonantsAbbr(lower);
    if (c.length >= 2) {
      variants.push(c);
      const r = reducedConsonantsAbbr(lower);
      if (r.length >= 2 && r !== c) variants.push(r);
    }
    return variants;
  }

  function generateTokens(name) {
    const words = splitWords(name).filter(w => w.length >= 2);
    const tokens = [];
    for (const w of words) {
      if (w.length >= 4) tokens.push(w.toLowerCase());
      tokens.push(...abbreviations(w));
    }
    return [...new Set(tokens)];
  }

  // Build search index from tag names
  let searchIndex = {}; // tagName -> tokens[]

  function buildSearchIndex(tagNames) {
    searchIndex = {};
    for (const name of tagNames) {
      searchIndex[name] = generateTokens(name);
    }
  }

  function matchesSearch(tagName, needle) {
    if (!needle) return true;
    const lower = needle.toLowerCase();
    if (tagName.toLowerCase().includes(lower)) return true;
    const tokens = searchIndex[tagName] || [];
    const needleVariants = [lower, ...abbreviations(lower)];
    return tokens.some(tok => needleVariants.some(v => tok.startsWith(v)));
  }

  // ---- Rung ID parsing ----
  function parseRungId(id) {
    return parseInt(id.split(":")[1], 10);
  }

  // ---- Node colors ----
  const ROLE_COLORS = {
    input:    { bg: "#4A90D9", border: "#3570B0", text: "#fff" },
    pivot:    { bg: "#D9A441", border: "#B0832E", text: "#1a1a1a" },
    terminal: { bg: "#5CB85C", border: "#449944", text: "#1a1a1a" },
    isolated: { bg: "#888",    border: "#666",    text: "#fff" },
  };
  const RUNG_COLOR = { bg: "rgba(128,128,128,0.15)", border: "rgba(128,128,128,0.4)", text: "var(--vscode-foreground, #ccc)" };

  // ---- Build Cytoscape elements ----
  function buildElements(data) {
    const elements = [];
    const tagRoles = data.tagRoles || {};
    const rungNodes = data.rungNodes || [];
    const graphEdges = data.graphEdges || [];
    const tagNames = Object.keys(tagRoles);

    buildSearchIndex(tagNames);

    const physicalInputSet = data._physicalInputs ? new Set(data._physicalInputs) : null;
    const physicalOutputSet = data._physicalOutputs ? new Set(data._physicalOutputs) : null;

    // Compute rung affinity for each tag — minimum rung index it connects to.
    // This lets us sort tags vertically by rung order (ladder is top-down).
    const tagRungAffinity = {};
    for (const edge of graphEdges) {
      const src = edge.source;
      const tgt = edge.target;
      let tagName, rungId;
      if (src.startsWith("rung:") && !tgt.startsWith("rung:")) {
        rungId = src; tagName = tgt;
      } else if (tgt.startsWith("rung:") && !src.startsWith("rung:")) {
        rungId = tgt; tagName = src;
      } else continue;
      const idx = parseRungId(rungId);
      if (!(tagName in tagRungAffinity) || idx < tagRungAffinity[tagName]) {
        tagRungAffinity[tagName] = idx;
      }
    }

    // Tag nodes
    for (const name of tagNames) {
      if (hiddenTags.has(name)) continue;
      const role = tagRoles[name] || "isolated";
      const colors = ROLE_COLORS[role] || ROLE_COLORS.isolated;
      const isPhysicalInput = physicalInputSet && physicalInputSet.has(name);
      const isPhysicalOutput = physicalOutputSet && physicalOutputSet.has(name);

      elements.push({
        group: "nodes",
        data: {
          id: name,
          label: name,
          nodeType: "tag",
          role,
          rungAffinity: tagRungAffinity[name] ?? 9999,
          borderWidth: (isPhysicalInput || isPhysicalOutput) ? 4 : 2,
          bgColor: colors.bg,
          borderColor: colors.border,
          textColor: colors.text,
          shape: role === "pivot" ? "hexagon" :
                 role === "isolated" ? "ellipse" : "round-rectangle",
        },
        position: pinnedPositions[name] || undefined,
      });
    }

    // Rung nodes
    const rungSet = new Set();
    for (const edge of graphEdges) {
      if (edge.source.startsWith("rung:")) rungSet.add(edge.source);
      if (edge.target.startsWith("rung:")) rungSet.add(edge.target);
    }

    for (const rungId of rungSet) {
      const idx = parseRungId(rungId);
      const rungNode = rungNodes[idx];
      if (!rungNode) continue;
      const label = "R" + (rungNode.rungIndex + 1);
      elements.push({
        group: "nodes",
        data: {
          id: rungId,
          label,
          nodeType: "rung",
          rungIdx: idx,
          sourceFile: rungNode.sourceFile,
          sourceLine: rungNode.sourceLine,
          bgColor: RUNG_COLOR.bg,
          borderColor: RUNG_COLOR.border,
          textColor: RUNG_COLOR.text,
          shape: "round-rectangle",
          borderWidth: 1,
        },
        position: pinnedPositions[rungId] || undefined,
      });
    }

    // Build node ID set for O(1) edge-endpoint lookup
    const nodeIds = new Set(elements.map(e => e.data.id));

    // Edges
    let edgeId = 0;
    for (const edge of graphEdges) {
      const src = edge.source;
      const tgt = edge.target;
      // Skip edges to/from hidden tags
      if (!src.startsWith("rung:") && hiddenTags.has(src)) continue;
      if (!tgt.startsWith("rung:") && hiddenTags.has(tgt)) continue;
      // Skip edges referencing nodes not in our element set
      if (!nodeIds.has(src) || !nodeIds.has(tgt)) continue;

      elements.push({
        group: "edges",
        data: {
          id: "e" + edgeId++,
          source: src,
          target: tgt,
          edgeType: edge.type,
        },
      });
    }

    return elements;
  }

  // ---- Initialize / update Cytoscape ----
  function initCytoscape(data) {
    if (cy) cy.destroy();

    // Prune hiddenTags to only tags that exist in current data
    const currentTags = new Set(Object.keys(data.tagRoles || {}));
    for (const tag of [...hiddenTags]) {
      if (!currentTags.has(tag)) hiddenTags.delete(tag);
    }
    saveWorkspaceState();

    const elements = buildElements(data);

    const nodeCount = elements.filter(e => e.group === "nodes").length;
    const useGrid = nodeCount > 500;
    if (useGrid) {
      gridBanner.textContent = "Graph is large (" + nodeCount + " nodes); layout simplified. Try filtering by role or search.";
      gridBanner.classList.add("visible");
    } else {
      gridBanner.classList.remove("visible");
    }
    const layoutConfig = useGrid
      ? { name: "grid", animate: false, condense: true, avoidOverlapPadding: 10, fit: true, padding: 30 }
      : Object.assign(
          { name: "dagre", rankDir: "LR", nodeSep: 30, edgeSep: 15, rankSep: 80,
            animate: false, fit: true, padding: 30 },
          rungOrderEnabled ? {
            // Sort nodes within the same rank by rung order (ladder is top-down)
            sort: function(a, b) {
              const aOrder = a.data("rungIdx") ?? a.data("rungAffinity") ?? 9999;
              const bOrder = b.data("rungIdx") ?? b.data("rungAffinity") ?? 9999;
              return aOrder - bOrder;
            },
          } : {},
        );

    cy = cytoscape({
      container: cyContainer,
      elements,
      style: [
        // Tag nodes
        {
          selector: 'node[nodeType="tag"]',
          style: {
            label: "data(label)",
            "text-valign": "center",
            "text-halign": "center",
            "background-color": "data(bgColor)",
            "border-color": "data(borderColor)",
            "border-width": "data(borderWidth)",
            color: "data(textColor)",
            "font-size": "11px",
            "text-wrap": "wrap",
            "text-max-width": "90px",
            width: 100,
            height: 36,
            shape: "data(shape)",
            "padding-top": "4px",
            "padding-bottom": "4px",
          },
        },
        // Rung nodes
        {
          selector: 'node[nodeType="rung"]',
          style: {
            label: "data(label)",
            "text-valign": "center",
            "text-halign": "center",
            "background-color": "data(bgColor)",
            "border-color": "data(borderColor)",
            "border-width": "data(borderWidth)",
            color: "data(textColor)",
            "font-size": "9px",
            width: 55,
            height: 22,
            shape: "round-rectangle",
            "background-opacity": 0.6,
          },
        },
        // Condition edges
        {
          selector: 'edge[edgeType="condition"]',
          style: {
            "line-color": "rgba(150,150,150,0.7)",
            "target-arrow-color": "rgba(150,150,150,0.7)",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            width: 1.5,
            "arrow-scale": 0.7,
          },
        },
        // Data edges
        {
          selector: 'edge[edgeType="data"]',
          style: {
            "line-color": "rgba(150,150,150,0.5)",
            "target-arrow-color": "rgba(150,150,150,0.5)",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            "line-style": "dashed",
            width: 1.2,
            "arrow-scale": 0.6,
          },
        },
        // Write edges
        {
          selector: 'edge[edgeType="write"]',
          style: {
            "line-color": "rgba(150,150,150,0.7)",
            "target-arrow-color": "rgba(150,150,150,0.7)",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            width: 1.5,
            "arrow-scale": 0.7,
          },
        },
        // Selected node
        {
          selector: "node:selected",
          style: {
            "border-width": 3,
            "border-color": "var(--vscode-focusBorder, #007fd4)",
          },
        },
        // Dimmed (search/filter non-match)
        {
          selector: ".dimmed",
          style: { opacity: 0.15 },
        },
        // Highlighted (neighbor of selection)
        {
          selector: ".highlighted",
          style: { "border-width": 3, opacity: 1 },
        },
        // Slice upstream
        {
          selector: ".slice-upstream",
          style: {
            "border-color": "#4A90D9",
            "border-width": 4,
            "line-color": "#4A90D9",
            "target-arrow-color": "#4A90D9",
            width: 3,
            opacity: 1,
          },
        },
        // Slice downstream
        {
          selector: ".slice-downstream",
          style: {
            "border-color": "#5CB85C",
            "border-width": 4,
            "line-color": "#5CB85C",
            "target-arrow-color": "#5CB85C",
            width: 3,
            opacity: 1,
          },
        },
        // Slice origin
        {
          selector: ".slice-origin",
          style: {
            "border-color": "#E8E840",
            "border-width": 5,
            opacity: 1,
          },
        },
        // Forced tag
        {
          selector: ".forced",
          style: {
            "border-color": "#D9A441",
            "border-width": 4,
          },
        },
        // Value badge
        {
          selector: "node.has-value",
          style: {
            "text-wrap": "wrap",
          },
        },
      ],
      layout: layoutConfig,
      wheelSensitivity: 0.3,
    });

    // Apply pinned positions after layout
    for (const [nodeId, pos] of Object.entries(pinnedPositions)) {
      const node = cy.getElementById(nodeId);
      if (node.length) node.position(pos);
    }

    setupInteractions();
    applyFilters();
  }

  // ---- Interactions ----
  function setupInteractions() {
    // Click tag node -> select, show info, highlight neighbors
    cy.on("tap", 'node[nodeType="tag"]', (evt) => {
      hideContextMenu();
      const node = evt.target;
      clearSlice();
      selectTagNode(node);
    });

    // Click rung node -> jump to source
    cy.on("tap", 'node[nodeType="rung"]', (evt) => {
      hideContextMenu();
      const node = evt.target;
      const sourceFile = node.data("sourceFile");
      const sourceLine = node.data("sourceLine");
      if (sourceFile) {
        vscodeApi.postMessage({ type: "goToSource", sourceFile, sourceLine });
      }
    });

    // Double-click tag node -> slice both directions
    cy.on("dbltap", 'node[nodeType="tag"]', (evt) => {
      const node = evt.target;
      performSlice(node.id(), "both");
    });

    // Right-click tag node -> context menu
    cy.on("cxttap", 'node[nodeType="tag"]', (evt) => {
      evt.originalEvent.preventDefault();
      const node = evt.target;
      const tagName = node.id();
      showContextMenu(evt.originalEvent, [
        { label: "Show Upstream", action: () => performSlice(tagName, "upstream") },
        { label: "Show Downstream", action: () => performSlice(tagName, "downstream") },
        { label: "Show Both", action: () => performSlice(tagName, "both") },
        { label: "Add to Data View", action: () => vscodeApi.postMessage({ type: "addToDataView", tag: tagName }) },
        { label: "Add to History", action: () => vscodeApi.postMessage({ type: "addToHistory", tag: tagName }) },
        { label: "Copy Name", action: () => navigator.clipboard.writeText(tagName) },
        { label: "Hide Tag", action: () => { hiddenTags.add(tagName); saveWorkspaceState(); rebuildGraph(); } },
      ]);
    });

    // Right-click rung node -> context menu
    cy.on("cxttap", 'node[nodeType="rung"]', (evt) => {
      evt.originalEvent.preventDefault();
      const node = evt.target;
      const sourceFile = node.data("sourceFile");
      const sourceLine = node.data("sourceLine");
      const label = node.data("label");
      showContextMenu(evt.originalEvent, [
        {
          label: "Go to Source",
          action: () => {
            if (sourceFile) vscodeApi.postMessage({ type: "goToSource", sourceFile, sourceLine });
          },
        },
        {
          label: "Copy Rung Info",
          action: () => navigator.clipboard.writeText(label + (sourceFile ? " (" + sourceFile + ":" + sourceLine + ")" : "")),
        },
      ]);
    });

    // Click background -> deselect
    cy.on("tap", (evt) => {
      if (evt.target === cy) {
        clearSelection();
        clearSlice();
        hideContextMenu();
      }
    });

    // Drag node -> pin position
    cy.on("free", "node", (evt) => {
      const node = evt.target;
      const pos = node.position();
      pinnedPositions[node.id()] = { x: pos.x, y: pos.y };
      saveWorkspaceState();
    });

    // Hover -> tooltip
    cy.on("mouseover", "node", (evt) => {
      const node = evt.target;
      showTooltip(evt.originalEvent, node);
    });
    cy.on("mouseout", "node", () => {
      hideTooltip();
    });
  }

  function computeSliceLocal(tagName, direction) {
    if (!graphData) return null;
    const readersOf = graphData.readersOf || {};
    const writersOf = graphData.writersOf || {};
    const rungNodes = graphData.rungNodes || [];
    const allEdges = graphData.graphEdges || [];

    // Build collapse map from block ranges so uncollapsed rung reads/writes
    // map back to the collapsed tag names visible in the graph.
    const collapse = {};
    for (const [label, members] of Object.entries(graphData.blockRanges || {})) {
      for (const m of members) collapse[m] = label;
    }
    const visibleTags = graphData.tagRoles || {};

    function mapTag(t) {
      const mapped = collapse[t] || t;
      return mapped in visibleTags ? mapped : null;
    }

    function bfsUpstream(start) {
      const visitedTags = new Set();
      const visitedRungs = new Set();
      const queue = [start];
      while (queue.length) {
        const cur = queue.pop();
        if (visitedTags.has(cur)) continue;
        visitedTags.add(cur);
        for (const ri of (writersOf[cur] || [])) {
          if (visitedRungs.has(ri)) continue;
          visitedRungs.add(ri);
          const rn = rungNodes[ri];
          if (!rn) continue;
          for (const t of (rn.conditionReads || []).concat(rn.dataReads || [])) {
            const m = mapTag(t);
            if (m && !visitedTags.has(m)) queue.push(m);
          }
        }
      }
      visitedTags.delete(start);
      return visitedTags;
    }

    function bfsDownstream(start) {
      const visitedTags = new Set();
      const visitedRungs = new Set();
      const queue = [start];
      while (queue.length) {
        const cur = queue.pop();
        if (visitedTags.has(cur)) continue;
        visitedTags.add(cur);
        for (const ri of (readersOf[cur] || [])) {
          if (visitedRungs.has(ri)) continue;
          visitedRungs.add(ri);
          const rn = rungNodes[ri];
          if (!rn) continue;
          for (const t of (rn.writes || [])) {
            const m = mapTag(t);
            if (m && !visitedTags.has(m)) queue.push(m);
          }
        }
      }
      visitedTags.delete(start);
      return visitedTags;
    }

    const upTags = (direction === "upstream" || direction === "both") ? bfsUpstream(tagName) : new Set();
    const dnTags = (direction === "downstream" || direction === "both") ? bfsDownstream(tagName) : new Set();
    const allTags = new Set([tagName, ...upTags, ...dnTags]);

    // Filter edges to those within the slice (mirrors on_pyrung_slice logic)
    const sliceEdges = [];
    const sliceRungs = new Set();
    for (const edge of allEdges) {
      const src = edge.source;
      const tgt = edge.target;
      const srcIsRung = src.startsWith("rung:");
      const tgtIsRung = tgt.startsWith("rung:");
      if (!srcIsRung && !tgtIsRung) {
        if (allTags.has(src) && allTags.has(tgt)) sliceEdges.push(edge);
      } else if (!srcIsRung && tgtIsRung) {
        if (allTags.has(src)) {
          const ri = parseInt(tgt.split(":")[1], 10);
          const rn = rungNodes[ri];
          if (rn && (rn.writes || []).some(w => { const m = mapTag(w); return m && allTags.has(m); })) {
            sliceEdges.push(edge);
            sliceRungs.add(tgt);
          }
        }
      } else if (srcIsRung && !tgtIsRung) {
        if (allTags.has(tgt)) {
          const ri = parseInt(src.split(":")[1], 10);
          const rn = rungNodes[ri];
          if (rn && (rn.conditionReads || []).concat(rn.dataReads || []).some(r => { const m = mapTag(r); return m && allTags.has(m); })) {
            sliceEdges.push(edge);
            sliceRungs.add(src);
          }
        }
      }
    }

    const result = {
      tag: tagName,
      direction,
      tags: [...allTags].sort(),
      edges: sliceEdges,
      rungs: [...sliceRungs].sort(),
    };
    if (direction === "both") {
      result.upstream = [...upTags].sort();
      result.downstream = [...dnTags].sort();
    }
    return result;
  }

  function performSlice(tagName, direction) {
    clearSlice();
    sliceHighlight = { tag: tagName, direction, upstream: [], downstream: [] };
    const result = computeSliceLocal(tagName, direction);
    if (result) applySlice(result);
  }

  function selectTagNode(node) {
    selectedNode = node.id();
    cy.elements().removeClass("highlighted");

    // Highlight direct neighbors
    const neighborhood = node.neighborhood();
    neighborhood.addClass("highlighted");
    node.addClass("highlighted");

    showInfoPanel(node);
  }

  function clearSelection() {
    selectedNode = null;
    cy.elements().removeClass("highlighted");
    infoPanel.classList.remove("visible");
  }

  function clearSlice() {
    if (!cy) return;
    sliceHighlight = null;
    cy.elements().removeClass("slice-upstream slice-downstream slice-origin dimmed");
    sliceBanner.classList.remove("visible");
  }

  function updateSliceBanner() {
    if (!sliceHighlight) {
      sliceBanner.classList.remove("visible");
      return;
    }
    const dir = sliceHighlight.direction;
    const tag = sliceHighlight.tag;
    let text;
    if (dir === "upstream") text = "Showing upstream slice from: " + tag;
    else if (dir === "downstream") text = "Showing downstream slice from: " + tag;
    else text = "Showing upstream + downstream slice from: " + tag;
    sliceBannerText.textContent = text;
    sliceBanner.classList.add("visible");
  }

  function applySlice(msg) {
    if (!cy || !sliceHighlight || sliceHighlight.tag !== msg.tag) return;

    const tag = msg.tag;
    const direction = msg.direction;

    // Populate upstream/downstream arrays from response
    if (direction === "both") {
      sliceHighlight.upstream = msg.upstream || [];
      sliceHighlight.downstream = msg.downstream || [];
    } else if (direction === "upstream") {
      sliceHighlight.upstream = msg.tags.filter(t => t !== tag);
    } else {
      sliceHighlight.downstream = msg.tags.filter(t => t !== tag);
    }

    const rungSet = new Set(msg.rungs || []);
    const upstreamSet = new Set(sliceHighlight.upstream);
    const allSliceTags = new Set([tag, ...sliceHighlight.upstream, ...sliceHighlight.downstream]);
    const allSliceIds = new Set([...allSliceTags, ...rungSet]);

    // Apply styling
    cy.elements().addClass("dimmed");
    const originNode = cy.getElementById(tag);
    if (originNode.length) {
      originNode.removeClass("dimmed").addClass("slice-origin");
    }

    for (const t of sliceHighlight.upstream) {
      const n = cy.getElementById(t);
      if (n.length) n.removeClass("dimmed").addClass("slice-upstream");
    }
    for (const t of sliceHighlight.downstream) {
      const n = cy.getElementById(t);
      if (n.length) n.removeClass("dimmed").addClass("slice-downstream");
    }

    // Undim rung nodes explicitly returned by the server
    for (const rId of rungSet) {
      const rungNode = cy.getElementById(rId);
      if (rungNode.length) rungNode.removeClass("dimmed");
    }

    // Color edges that connect slice members
    cy.edges().forEach(edge => {
      const src = edge.source().id();
      const tgt = edge.target().id();

      if (allSliceIds.has(src) && allSliceIds.has(tgt)) {
        edge.removeClass("dimmed");
        const srcIsUpstream = upstreamSet.has(src) || (src.startsWith("rung:") && rungSet.has(src) && hasUpstreamNeighbor(src, upstreamSet));
        const tgtIsUpstream = upstreamSet.has(tgt) || (tgt.startsWith("rung:") && rungSet.has(tgt) && hasUpstreamNeighbor(tgt, upstreamSet));
        if (srcIsUpstream || tgtIsUpstream) {
          edge.addClass("slice-upstream");
        } else {
          edge.addClass("slice-downstream");
        }
      }
    });

    updateSliceBanner();
  }

  function hasUpstreamNeighbor(rungId, upstreamSet) {
    const node = cy.getElementById(rungId);
    if (!node.length) return false;
    const neighbors = node.neighborhood("node");
    for (let i = 0; i < neighbors.length; i++) {
      if (upstreamSet.has(neighbors[i].id())) return true;
    }
    return false;
  }

  // ---- Filtering ----
  function applyFilters() {
    if (!cy) return;
    cy.batch(() => {
      cy.nodes().forEach(node => {
        if (node.data("nodeType") === "tag") {
          const role = node.data("role");
          const name = node.id();
          const roleVisible = activeRoles.has(role);
          const searchVisible = matchesSearch(name, searchNeedle);
          const inSlice = node.hasClass("slice-upstream") || node.hasClass("slice-downstream") || node.hasClass("slice-origin");
          if (!inSlice && (!roleVisible || !searchVisible)) {
            node.addClass("dimmed");
          } else {
            node.removeClass("dimmed");
          }
        }
      });
    });
  }

  // ---- Info panel ----
  function showInfoPanel(node) {
    const tagName = node.id();
    const role = node.data("role");
    const value = tagValues[tagName];
    const isForced = forces && Object.prototype.hasOwnProperty.call(forces, tagName);

    // Find connected rungs using readersOf/writersOf indices
    const readers = [];
    const writers = [];
    if (graphData) {
      const rungNodes = graphData.rungNodes || [];
      const readerIndices = (graphData.readersOf && graphData.readersOf[tagName]) || [];
      const writerIndices = (graphData.writersOf && graphData.writersOf[tagName]) || [];
      for (const idx of readerIndices) {
        const rn = rungNodes[idx];
        if (rn) readers.push("R" + (rn.rungIndex + 1));
      }
      for (const idx of writerIndices) {
        const rn = rungNodes[idx];
        if (rn) writers.push("R" + (rn.rungIndex + 1));
      }
    }

    // Upstream/downstream counts from the graph
    const upstreamCount = graphData && graphData.writersOf && graphData.writersOf[tagName]
      ? graphData.writersOf[tagName].length : 0;
    const downstreamCount = graphData && graphData.readersOf && graphData.readersOf[tagName]
      ? graphData.readersOf[tagName].length : 0;

    let html = '<div class="info-title">' + esc(tagName) + '</div>';
    html += '<div class="slice-btns">';
    html += '<button class="slice-btn" data-slice-dir="upstream" data-slice-tag="' + esc(tagName) + '">\u2190 Upstream</button>';
    html += '<button class="slice-btn" data-slice-dir="downstream" data-slice-tag="' + esc(tagName) + '">Downstream \u2192</button>';
    html += '</div>';
    html += '<div class="info-row"><b>Role:</b> ' + esc(role) + '</div>';
    html += '<div id="info-value" class="info-row" style="' + (value !== undefined ? '' : 'display:none') + '"><b>Value:</b> <span id="info-value-text">' + (value !== undefined ? esc(String(value)) : '') + '</span></div>';
    html += '<div id="info-forced" class="info-row" style="color:#D9A441;' + (isForced ? '' : 'display:none') + '"><b>Forced:</b> <span id="info-forced-text">' + (isForced ? esc(String(forces[tagName])) : '') + '</span></div>';
    html += '<div class="info-row"><b>Writers:</b> ' + upstreamCount + ' rung(s)</div>';
    html += '<div class="info-row"><b>Readers:</b> ' + downstreamCount + ' rung(s)</div>';

    if (readers.length) {
      html += '<div class="info-section"><div class="info-section-title">Read by:</div><ul class="info-list">';
      for (const r of readers) html += '<li>' + esc(r) + '</li>';
      html += '</ul></div>';
    }
    if (writers.length) {
      html += '<div class="info-section"><div class="info-section-title">Written by:</div><ul class="info-list">';
      for (const w of writers) html += '<li>' + esc(w) + '</li>';
      html += '</ul></div>';
    }

    infoPanel.innerHTML = html;
    infoPanel.classList.add("visible");

    // Wire up slice buttons
    infoPanel.querySelectorAll(".slice-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        performSlice(btn.getAttribute("data-slice-tag"), btn.getAttribute("data-slice-dir"));
      });
    });
  }

  // ---- Context menu ----
  function showContextMenu(event, items) {
    hideContextMenu();
    let html = "";
    for (let i = 0; i < items.length; i++) {
      html += '<div class="ctx-item" data-index="' + i + '">' + esc(items[i].label) + '</div>';
    }
    ctxMenu.innerHTML = html;
    ctxMenu.style.left = event.clientX + "px";
    ctxMenu.style.top = event.clientY + "px";
    ctxMenu.classList.add("visible");

    const handler = (e) => {
      const item = e.target.closest(".ctx-item");
      if (item) {
        const idx = parseInt(item.getAttribute("data-index"), 10);
        if (items[idx]) items[idx].action();
      }
      hideContextMenu();
      document.removeEventListener("click", handler, true);
    };
    // Slight delay so the current click doesn't immediately dismiss
    setTimeout(() => document.addEventListener("click", handler, true), 0);
  }

  function hideContextMenu() {
    ctxMenu.classList.remove("visible");
    ctxMenu.innerHTML = "";
  }

  // ---- Tooltip ----
  function showTooltip(event, node) {
    let html = "";
    if (node.data("nodeType") === "tag") {
      const tagName = node.id();
      const role = node.data("role");
      const val = tagValues[tagName];
      html = "<b>" + esc(tagName) + "</b><br>Role: " + esc(role);
      if (val !== undefined) html += "<br>Value: " + esc(String(val));
    } else {
      const label = node.data("label");
      const idx = node.data("rungIdx");
      const rn = graphData && graphData.rungNodes ? graphData.rungNodes[idx] : null;
      const reads = rn ? (rn.conditionReads || []).concat(rn.dataReads || []) : [];
      const writes = rn ? (rn.writes || []) : [];
      html = "<b>" + esc(label) + "</b>";
      if (reads.length) html += "<br>Reads: " + reads.map(esc).join(", ");
      if (writes.length) html += "<br>Writes: " + writes.map(esc).join(", ");
    }
    tooltip.innerHTML = html;
    tooltip.style.left = (event.clientX + 12) + "px";
    tooltip.style.top = (event.clientY + 12) + "px";
    tooltip.classList.add("visible");
  }

  function hideTooltip() {
    tooltip.classList.remove("visible");
  }

  // ---- Live value overlay ----
  function updateValues(newTagValues, newForces) {
    tagValues = newTagValues || {};
    forces = newForces || {};
    if (!cy) return;

    cy.batch(() => {
      cy.nodes('[nodeType="tag"]').forEach(node => {
        const tagName = node.id();
        const val = tagValues[tagName];
        const isForced = forces && Object.prototype.hasOwnProperty.call(forces, tagName);

        // Update label with value badge
        if (val !== undefined) {
          let badge;
          if (val === true || val === "True") badge = "\u25cf"; // filled dot indicator
          else if (val === false || val === "False") badge = "\u25cb"; // hollow dot
          else badge = String(val);
          node.data("label", tagName + "\\n" + badge);
          node.addClass("has-value");
        } else {
          node.data("label", tagName);
          node.removeClass("has-value");
        }

        // Force styling
        if (isForced) {
          node.addClass("forced");
        } else {
          node.removeClass("forced");
        }
      });
    });

    // Patch value/force text in the info panel without rebuilding it,
    // so slice buttons stay clickable during continue.
    if (selectedNode) {
      const val = tagValues[selectedNode];
      const valEl = document.getElementById("info-value");
      const valText = document.getElementById("info-value-text");
      if (valEl && valText) {
        if (val !== undefined) {
          valText.textContent = String(val);
          valEl.style.display = "";
        } else {
          valEl.style.display = "none";
        }
      }
      const isForced = forces && Object.prototype.hasOwnProperty.call(forces, selectedNode);
      const frcEl = document.getElementById("info-forced");
      const frcText = document.getElementById("info-forced-text");
      if (frcEl && frcText) {
        if (isForced) {
          frcText.textContent = String(forces[selectedNode]);
          frcEl.style.display = "";
        } else {
          frcEl.style.display = "none";
        }
      }
    }
  }

  // ---- Rebuild graph ----
  function rebuildGraph() {
    if (graphData) {
      initCytoscape(graphData);
      if (tagValues) updateValues(tagValues, forces);
    }
  }

  // ---- Toolbar handlers ----
  searchInput.addEventListener("input", () => {
    searchNeedle = searchInput.value.trim();
    applyFilters();
  });

  roleButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      const role = btn.getAttribute("data-role");
      if (activeRoles.has(role)) {
        activeRoles.delete(role);
        btn.classList.remove("active");
      } else {
        activeRoles.add(role);
        btn.classList.add("active");
      }
      applyFilters();
    });
  });

  rungOrderBtn.addEventListener("click", () => {
    rungOrderEnabled = !rungOrderEnabled;
    rungOrderBtn.classList.toggle("active", rungOrderEnabled);
    if (cy) {
      const nodeCount = cy.nodes().length;
      if (nodeCount <= 500) {
        const layoutOpts = Object.assign(
          { name: "dagre", rankDir: "LR", nodeSep: 30, edgeSep: 15, rankSep: 80,
            animate: false, fit: true, padding: 30 },
          rungOrderEnabled ? {
            sort: function(a, b) {
              const aOrder = a.data("rungIdx") ?? a.data("rungAffinity") ?? 9999;
              const bOrder = b.data("rungIdx") ?? b.data("rungAffinity") ?? 9999;
              return aOrder - bOrder;
            },
          } : {},
        );
        cy.layout(layoutOpts).run();
      }
    } else {
      rebuildGraph();
    }
  });

  sliceBannerClose.addEventListener("click", () => {
    clearSlice();
    applyFilters();
  });

  resetBtn.addEventListener("click", () => {
    pinnedPositions = {};
    hiddenTags.clear();
    saveWorkspaceState();
    searchInput.value = "";
    searchNeedle = "";
    activeRoles.clear();
    ["input", "pivot", "terminal", "isolated"].forEach(r => activeRoles.add(r));
    roleButtons.forEach(b => b.classList.add("active"));
    clearSlice();
    rebuildGraph();
  });

  // ---- Escape to close context menu ----
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      hideContextMenu();
      clearSlice();
    }
  });

  // ---- Utility ----
  function esc(value) {
    const div = document.createElement("div");
    div.textContent = String(value);
    return div.innerHTML;
  }

  // ---- Message handling ----
  window.addEventListener("message", (event) => {
    const msg = event.data;

    if (msg.type === "graph") {
      graphData = msg.data;
      initCytoscape(graphData);
      return;
    }

    if (msg.type === "trace") {
      updateValues(msg.tagValues, msg.forces);
      return;
    }

    // sliceResult no longer arrives from the extension host — slices are
    // computed locally in performSlice() using cached graphData.
  });

  // Signal ready
  vscodeApi.postMessage({ type: "ready" });
})();
</script>
</body>
</html>`;
  }
}

module.exports = { PyrungGraphPanelProvider };
