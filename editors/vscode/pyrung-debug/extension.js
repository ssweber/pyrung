const vscode = require("vscode");

class PyrungAdapterFactory {
  createDebugAdapterDescriptor(session) {
    const config = session.configuration;
    const python = config.pythonPath || "python";
    return new vscode.DebugAdapterExecutable(python, ["-m", "pyrung.dap"]);
  }
}

exports.activate = function (context) {
  context.subscriptions.push(
    vscode.debug.registerDebugAdapterDescriptorFactory(
      "pyrung",
      new PyrungAdapterFactory()
    )
  );
};

exports.deactivate = function () {};
