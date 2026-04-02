const path = require("path");
const vscode = require("vscode");

class PyrungAdapterFactory {
  createDebugAdapterDescriptor(session) {
    const config = session.configuration;
    const python = config.pythonPath || "python";
    const options = {};
    if (config.program) {
      options.cwd = path.dirname(config.program);
    }
    return new vscode.DebugAdapterExecutable(python, ["-m", "pyrung.dap"], options);
  }
}

module.exports = {
  PyrungAdapterFactory,
};
