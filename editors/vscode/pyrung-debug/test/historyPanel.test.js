const test = require("node:test");
const assert = require("node:assert/strict");
const { setTimeout: delay } = require("node:timers/promises");

const { PyrungHistoryPanelProvider } = require("../historyPanel");

function createFakeWebviewView() {
  let receiveHandler = null;
  let disposeHandler = null;
  const postedMessages = [];

  return {
    postedMessages,
    webview: {
      options: null,
      html: "",
      onDidReceiveMessage(handler) {
        receiveHandler = handler;
        return { dispose() {} };
      },
      postMessage(message) {
        postedMessages.push(message);
        return Promise.resolve(true);
      },
    },
    onDidDispose(handler) {
      disposeHandler = handler;
      return { dispose() {} };
    },
    async emit(message) {
      if (receiveHandler) {
        await receiveHandler(message);
      }
    },
    dispose() {
      if (disposeHandler) {
        disposeHandler();
      }
    },
  };
}

function createFakeSession({ id = "session-1", onCustomRequest } = {}) {
  const calls = [];
  return {
    id,
    calls,
    async customRequest(command, args) {
      calls.push({ command, args });
      if (onCustomRequest) {
        return onCustomRequest(command, args);
      }
      return { entries: [] };
    },
  };
}

function latestState(view) {
  const states = view.postedMessages.filter((message) => message.type === "state");
  assert.ok(states.length > 0, "expected at least one state message");
  return states.at(-1);
}

test("adding a watched tag while running updates UI state but defers history fetches", async () => {
  let executionState = "running";
  const session = createFakeSession({
    onCustomRequest(command) {
      assert.equal(command, "pyrungTagChanges");
      return {
        entries: [
          {
            scanId: 8,
            prevScanId: 7,
            timestamp: 1.25,
            changes: { Mode: ["0", "1"] },
          },
        ],
      };
    },
  });
  const provider = new PyrungHistoryPanelProvider({
    getExecutionState: () => executionState,
  });
  const view = createFakeWebviewView();

  provider.resolveWebviewView(view);
  provider.setSession(session);
  await view.emit({ type: "ready" });
  view.postedMessages.length = 0;

  provider.addTag("Mode");

  assert.equal(session.calls.length, 0);
  assert.deepEqual(latestState(view).watchedTags, ["Mode"]);

  executionState = "stopped";
  await provider.refresh();

  assert.equal(session.calls.length, 1);
  assert.equal(session.calls[0].command, "pyrungTagChanges");
  assert.deepEqual(session.calls[0].args.tags, ["Mode"]);
  assert.deepEqual(latestState(view).entries, [
    {
      scanId: 8,
      prevScanId: 7,
      timestamp: 1.25,
      changes: { Mode: ["0", "1"] },
    },
  ]);
});

test("adding a watched tag while stopped fetches retained history immediately", async () => {
  const session = createFakeSession({
    onCustomRequest(command) {
      assert.equal(command, "pyrungTagChanges");
      return {
        entries: [
          {
            scanId: 5,
            prevScanId: 4,
            timestamp: 0.5,
            changes: { Running: ["False", "True"] },
          },
        ],
      };
    },
  });
  const provider = new PyrungHistoryPanelProvider({
    getExecutionState: () => "stopped",
  });
  const view = createFakeWebviewView();

  provider.resolveWebviewView(view);
  provider.setSession(session);
  await view.emit({ type: "ready" });
  view.postedMessages.length = 0;

  provider.addTag("Running");
  await delay(0);

  assert.equal(session.calls.length, 1);
  assert.equal(session.calls[0].command, "pyrungTagChanges");
  assert.deepEqual(session.calls[0].args.tags, ["Running"]);
  assert.deepEqual(latestState(view).entries, [
    {
      scanId: 5,
      prevScanId: 4,
      timestamp: 0.5,
      changes: { Running: ["False", "True"] },
    },
  ]);
});

test("hint updates only publish metadata for watched history tags", async () => {
  const provider = new PyrungHistoryPanelProvider({
    getExecutionState: () => "stopped",
  });
  const view = createFakeWebviewView();

  provider.resolveWebviewView(view);
  await view.emit({ type: "ready" });
  provider.addTag("Mode");
  provider.addTag("Running");
  view.postedMessages.length = 0;

  provider.updateHints({
    Mode: { choices: { 0: "Idle", 1: "Run" } },
    Other: { readonly: true },
  });
  await delay(0);

  assert.deepEqual(latestState(view).tagHints, {
    Mode: { choices: { 0: "Idle", 1: "Run" } },
  });
});

test("live running updates append only watched-tag changes and post immediately", async () => {
  const provider = new PyrungHistoryPanelProvider({
    getExecutionState: () => "running",
  });
  const view = createFakeWebviewView();

  provider.resolveWebviewView(view);
  await view.emit({ type: "ready" });
  provider.addTag("Mode");
  view.postedMessages.length = 0;

  provider.appendLiveChanges(
    [
      { tag: "Ignored", previous: "10", current: "11" },
      { tag: "Mode", previous: "0", current: "1" },
    ],
    12
  );

  assert.deepEqual(latestState(view).entries, [
    {
      scanId: 12,
      prevScanId: 11,
      timestamp: null,
      changes: {
        Mode: ["0", "1"],
      },
    },
  ]);
});

function windowEntry() {
  return {
    window: true,
    scanId: 2400,
    prevScanId: 0,
    timestamp: 24,
    changes: { Lamp: ["False", "True"] },
  };
}

async function stoppedPanelWith(onCustomRequest) {
  const session = createFakeSession({ onCustomRequest });
  const provider = new PyrungHistoryPanelProvider({
    getExecutionState: () => "stopped",
  });
  const view = createFakeWebviewView();
  provider.resolveWebviewView(view);
  provider.setSession(session);
  await view.emit({ type: "ready" });
  provider.addTag("Lamp");
  await delay(0);
  return { session, provider, view };
}

test("a window offers more, and loading it reads its own scans in its place", async () => {
  const pages = [
    {
      entries: [
        { scanId: 3005, prevScanId: 3004, timestamp: 30.05, changes: { Lamp: ["False", "True"] } },
        windowEntry(),
      ],
    },
    {
      entries: [
        { scanId: 100, prevScanId: 99, timestamp: 1, changes: { Lamp: ["True", "False"] } },
      ],
    },
  ];
  const { session, view } = await stoppedPanelWith(() => pages.shift());

  assert.equal(latestState(view).hasMore, true);
  assert.deepEqual(
    latestState(view).entries.map((entry) => Boolean(entry.window)),
    [false, true]
  );

  await view.emit({ type: "loadOlder" });

  // beforeScan is exclusive, and the window's last scan is one of those not read.
  assert.equal(session.calls.at(-1).args.beforeScan, 2401);
  assert.deepEqual(
    latestState(view).entries.map((entry) => entry.scanId),
    [3005, 100]
  );
  assert.equal(latestState(view).hasMore, false);
});

test("loading after an entry still asks for what is older than it", async () => {
  const entry = { scanId: 50, prevScanId: 49, timestamp: 0.5, changes: { Lamp: ["0", "1"] } };
  const { session, provider, view } = await stoppedPanelWith(() => ({ entries: [entry] }));
  provider._hasMore = true;

  await view.emit({ type: "loadOlder" });

  assert.equal(session.calls.at(-1).args.beforeScan, 50);
});

test("a window with no net change for the watched tags stays, as scans not read", async () => {
  const { provider, view } = await stoppedPanelWith(() => ({ entries: [windowEntry()] }));
  provider._watchedTags.delete("Lamp");
  provider._watchedTags.add("Other");

  provider._syncEntriesToWatchedTags();
  provider._postState();

  const [entry] = latestState(view).entries;
  assert.equal(entry.window, true);
  assert.deepEqual(entry.changes, {});
});

test("a window renders as a range, explains nothing and forks at its two ends", async () => {
  const { provider } = await stoppedPanelWith(() => ({ entries: [] }));
  const source = provider._html();
  const begin = source.indexOf("function renderWindow(entry)");
  const end = source.indexOf("function renderEntries()");
  assert.ok(begin > 0 && end > begin);
  const render = new Function(
    "esc",
    "formatChange",
    "state",
    source.slice(begin, end) + "\nreturn renderWindow;"
  )(String, (_tag, values) => values.join(" -> "), { activeScanId: null });

  const html = render(windowEntry());

  assert.match(html, /#1\.\.#2400/);
  assert.match(html, /Lamp/);
  assert.match(html, /window-note/);
  assert.doesNotMatch(html, /data-explain/);
  assert.deepEqual(
    [...html.matchAll(/data-fork-scan="(\d+)"/g)].map((match) => match[1]),
    ["0", "2400"]
  );
});
