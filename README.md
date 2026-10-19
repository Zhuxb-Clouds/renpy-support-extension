# Ren'Py Language Support

[中文文档](README.zh-CN.md)

A Visual Studio Code extension providing language support for Ren'Py script files (`.rpy`, `.rpym`).

## Features

- **Syntax Highlighting** — Full syntax highlighting for Ren'Py scripts, including screens, styles, ATL, and embedded Python
- **Code Formatting** — Automatic indentation and formatting via built-in LSP server
- **Context-Aware Completion** — labels, images, transforms, transitions, screens, styles, `define`/`default` variables (including dotted namespaces like `music.`), screen/ATL/style properties, and **displayable constructor keyword arguments** (`Transform(` → `xalign=…`, `Text(` → `kerning=…`) across multi-line calls, with already-written arguments filtered out
- **Signature Help** — `call label(...)` / `call screen ...(...)` show real parameters; statement templates document clause keywords (`at`, `with`, `fadeout`, …); displayable constructors (`Transform(`, `Text(`, `Fixed(` …) show their keyword-argument signature and highlight the argument under the cursor (keyword arguments matched by name)
- **Diagnostics** — Warnings and errors for common issues:
  - Undefined `jump`/`call` labels, duplicate label/screen definitions, missing image files, unused labels
  - `show`/`scene`/`hide` of undefined images, `at` clauses referencing unknown transforms, `style x is parent` with an unknown parent
  - Unclosed string literals, reported as errors on the line where the quote opened
- **Engine API Hover Docs** — 1070+ entries from the official Ren'Py documentation and the engine's source docstrings (`config.*`, `gui.*`, `renpy.*` functions, style/transform properties, displayable classes); hovering `kerning`, `config.screen_width` or `renpy.pause` shows the official description
- **Media Previews** — hovering a `show`/`scene`/`image` line renders the actual picture; `play`/`queue`/`voice` lines get a link that opens the audio/video in a preview panel
- **Workspace Symbols** — `Ctrl+T` fuzzy search across all labels, screens, transforms, images, styles, defines and defaults
- **Call Hierarchy** — `Shift+Alt+H` on a label (or a `jump`/`call` site) shows incoming callers and outgoing jump/call targets
- **Quick Fixes** — Code Actions on diagnostics: create a missing label with one click, delete an unused label block
- **Translation Tools** — stale-translation diagnostics for `tl/` files, go-to-definition between dialogue and its translation entry, and a per-language **Translation Report**
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
| `Ren'Py LSP: Show Translation Report` | Per-language translation coverage |
| `Ren'Py LSP: Run Ren'Py Lint` | Run the Ren'Py SDK lint (requires `renpy-lsp.sdkPath`) |
| `Ren'Py LSP: Launch Project` | Launch the game via the SDK launcher |

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `renpy-lsp.pythonPath` | `""` | Custom Python interpreter path (auto-detect if empty) |
| `renpy-lsp.sdkPath` | `""` | Ren'Py SDK directory (enables the lint & launch commands) |
| `renpy-lsp.formatting.enabled` | `true` | Enable document formatting |
| `renpy-lsp.formatting.indentSize` | `4` | Spaces per indentation level |
| `renpy-lsp.formatting.blankLines` | `collapse` | Blank-line handling: `preserve` (untouched), `collapse` (fold consecutive to one), `betweenSay` (also insert one between dialogue/narration lines inside `label` blocks), `strip` (remove all) |
| `renpy-lsp.diagnostics.enabled` | `true` | Enable diagnostics |
| `renpy-lsp.diagnostics.fullOnSave` | `false` | Also re-run the full pass on save (edits already re-run it; saves re-run only when the content changed) |
| `renpy-lsp.diagnostics.severity` | `{}` | Per-check severity overrides, e.g. `{ "unused-label": "none", "undefined-transform": "information" }` — `none` disables a check |
| `renpy-lsp.previews.enabled` | `true` | Media previews on hover: pictures for `show`/`scene`/`image` lines, audio/video preview links for `play`/`queue`/`voice` lines |
| `renpy-lsp.logLevel` | `info` | Language server log verbosity in the Output panel (`error`/`warning`/`info`/`debug`) |

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
- `bundled/tools/renpy_api_docs.json` — Engine API hover docs, generated by `scripts/fetch_api.py` (`python scripts/fetch_api.py --ref <renpy tag>` regenerates it; `--offline` re-parses the download cache)
- `syntaxes/` — TextMate grammar files for syntax highlighting

## License

ISC License. See [LICENSE](LICENSE) for details.

## Contributing

Issues and pull requests are welcome at [GitHub](https://github.com/Zhuxb-Clouds/renpy-support-extension).
