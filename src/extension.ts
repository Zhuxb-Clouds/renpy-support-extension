import * as vscode from "vscode";
import * as path from "path";
import * as fs from "fs-extra";
import {
  LanguageClient,
  LanguageClientOptions,
  ServerOptions,
  TransportKind,
} from "vscode-languageclient/node";

let client: LanguageClient | undefined;
let extensionContext: vscode.ExtensionContext | undefined;

// ─── Helpers ───────────────────────────────────────────────────────────

/** Return the first existing executable from a list of candidate paths. */
function findExecutable(candidates: string[]): string | undefined {
  for (const p of candidates) {
    try {
      fs.accessSync(p, fs.constants.X_OK);
      return p;
    } catch {
      // not found / not executable – try next
    }
  }
  return undefined;
}

/** Try to obtain the Python path from the official VS Code Python extension. */
async function getPythonFromVSCodeExtension(): Promise<string | undefined> {
  try {
    const pythonExt = vscode.extensions.getExtension("ms-python.python");
    if (!pythonExt) {
      return undefined;
    }
    if (!pythonExt.isActive) {
      await pythonExt.activate();
    }
    // The Python extension exposes an API with `environments.getActiveEnvironmentPath`
    const api = pythonExt.exports;
    const envPath = api?.environments?.getActiveEnvironmentPath?.();
    if (envPath?.path && fs.existsSync(envPath.path)) {
      console.log(`Ren'Py LSP: detected Python from VS Code Python extension: "${envPath.path}"`);
      return envPath.path;
    }
  } catch (err) {
    console.warn("Ren'Py LSP: failed to query VS Code Python extension:", err);
  }
  return undefined;
}

/** Resolve the Python interpreter path using configuration + auto-detect. */
async function resolvePythonPath(extensionPath: string): Promise<string | undefined> {
  const cfg = vscode.workspace.getConfiguration("renpy-lsp");
  const explicit: string = cfg.get<string>("pythonPath", "").trim();

  // 1. User-configured path
  if (explicit) {
    if (fs.existsSync(explicit)) {
      return explicit;
    }
    vscode.window.showWarningMessage(
      `Ren'Py LSP: configured pythonPath "${explicit}" does not exist. Falling back to auto-detect.`,
    );
  }

  // 2. VS Code Python extension's active interpreter
  const fromVSCode = await getPythonFromVSCodeExtension();
  if (fromVSCode) {
    return fromVSCode;
  }

  // 3. Extension-local venv → workspace venvs
  const workspaceFolders = vscode.workspace.workspaceFolders ?? [];
  const candidates: string[] = [
    path.join(extensionPath, ".venv", "bin", "python3"),
    path.join(extensionPath, ".venv", "bin", "python"),
  ];
  for (const ws of workspaceFolders) {
    candidates.push(
      path.join(ws.uri.fsPath, ".venv", "bin", "python3"),
      path.join(ws.uri.fsPath, ".venv", "bin", "python"),
    );
  }

  const detected = findExecutable(candidates);
  if (detected) {
    return detected;
  }

  // 4. Last resort: bare "python3" (resolved by PATH at spawn time)
  return "python3";
}

// ─── Activation ────────────────────────────────────────────────────────

export function activate(context: vscode.ExtensionContext) {
  console.log("Ren'Py LSP extension is now active!");
  extensionContext = context;

  // Commands
  context.subscriptions.push(
    vscode.commands.registerCommand("renpy-lsp.startServer", () => {
      startLanguageServer(context);
    }),
    vscode.commands.registerCommand("renpy-lsp.stopServer", () => {
      stopLanguageServer();
    }),
    vscode.commands.registerCommand("renpy-lsp.restartServer", async () => {
      await stopLanguageServer();
      startLanguageServer(context);
    }),
    vscode.commands.registerCommand("renpy-lsp.formatAllFiles", () => {
      formatAllRpyFiles();
    }),
  );

  // Auto-start when .rpy files are present
  checkForRpyFiles().then((has) => {
    if (has) {
      console.log("Detected .rpy files in workspace, starting language server...");
      startLanguageServer(context);
    }
  });

  // Watch for new .rpy files
  const fileWatcher = vscode.workspace.createFileSystemWatcher("**/*.rpy");
  fileWatcher.onDidCreate(() => {
    if (!client) {
      console.log("New .rpy file created, starting language server...");
      startLanguageServer(context);
    }
  });
  context.subscriptions.push(fileWatcher);

  // Restart server on relevant config change
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("renpy-lsp")) {
        vscode.window.showInformationMessage("Ren'Py LSP config changed — restarting server…");
        stopLanguageServer().then(() => startLanguageServer(context));
      }
    }),
  );
}

export function deactivate(): Thenable<void> | undefined {
  return stopLanguageServer();
}

// ─── .rpy detection ────────────────────────────────────────────────────

async function checkForRpyFiles(): Promise<boolean> {
  if (!vscode.workspace.workspaceFolders) {
    return false;
  }
  try {
    const files = await vscode.workspace.findFiles("**/*.rpy", "**/node_modules/**", 1);
    return files.length > 0;
  } catch (error) {
    console.error("Error checking for .rpy files:", error);
    return false;
  }
}

// ─── Format all .rpy files ─────────────────────────────────────────────

async function formatAllRpyFiles() {
  if (!client) {
    vscode.window.showWarningMessage("Ren'Py LSP: Language server is not running.");
    return;
  }

  const files = await vscode.workspace.findFiles("**/*.{rpy,rpym}", "**/node_modules/**");
  if (files.length === 0) {
    vscode.window.showInformationMessage("No .rpy/.rpym files found in workspace.");
    return;
  }

  const cfg = vscode.workspace.getConfiguration("renpy-lsp");
  if (!cfg.get<boolean>("formatting.enabled", true)) {
    vscode.window.showWarningMessage(
      "Ren'Py LSP: Formatting is disabled. Enable renpy-lsp.formatting.enabled first.",
    );
    return;
  }

  let formatted = 0;
  let failed = 0;

  await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: "Formatting Ren'Py files",
      cancellable: true,
    },
    async (progress, token) => {
      for (let i = 0; i < files.length; i++) {
        if (token.isCancellationRequested) {
          break;
        }
        const uri = files[i];
        const basename = path.basename(uri.fsPath);
        progress.report({
          message: `${basename} (${i + 1}/${files.length})`,
          increment: 100 / files.length,
        });

        try {
          const doc = await vscode.workspace.openTextDocument(uri);
          const edits = await vscode.commands.executeCommand<vscode.TextEdit[]>(
            "vscode.executeFormatDocumentProvider",
            doc.uri,
            { tabSize: cfg.get<number>("formatting.indentSize", 4), insertSpaces: true },
          );
          if (edits && edits.length > 0) {
            const wsEdit = new vscode.WorkspaceEdit();
            for (const edit of edits) {
              wsEdit.replace(doc.uri, edit.range, edit.newText);
            }
            await vscode.workspace.applyEdit(wsEdit);
            await doc.save();
            formatted++;
          }
        } catch (err) {
          console.error(`Failed to format ${uri.fsPath}:`, err);
          failed++;
        }
      }
    },
  );

  const parts: string[] = [`Formatted ${formatted} file(s)`];
  if (failed > 0) {
    parts.push(`${failed} failed`);
  }
  vscode.window.showInformationMessage(`Ren'Py LSP: ${parts.join(", ")}.`);
}

// ─── Server lifecycle ──────────────────────────────────────────────────

async function startLanguageServer(context: vscode.ExtensionContext) {
  if (client) {
    console.log("Language server is already running");
    return;
  }

  const serverScript = context.asAbsolutePath(path.join("bundled", "tools", "lsp_server.py"));
  if (!fs.existsSync(serverScript)) {
    vscode.window.showWarningMessage(
      "Ren'Py language server not found in bundled/tools directory. Please ensure the server is properly installed.",
    );
    return;
  }

  const pythonPath = await resolvePythonPath(context.extensionPath);
  if (!pythonPath) {
    const choice = await vscode.window.showErrorMessage(
      "Ren'Py LSP: could not locate a Python 3 interpreter. " +
        "Please install Python 3.11+ or set the path manually.",
      "Open Settings",
      "Install Python Extension",
    );
    if (choice === "Open Settings") {
      vscode.commands.executeCommand("workbench.action.openSettings", "renpy-lsp.pythonPath");
    } else if (choice === "Install Python Extension") {
      vscode.commands.executeCommand("workbench.extensions.installExtension", "ms-python.python");
    }
    return;
  }
  console.log(`Ren'Py LSP: using Python interpreter "${pythonPath}"`);

  const serverOptions: ServerOptions = {
    run: {
      command: pythonPath,
      args: [serverScript],
      options: { cwd: context.extensionPath },
      transport: TransportKind.stdio,
    },
    debug: {
      command: pythonPath,
      args: [serverScript],
      options: { cwd: context.extensionPath },
      transport: TransportKind.stdio,
    },
  };

  const clientOptions: LanguageClientOptions = {
    documentSelector: [
      { scheme: "file", language: "renpy" },
      { scheme: "file", pattern: "**/*.rpy" },
      { scheme: "file", pattern: "**/*.rpym" },
    ],
    synchronize: {
      fileEvents: vscode.workspace.createFileSystemWatcher("**/*.{rpy,rpym}"),
    },
  };

  client = new LanguageClient("renpy-lsp", "Ren'Py Language Server", serverOptions, clientOptions);

  client.start().then(
    () => {
      console.log("Ren'Py language server started successfully");
    },
    (error: Error) => {
      console.error("Failed to start language server:", error);
      vscode.window.showErrorMessage(`Failed to start Ren'Py Language Server: ${error.message}`);
      client = undefined;
    },
  );
}

async function stopLanguageServer(): Promise<void> {
  if (!client) {
    return;
  }
  try {
    await client.stop();
    client = undefined;
    console.log("Ren'Py language server stopped");
  } catch (error) {
    console.error("Error stopping language server:", error);
  }
}
