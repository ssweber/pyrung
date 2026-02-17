const vscode = require("vscode");

class PyrungAdapterFactory {
  createDebugAdapterDescriptor(session) {
    const config = session.configuration;
    const python = config.pythonPath || "python";
    return new vscode.DebugAdapterExecutable(python, ["-m", "pyrung.dap"]);
  }
}

module.exports = {
  PyrungAdapterFactory,
};
