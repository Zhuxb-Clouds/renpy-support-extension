# Ren'Py LSP — VS Code extension

This repository contains a small Visual Studio Code extension that provides basic language support for Ren'Py `.rpy` files by launching a bundled Python language server (LSP).

Quick overview
- The VS Code client is TypeScript and lives in `src/extension.ts`.
- The bundled Python LSP server is `bundled/tools/lsp_server.py` and uses `pygls` + `lsprotocol`.
- A simple indentation-aware parser lives at `bundled/tools/ast_parser.py` and is the canonical parser for language features.

Developer setup

1. Ensure Python 3.11 is available on your PATH. The extension expects `.venv/bin/python3.11` by default.

2. Create a virtual environment and install Python deps:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

If you don't have `python3.11` but have a system `python3`, replace `python3.11` with `python3` above (server requires Python >= 3.11 ideally).

Running the Python LSP server manually

After creating the venv and installing requirements you can run the server directly to inspect logs:

```bash
source .venv/bin/activate
./.venv/bin/python3.11 bundled/tools/lsp_server.py
```

Client (extension) build & test

- Install Node deps as usual (this repo uses npm):

```bash
npm install
```

- Build extension bundle:

```bash
npm run compile
```

- Run tests:

```bash
npm run test
```

Key developer notes
- The extension launches the Python process using `TransportKind.stdio` in `src/extension.ts` — don't change transport to sockets without updating both sides.
- The server currently implements full-document formatting and returns a single `TextEdit` replacing the whole document. Preserve that contract when modifying formatting behavior.
- The language activation is controlled in `package.json` via `activationEvents` and the language configuration is in `language-configuration.json`.

Files to inspect first
- `src/extension.ts` — client startup, server spawn, commands
- `bundled/tools/lsp_server.py` — LSP handlers (formatting currently)
- `bundled/tools/ast_parser.py` — indentation-aware parser used by the server
- `language-configuration.json` — indentation / comment rules for the editor

If any of the above assumptions (Python path, venv layout, activation behavior) are incorrect for your environment, edit `src/extension.ts` to point to the correct Python binary or tell me and I can update this README and the extension.

