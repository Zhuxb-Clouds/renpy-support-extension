# Changelog

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
