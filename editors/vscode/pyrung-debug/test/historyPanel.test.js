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
