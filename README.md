# Ren'Py Language Support

[中文文档](README.zh-CN.md)

A Visual Studio Code extension providing language support for Ren'Py script files (`.rpy`, `.rpym`).

## Features

- **Syntax Highlighting** — Full syntax highlighting for Ren'Py scripts, including screens, styles, ATL, and embedded Python
- **Code Formatting** — Automatic indentation and formatting via built-in LSP server
- **Diagnostics** — Warnings and errors for common issues:
  - Undefined `jump`/`call` labels, duplicate label/screen definitions, missing image files, unused labels
  - `show`/`scene`/`hide` of undefined images, `at` clauses referencing unknown transforms, `style x is parent` with an unknown parent
- **Quick Fixes** — Code Actions on diagnostics: create a missing label with one click, delete an unused label block
- **Context-Aware Completion** — labels, images, transforms, transitions, screens, styles, `define`/`default` variables (including dotted namespaces like `music.`), and screen/ATL/style properties
- **Translation ID Hover** — hovering a dialogue line shows the Ren'Py translation identifier
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
| `renpy-lsp.formatting.blankLines` | `collapse` | Blank-line handling: `preserve` (untouched), `collapse` (fold consecutive to one), `betweenSay` (also insert one between dialogue/narration lines inside `label` blocks), `strip` (remove all) |
| `renpy-lsp.diagnostics.enabled` | `true` | Enable diagnostics |
| `renpy-lsp.diagnostics.fullOnSave` | `false` | Run full cross-workspace diagnostics on every save |

### Per-project format config

Style settings (`indentSize`, `blankLines`) can be committed to the project via a `.renpy-format.json` file in the workspace root:

```json
{
  "indentSize": 2,
  "blankLines": "betweenSay"
}
```

When present, it overrides the corresponding VS Code settings per key.

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

### Release

Releases are automated through GitHub Actions (`.github/workflows/release.yml`):

1. Make sure `CHANGELOG.md` has a section for the new version and `package.json` / `pyproject.toml` carry that version.
2. Create and push a tag matching the version:

   ```bash
   git tag v1.7.0
   git push origin v1.7.0
   ```

3. The workflow runs the test suite, verifies the tag matches `package.json`, builds the `.vsix`, publishes it to the VS Code Marketplace (requires the `VSCE_PAT` repository secret — when absent this step is skipped), and creates a GitHub Release with the `.vsix` attached and the CHANGELOG section as release notes.

Pushes and pull requests are additionally checked by `.github/workflows/ci.yml` (Python tests + production bundle).

### Project Structure

- `src/extension.ts` — VS Code client entry point
- `bundled/tools/lsp_server.py` — Python LSP server (using pygls)
- `bundled/tools/ast_parser.py` — Indentation-aware Ren'Py parser
- `syntaxes/` — TextMate grammar files for syntax highlighting

## License

ISC License. See [LICENSE](LICENSE) for details.

## Contributing

Issues and pull requests are welcome at [GitHub](https://github.com/Zhuxb-Clouds/renpy-support-extension).
