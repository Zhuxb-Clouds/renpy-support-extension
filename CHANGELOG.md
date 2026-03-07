# Changelog

## [1.2.1] - 2026-03-07

### Improved

- **Major performance optimization for the language server**
  - Added 300ms debounce on `didChange` — typing no longer triggers diagnostics on every keystroke
  - Split diagnostics into lightweight (syntax-only, on change) and full (cross-workspace, on save/open)
  - Introduced `_WorkspaceIndex` for incremental workspace indexing — file lists and symbol indices are cached and updated incrementally instead of re-globbing and re-parsing all files on every request
  - Parse cache now uses content hashing (`hash()`) instead of full-text string comparison
  - `_get_parse_for_file` uses O(1) path→URI mapping instead of O(n) cache scan
  - Added `didSave` handler for full diagnostics on save
  - Added `workspace/didChangeWatchedFiles` handler to invalidate caches on file create/delete/external changes
  - `refreshWorkspace` command now rebuilds the workspace index

## [1.2.0] - 2026-03-03

### Added

- Comprehensive logging throughout the extension and language server
  - New "Ren'Py LSP" Output Channel in VS Code for client-side logs
  - Server-side logging for all LSP features (parse, diagnostics, hover, completion, etc.)
  - Timestamped log entries with severity levels (INFO / WARN / ERROR)
  - Detailed logs for Python interpreter resolution, server lifecycle, and command execution
- GLSL (OpenGL Shading Language) syntax highlighting inside `renpy.register_shader()` strings
  - Supports `vertex_*`, `fragment_*`, `variables`, and other shader keyword arguments
  - Highlights types, qualifiers, built-in functions/variables, comments, numbers, operators, and swizzle


### Fixed

- Suppressed noisy "Cancel notification for unknown message id" warnings from pygls
  - These are harmless and occur when VS Code cancels already-completed requests

## [1.1.0] - 2026-03-02

### Added

- "Show Project Statistics" command (`renpy-lsp.showStats`) displaying:
  - File count, total lines, labels, screens, defines, defaults, images, transforms
  - Dialogue line count and word count
- "Refresh Workspace" command (`renpy-lsp.refreshWorkspace`) to re-parse all files

### Fixed

- Word counting now correctly handles CJK (Chinese/Japanese/Korean) characters
  - Each CJK character counts as one word
  - Non-CJK text is split by whitespace as before

## [1.0.2] - 2026-02-28

### Added

- Chinese documentation (README.zh-CN.md)

### Fixed

- Improved audio file path resolution

## [1.0.1] - 2026-02-28

### Fixed

- Updated extension icon
- Improved Python interpreter auto-detection

## [1.0.0] - 2026-02-28

### Added

- Syntax highlighting for Ren'Py `.rpy` and `.rpym` files
- Ren'Py code injection highlighting in Python and Markdown files
- Built-in document formatter with configurable indentation
- "Format All Ren'Py Files" command for batch formatting
- Go to Definition for labels, screens, defines, defaults, images, and transforms
- Find All References for labels, defines, defaults, and screens
- Document Symbols (outline) view
- Hover information for labels, screens, defines, defaults, images, and transforms
- Diagnostics for duplicate labels and screens
- Auto-detection of Python interpreter (`.venv` → system `python3`)
- Configurable Python interpreter path via `renpy-lsp.pythonPath`
- Language Server start / stop / restart commands
