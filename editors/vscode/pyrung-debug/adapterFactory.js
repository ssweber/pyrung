const path = require("path");
const vscode = require("vscode");

class PyrungAdapterFactory {
  async createDebugAdapterDescriptor(session) {
    const config = session.configuration;
    const python = config.pythonPath || (await _resolvePython(config.program)) || "python";
    const options = {};
    if (config.program) {
      options.cwd = path.dirname(config.program);
    }
    return new vscode.DebugAdapterExecutable(python, ["-m", "pyrung.dap"], options);
  }
}

async function _resolvePython(programPath) {
  const pythonExt = vscode.extensions.getExtension("ms-python.python");
  if (!pythonExt) return null;

  if (!pythonExt.isActive) {
    try {
      await pythonExt.activate();
    } catch {
      return null;
    }
  }

  const api = pythonExt.exports;
  if (!api?.environments?.getActiveEnvironmentPath) return null;

  const resource = programPath ? vscode.Uri.file(programPath) : undefined;
  const envPath = api.environments.getActiveEnvironmentPath(resource);
  if (!envPath?.path) return null;

  if (api.environments.resolveEnvironment) {
    try {
      const env = await api.environments.resolveEnvironment(envPath);
      const uri = env?.executable?.uri;
      if (uri) return uri.fsPath;
    } catch {
      /* fall through */
    }
  }

  return envPath.path;
}

class PyrungConfigProvider {
  async resolveDebugConfigurationWithSubstitutedVariables(_folder, config) {
    if (config.type !== "pyrung") return config;

    const python =
      config.pythonPath || (await _resolvePython(config.program)) || "python";
    const { execFileSync } = require("child_process");
    try {
      execFileSync(python, ["-c", "import pyrung.dap"], {
        timeout: 10000,
        stdio: "ignore",
        windowsHide: true,
      });
    } catch {
      const choice = await vscode.window.showErrorMessage(
        `pyrung is not installed in the selected Python environment (${python}). ` +
          "Select a Python interpreter that has pyrung installed, or set pythonPath in launch.json.",
        "Select Interpreter",
        "Cancel"
      );
      if (choice === "Select Interpreter") {
        await vscode.commands.executeCommand("python.setInterpreter");
      }
      return undefined;
    }
    return config;
  }
}

module.exports = {
  PyrungAdapterFactory,
  PyrungConfigProvider,
};
