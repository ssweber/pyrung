const vscode = require("vscode");

const { PyrungAdapterFactory } = require("./adapterFactory");
const { PyrungDecorationController } = require("./decorationController");
const { PyrungInlineValuesProvider } = require("./inlineValuesProvider");

exports.activate = function (context) {
  const decorator = new PyrungDecorationController();
  const inlineValuesProvider = new PyrungInlineValuesProvider({
    getConditionLinesForDocument: (document) => decorator.conditionLinesForDocument(document),
  });

  context.subscriptions.push(decorator);
  context.subscriptions.push(
    vscode.languages.registerInlineValuesProvider(
      { language: "python", scheme: "file" },
      inlineValuesProvider
    )
  );

  context.subscriptions.push(
    vscode.debug.registerDebugAdapterDescriptorFactory(
      "pyrung",
      new PyrungAdapterFactory()
    )
  );

  context.subscriptions.push(
    vscode.debug.registerDebugAdapterTrackerFactory("pyrung", {
      createDebugAdapterTracker() {
        return {
          onDidSendMessage: (message) => decorator.handleAdapterMessage(message),
        };
      },
    })
  );

  context.subscriptions.push(
    vscode.window.onDidChangeVisibleTextEditors(() => decorator.renderVisibleEditors())
  );
};

exports.deactivate = function () {};
