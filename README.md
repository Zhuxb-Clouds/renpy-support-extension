# Ren'Py Language Support

[中文文档](README.zh-CN.md)

A Visual Studio Code extension providing language support for Ren'Py script files (`.rpy`, `.rpym`).

## Features

- **Syntax Highlighting** — Full syntax highlighting for Ren'Py scripts, including screens, styles, ATL, and embedded Python
- **Code Formatting** — Automatic indentation and formatting via built-in LSP server
- **Diagnostics** — Warnings and errors for common issues
- **Markdown Support** — Syntax highlighting for Ren'Py code blocks in Markdown files

## Installation

Install from the [VS Code Marketplace](https://marketplace.visualstudio.com/items?itemName=zhuxb-clouds.renpy-support-extension) or search for "Ren'Py Language Support" in VS Code Extensions.

## Requirements

- VS Code 1.74.0 or higher
- Python 3.11+ (for the language server)

The extension will auto-detect Python from `.venv/bin/python3` or system `python3`. You can also configure a custom path in settings.

## Commands

Open Command Palette (`Ctrl+Shift+P` / `Cmd+Shift+P`) and type:

| Command | Description |
|---------|-------------|
| `Ren'Py LSP: Start Language Server` | Start the LSP server |
| `Ren'Py LSP: Stop Language Server` | Stop the LSP server |
| `Ren'Py LSP: Restart Language Server` | Restart the LSP server |
| `Ren'Py LSP: Format All Ren'Py Files` | Format all `.rpy` files in workspace |

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `renpy-lsp.pythonPath` | `""` | Custom Python interpreter path (auto-detect if empty) |
| `renpy-lsp.formatting.enabled` | `true` | Enable document formatting |
| `renpy-lsp.formatting.indentSize` | `4` | Spaces per indentation level |
| `renpy-lsp.diagnostics.enabled` | `true` | Enable diagnostics |

## Development

### Setup

```bash
# Clone the repository
git clone https://github.com/Zhuxb-Clouds/renpy-support-extension.git
cd renpy-support-extension

# Install Node.js dependencies
npm install

# Create Python virtual environment
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Build

```bash
npm run compile      # Development build
npm run package      # Production build
npm run vsix         # Build .vsix package
```

### Project Structure

- `src/extension.ts` — VS Code client entry point
- `bundled/tools/lsp_server.py` — Python LSP server (using pygls)
- `bundled/tools/ast_parser.py` — Indentation-aware Ren'Py parser
- `syntaxes/` — TextMate grammar files for syntax highlighting

## License

ISC License. See [LICENSE](LICENSE) for details.

## Contributing

Issues and pull requests are welcome at [GitHub](https://github.com/Zhuxb-Clouds/renpy-support-extension).
