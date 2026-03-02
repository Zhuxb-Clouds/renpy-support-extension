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

// ─── Output Channel Logger ─────────────────────────────────────────────
const outputChannel = vscode.window.createOutputChannel("Ren'Py LSP");

function log(msg: string) {
  const ts = new Date().toISOString();
  outputChannel.appendLine(`[${ts}] ${msg}`);
  console.log(`Ren'Py LSP: ${msg}`);
}

function logWarn(msg: string) {
  const ts = new Date().toISOString();
  outputChannel.appendLine(`[${ts}] [WARN] ${msg}`);
  console.warn(`Ren'Py LSP: ${msg}`);
}

function logError(msg: string, err?: unknown) {
  const ts = new Date().toISOString();
  const errStr = err instanceof Error ? `${err.message}\n${err.stack}` : String(err ?? "");
  outputChannel.appendLine(`[${ts}] [ERROR] ${msg} ${errStr}`);
  console.error(`Ren'Py LSP: ${msg}`, err);
}

// ─── Helpers ───────────────────────────────────────────────────────────

/** Return the first existing executable from a list of candidate paths. */
function findExecutable(candidates: string[]): string | undefined {
  log(`findExecutable: searching ${candidates.length} candidate(s)…`);
  for (const p of candidates) {
    try {
      fs.accessSync(p, fs.constants.X_OK);
      log(`findExecutable: found executable "${p}"`);
      return p;
    } catch {
      log(`findExecutable: candidate not executable: "${p}"`);
    }
  }
  logWarn("findExecutable: no executable found among candidates");
  return undefined;
}

/** Try to obtain the Python path from the official VS Code Python extension. */
async function getPythonFromVSCodeExtension(): Promise<string | undefined> {
  log("getPythonFromVSCodeExtension: querying ms-python.python extension…");
  try {
    const pythonExt = vscode.extensions.getExtension("ms-python.python");
    if (!pythonExt) {
      log("getPythonFromVSCodeExtension: ms-python.python extension not installed");
      return undefined;
    }
    if (!pythonExt.isActive) {
      log("getPythonFromVSCodeExtension: activating ms-python.python…");
      await pythonExt.activate();
    }
    const api = pythonExt.exports;
    const envPath = api?.environments?.getActiveEnvironmentPath?.();
    if (envPath?.path && fs.existsSync(envPath.path)) {
      log(`getPythonFromVSCodeExtension: detected Python: "${envPath.path}"`);
      return envPath.path;
    }
    log("getPythonFromVSCodeExtension: extension active but no valid environment path");
  } catch (err) {
    logError("getPythonFromVSCodeExtension: failed to query", err);
  }
  return undefined;
}

/** Resolve the Python interpreter path using configuration + auto-detect. */
async function resolvePythonPath(extensionPath: string): Promise<string | undefined> {
  log("resolvePythonPath: resolving Python interpreter…");
  const cfg = vscode.workspace.getConfiguration("renpy-lsp");
  const explicit: string = cfg.get<string>("pythonPath", "").trim();

  // 1. User-configured path
  if (explicit) {
    log(`resolvePythonPath: user configured pythonPath = "${explicit}"`);
    if (fs.existsSync(explicit)) {
      log(`resolvePythonPath: using configured path "${explicit}"`);
      return explicit;
    }
    logWarn(`resolvePythonPath: configured path "${explicit}" does not exist`);
    vscode.window.showWarningMessage(
      `Ren'Py LSP: configured pythonPath "${explicit}" does not exist. Falling back to auto-detect.`,
    );
  }

  // 2. VS Code Python extension's active interpreter
  const fromVSCode = await getPythonFromVSCodeExtension();
  if (fromVSCode) {
    log(`resolvePythonPath: resolved via VS Code Python extension: "${fromVSCode}"`);
    return fromVSCode;
  }

  // 3. Extension-local venv → workspace venvs
  log("resolvePythonPath: scanning local venvs…");
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
    log(`resolvePythonPath: resolved via local venv: "${detected}"`);
    return detected;
  }

  // 4. Last resort: bare "python3" (resolved by PATH at spawn time)
  logWarn('resolvePythonPath: falling back to bare "python3" on PATH');
  return "python3";
}

// ─── Activation ────────────────────────────────────────────────────────

export function activate(context: vscode.ExtensionContext) {
  log("Extension activating…");
  log(`Extension path: ${context.extensionPath}`);
  log(
    `Workspace folders: ${(vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath).join(", ") || "(none)"}`,
  );
  extensionContext = context;

  // Commands
  context.subscriptions.push(
    vscode.commands.registerCommand("renpy-lsp.startServer", () => {
      log("Command: startServer");
      startLanguageServer(context);
    }),
    vscode.commands.registerCommand("renpy-lsp.stopServer", () => {
      log("Command: stopServer");
      stopLanguageServer();
    }),
    vscode.commands.registerCommand("renpy-lsp.restartServer", async () => {
      log("Command: restartServer");
      await stopLanguageServer();
      startLanguageServer(context);
    }),
    vscode.commands.registerCommand("renpy-lsp.formatAllFiles", () => {
      log("Command: formatAllFiles");
      formatAllRpyFiles();
    }),
    vscode.commands.registerCommand("renpy-lsp.refreshWorkspace", () => {
      log("Command: refreshWorkspace");
      refreshWorkspace();
    }),
    vscode.commands.registerCommand("renpy-lsp.showStats", () => {
      log("Command: showStats");
      showProjectStats();
    }),
  );

  // Auto-start when .rpy files are present
  checkForRpyFiles().then((has) => {
    if (has) {
      log("Detected .rpy files in workspace — auto-starting language server");
      startLanguageServer(context);
    } else {
      log("No .rpy files detected in workspace, server will not auto-start");
    }
  });

  // Watch for new .rpy files
  const fileWatcher = vscode.workspace.createFileSystemWatcher("**/*.rpy");
  fileWatcher.onDidCreate((uri) => {
    log(`File watcher: new .rpy file created: ${uri.fsPath}`);
    if (!client) {
      log("No running client — starting language server…");
      startLanguageServer(context);
    }
  });
  context.subscriptions.push(fileWatcher);

  // Restart server on relevant config change
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("renpy-lsp")) {
        log("Configuration changed for renpy-lsp — restarting server");
        vscode.window.showInformationMessage("Ren'Py LSP config changed — restarting server…");
        stopLanguageServer().then(() => startLanguageServer(context));
      }
    }),
  );

  log("Extension activation complete");
}

export function deactivate(): Thenable<void> | undefined {
  log("Extension deactivating…");
  return stopLanguageServer();
}

// ─── .rpy detection ────────────────────────────────────────────────────

async function checkForRpyFiles(): Promise<boolean> {
  if (!vscode.workspace.workspaceFolders) {
    log("checkForRpyFiles: no workspace folders open");
    return false;
  }
  try {
    const files = await vscode.workspace.findFiles("**/*.rpy", "**/node_modules/**", 1);
    log(`checkForRpyFiles: found ${files.length} .rpy file(s)`);
    return files.length > 0;
  } catch (error) {
    logError("checkForRpyFiles: error searching for .rpy files", error);
    return false;
  }
}

// ─── Format all .rpy files ─────────────────────────────────────────────

async function formatAllRpyFiles() {
  log("formatAllRpyFiles: starting batch format");
  if (!client) {
    logWarn("formatAllRpyFiles: server not running");
    vscode.window.showWarningMessage("Ren'Py LSP: Language server is not running.");
    return;
  }

  const files = await vscode.workspace.findFiles("**/*.{rpy,rpym}", "**/node_modules/**");
  log(`formatAllRpyFiles: found ${files.length} file(s)`);
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
            log(`formatAllRpyFiles: formatted ${basename}`);
          }
        } catch (err) {
          logError(`formatAllRpyFiles: failed to format ${uri.fsPath}`, err);
          failed++;
        }
      }
    },
  );

  const parts: string[] = [`Formatted ${formatted} file(s)`];
  if (failed > 0) {
    parts.push(`${failed} failed`);
  }
  log(`formatAllRpyFiles: done — ${parts.join(", ")}`);
  vscode.window.showInformationMessage(`Ren'Py LSP: ${parts.join(", ")}.`);
}

// ─── Refresh Workspace ─────────────────────────────────────────────────

async function refreshWorkspace() {
  log("refreshWorkspace: sending request to server");
  if (!client) {
    logWarn("refreshWorkspace: server not running");
    vscode.window.showWarningMessage("Ren'Py LSP: Language server is not running.");
    return;
  }

  try {
    const result = (await client.sendRequest("workspace/executeCommand", {
      command: "renpy.refreshWorkspace",
      arguments: [],
    })) as { success: boolean; message: string; fileCount: number };

    if (result && result.success) {
      log(`refreshWorkspace: success — ${result.message} (${result.fileCount} files)`);
      vscode.window.showInformationMessage(`Ren'Py LSP: ${result.message}`);
    } else {
      logWarn("refreshWorkspace: server returned failure");
      vscode.window.showWarningMessage("Ren'Py LSP: Failed to refresh workspace.");
    }
  } catch (err) {
    logError("refreshWorkspace: request failed", err);
    vscode.window.showErrorMessage(`Ren'Py LSP: Error refreshing workspace: ${err}`);
  }
}

// ─── Show Project Stats ────────────────────────────────────────────────

interface ProjectStats {
  files: number;
  lines: number;
  labels: number;
  screens: number;
  defines: number;
  defaults: number;
  images: number;
  transforms: number;
  dialogueLines: number;
  words: number;
}

async function showProjectStats() {
  log("showProjectStats: requesting stats from server");
  if (!client) {
    logWarn("showProjectStats: server not running");
    vscode.window.showWarningMessage("Ren'Py LSP: Language server is not running.");
    return;
  }

  try {
    const stats = (await client.sendRequest("workspace/executeCommand", {
      command: "renpy.showStats",
      arguments: [],
    })) as ProjectStats;

    if (!stats) {
      logWarn("showProjectStats: server returned null stats");
      vscode.window.showWarningMessage("Ren'Py LSP: Failed to get project statistics.");
      return;
    }
    log(
      `showProjectStats: ${stats.files} files, ${stats.lines} lines, ${stats.labels} labels, ${stats.screens} screens`,
    );

    // Format the stats as a nice message
    const message = [
      `📁 Files: ${stats.files}`,
      `📝 Lines: ${stats.lines.toLocaleString()}`,
      `🏷️ Labels: ${stats.labels}`,
      `🖥️ Screens: ${stats.screens}`,
      `📌 Defines: ${stats.defines}`,
      `📋 Defaults: ${stats.defaults}`,
      `🖼️ Images: ${stats.images}`,
      `🔄 Transforms: ${stats.transforms}`,
      `💬 Dialogue: ${stats.dialogueLines.toLocaleString()} lines`,
      `📖 Words: ${stats.words.toLocaleString()}`,
    ].join("\n");

    // Show as an information message with a "Copy" button
    const choice = await vscode.window.showInformationMessage(
      `Ren'Py Project Statistics:\n\n${message}`,
      { modal: true },
      "Copy to Clipboard",
    );

    if (choice === "Copy to Clipboard") {
      await vscode.env.clipboard.writeText(message);
      vscode.window.showInformationMessage("Statistics copied to clipboard!");
    }
  } catch (err) {
    logError("showProjectStats: request failed", err);
    vscode.window.showErrorMessage(`Ren'Py LSP: Error getting statistics: ${err}`);
  }
}

// ─── Server lifecycle ──────────────────────────────────────────────────

async function startLanguageServer(context: vscode.ExtensionContext) {
  log("startLanguageServer: starting…");
  if (client) {
    log("startLanguageServer: server already running, skipping");
    return;
  }

  const serverScript = context.asAbsolutePath(path.join("bundled", "tools", "lsp_server.py"));
  log(`startLanguageServer: server script path = "${serverScript}"`);
  if (!fs.existsSync(serverScript)) {
    logError(`startLanguageServer: server script not found at "${serverScript}"`);
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
  log(`startLanguageServer: using Python interpreter "${pythonPath}"`);

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
  log("startLanguageServer: LanguageClient created, starting…");

  client.start().then(
    () => {
      log("startLanguageServer: server started successfully");
    },
    (error: Error) => {
      logError("startLanguageServer: failed to start server", error);
      vscode.window.showErrorMessage(`Failed to start Ren'Py Language Server: ${error.message}`);
      client = undefined;
    },
  );
}

async function stopLanguageServer(): Promise<void> {
  if (!client) {
    log("stopLanguageServer: no client running");
    return;
  }
  try {
    log("stopLanguageServer: stopping…");
    await client.stop();
    client = undefined;
    log("stopLanguageServer: server stopped");
  } catch (error) {
    logError("stopLanguageServer: error while stopping", error);
  }
}
