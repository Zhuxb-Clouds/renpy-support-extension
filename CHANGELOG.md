# Changelog

## [1.9.2] - 2026-10-03

### Fixed

- `show screen` / `hide screen` are parsed as their own statements — `hide screen twitter_feed with dissolve` no longer reports `Image "screen twitter_feed" is not defined`. `hide screen` gains the same support as `show screen`: go-to-definition, find-references, and rename now cover it, and `show screen` accepts a trailing `with` clause.

## [1.9.1] - 2026-10-03

### Fixed

- `show X at <transform> as <tag>` no longer glues the `as` clause onto the transform names — `Transform "pos_center as noel" is not defined` style false positives are gone, and a defined `pos_left`/`pos_center` is recognized again. `Scene`/`Camera` clauses and `behind`/`onlayer`/`zorder` glued onto the image expression are stripped too, which also fixes go-to-definition on such `show` lines.
- `at fx_waves(dark=dark, lite=lite)` is treated as one expression — keyword arguments no longer surface as transform names (`Transform "lite=lite)" is not defined`).
- `hide <tag>` / `show <tag>` recognize tags introduced by `show X as tag` across the workspace (indexed per file), so `Image "chapter_show_display" is not defined` no longer fires for displayables shown via `show expression … as tag`.

### Changed

- **Diagnostics refresh on change, not on save.** The full pass now runs on file open and through a coalescing background queue on every edit — warnings stay up to date while typing instead of being cleared and only flooding back after a save or window reload. A save re-runs the pass only when `renpy-lsp.diagnostics.fullOnSave` is enabled *and* the content changed since the last run (content-hash guard); unchanged saves re-run nothing.
- **New `renpy-lsp.diagnostics.severity` setting** — per-check severity overrides (`"error"|"warning"|"information"|"hint"|"none"`), e.g. `{ "unused-label": "none", "undefined-transform": "information" }`; `none` suppresses a check entirely. Applies immediately on settings change without reopening files.

## [1.9.0] - 2026-09-29

### Added

- **Signature help** (`textDocument/signatureHelp`) — typing `call label(...)` or `call screen name(...)` shows the target's real parameters from the workspace with per-argument highlighting; clause layouts for `show`/`scene`/`hide`/`play`/`queue`/`stop`/`camera`/`with`/`define`/`label`/… show the accepted clause keywords (`at`, `with`, `fadeout`, …) with the active clause tracked from the cursor.
- **Unused variable diagnostics** — `define`/`default` names never used outside their own definition line are hinted with the *Unnecessary* tag (`unused-define`). Word-based usage scan across the workspace (false-negative by design, so dialogue text containing the name keeps it "used"); dotted namespaces, `_`-prefixed internals, and engine-read names like `save_name` are exempt.
- **Translation tooling for `tl/` directories**
  - Consistency diagnostics: `old`/`new` pairing errors (`translation-missing-new`/`-missing-old`, errors), `old` strings no longer present in the project (`translation-stale`, warning), and dialogue blocks whose `<label>_<hash>` id no longer matches any source say statement (`translation-stale`).
  - Translation navigation: go-to-definition from a dialogue line jumps to its `translate` entry in `tl/` (dialogue block by translation id, `strings` block by `old` text); from a tl entry (`new` line, dialogue line, or `translate` header) jumps back to the source line.
  - **Show Translation Report** command — per-language coverage (dialogue translated/total, stale counts, `strings` pairs) with a sample of stale entries in the Output channel.
- **Ren'Py SDK integration** (client-side)
  - New `renpy-lsp.sdkPath` setting pointing at the SDK directory.
  - **Run Ren'Py Lint** command — runs the SDK's `lint`, parses `lint.txt`/stdout into the Problems panel (collection `renpy-lint`) and the Output channel.
  - **Launch Project** command — launches the game through the SDK launcher.

### Changed

- New server modules: `translation.py` (tl checks/navigation/reporting — the translation-id helpers moved out of `lsp_server.py`) and `signatures.py`. Test suite grew 66 → 86 (`test_signatures.py`, `test_translation.py`, unused-define cases).

## [1.8.0] - 2026-09-29

### Added

- **New cross-workspace diagnostics**
  - `show`/`scene`/`hide` of an image that is neither defined via `image` statements nor auto-detected under `images/` now warns (`undefined-image`). Built-in displayables (`black`, `white`, `window`), `show expression`, `show layer`, and bare `scene` are exempt, and attribute lists like `show eileen vhappy` are satisfied by any image with the `eileen` tag.
  - `at` clauses referencing an unknown transform now warn (`undefined-transform`). `transform` definitions, built-in positionals (`left`, `center`, …), `define`/`default` names (e.g. `define zoom = Transform(...)`) and inline expressions such as `Transform(xpos=0.5)` are all accepted.
  - `style x is parent` with an unknown parent now warns (`undefined-style`). Project styles and Ren'Py's built-in styles (`default`, `button_text`, `say_dialogue`, …) are recognized.
- **Quick fixes (Code Actions)**
  - `Label "…" is not defined in the project` → *Create label "…"* appends a stub `label …:` block at the end of the file.
  - `Label "…" is defined but never used` → *Delete unused label "…"* removes the whole label block (without swallowing the blank-line separator before the next statement).
- All diagnostics now carry machine-readable `code` values (`undefined-label`, `unused-label`, `undefined-image`, `undefined-transform`, `undefined-style`, `duplicate-label`, `duplicate-screen`, `missing-image-file`, `empty-atl-block`, `unknown-statement`), which the quick fixes match on.

### Changed

- **Performance test suite** — new `tests/test_performance.py` (pytest `perf` marker, deselect with `-m "not perf"`): parser throughput and 2×-input scaling checks, 100-file workspace index warm-up, cold/warm full-diagnostics budgets, completion latency, and formatter throughput on a ~9k-line synthetic file. Measured on the synthetic project: 9k-line parse ≈ 90 ms (linear), index warm-up ≈ 160 ms, warm full diagnostics ≈ 2 ms.
- The new `undefined-transform` check resolves unknown names with a **single batched workspace scan** (`server_context._all_workspace_python_names()`) instead of one full scan per unknown name — the shape that the new scaling tests guard.
- **Server modularization** — `lsp_server.py` (~3550 lines) was split for maintainability: shared state and helpers moved to `server_context.py`, diagnostics (computation, publishing, background scheduling) to `diagnostics.py`, context-aware completion to `completion.py`, and quick fixes to `code_actions.py`. Behavior is unchanged; the pytest suite grew from 39 to 60 tests (new `test_diagnostics.py`, `test_code_actions.py`).

## [1.7.0] - 2026-09-29

### Added

- Completion now understands dotted define/default namespaces: with `define music.未命名1 = "audio/..."` in the project, typing `music.` anywhere in a script offers `未命名1` (plus every other `music.*` member, including nested namespaces such as `music.sub.*` level by level). Items show the full define path and defining file/line, and unknown namespaces fall back to the previous context-specific completions.
- Added automated release infrastructure: a CI workflow (Python tests + production bundle on pushes/PRs) and a tag-driven release workflow that runs tests, verifies the tag matches `package.json`, builds the `.vsix`, publishes to the VS Code Marketplace (gated on the `VSCE_PAT` secret), and creates a GitHub Release with the `.vsix` and CHANGELOG notes attached.

## [1.6.3] - 2026-08-19

### Fixed

- Fixed the formatter leaving a stray space after prefix (unary / splat) operators, e.g. `def f(label, * args):` and `pos (45, - 400)` were left untouched instead of being tightened to `*args` and `-400`. Prefix `*` / `**` / `-` / `+` followed by an operand are now collapsed; binary operators (including `a - -b`) keep their spacing.

## [1.6.2] - 2026-08-19

### Fixed

- Fixed the formatter leaving spaces around `-` in image names, producing invalid Ren'Py such as `image 便利店 - 内部:` (Ren'Py reports "image name components may not begin with a '-'"). Spaces around dashes in `image`/`show`/`scene`/`hide` image names are now collapsed, yielding the valid `image 便利店-内部:`.

## [1.6.1] - 2026-08-18

### Fixed

- Fixed the formatter inserting a stray space inside unary minus after an assignment, e.g. `init offset = -2` was rewritten as the invalid `init offset =  - 2` (same issue affected `define`/`default`/`image` assignments like `define foo = -2`). Unary `-`/`+` immediately following a spaced operator are now kept tight to their operand.

## [1.6.0] - 2026-08-18

### Added

- Added `renpy-lsp.formatting.blankLines` setting controlling blank-line handling when formatting: `preserve` (leave untouched), `collapse` (default, fold consecutive blank lines into one), `betweenSay` (also insert one blank line between dialogue/narration lines inside `label` script blocks), and `strip` (remove all blank lines)
- Added support for a `.renpy-format.json` file in the workspace root (keys: `indentSize`, `blankLines`); when present it overrides the corresponding VS Code settings per key, so format style can be committed with the project

### Fixed

- `renpy-lsp.formatting.indentSize` is now the single source of truth for formatting indentation — regular document formatting previously ignored it and followed `editor.tabSize` (only the batch "Format All Ren'Py Files" command honored it)

## [1.5.0] - 2026-08-14

### Added

- **Python pytest test base**
  - Added focused pytest coverage for `ast_parser`, `WorkspaceIndex`, and LSP completion helpers
  - Added parser regressions for nested parameters, tuple unpacking loops, camera ATL blocks, styles, and error recovery
  - Added workspace index coverage for style aggregation

- **Expanded context-aware completions**
  - Added `define`/`default` variable completions in general Ren'Py contexts
  - Added style-name completions for `style ... is ...` and screen `style` properties
  - Added screen-language displayable/property completions inside `screen` blocks
  - Added ATL statement/property completions inside `transform` blocks
  - Added style property completions inside `style` blocks

- **Ren'Py snippets**
  - Added snippets for `label`, `screen`, `menu`, and `define`

### Fixed

- Fixed context completion triggers that depended on trailing spaces, such as `jump `, `with `, `at `, `call screen `, and `play music `
- Fixed TextMate highlighting for Unicode image names and `scene ... as ...` clauses, e.g. `scene 便利店内 as bg with fade`
- Expanded formatter spacing normalization for common expressions outside strings/comments, including commas, statement-level assignments, comparison operators, and binary arithmetic such as `matrixcolor TintMatrix('#EEFDFD') *  BrightnessMatrix(0.1)`
- Improved save responsiveness by honoring `renpy-lsp.formatting.enabled`, honoring `renpy-lsp.diagnostics.enabled`, and adding `renpy-lsp.diagnostics.fullOnSave` for opt-in cross-workspace diagnostics on save

## [1.4.0] - 2026-06-20

### Fixed

- **Dramatically reduced false positive diagnostics in parser**
  - Fixed `screen`, `transform`, `label`, and `call` definition regexes to support nested parentheses in parameters — previously a single unmatched definition would cascade into hundreds of false "Unrecognized statement" warnings for all indented body lines
  - Fixed `for` loop regex to support tuple unpacking (e.g. `for key, value in dict.items():`)
  - Fixed `window` statement regex to support optional transition arguments

### Added

- **`camera` statement support**
  - Added full parsing for `camera` and `camera <layer>` statements with `at` and `with` clauses
  - Camera blocks are now recognized as block nodes, suppressing false errors for ATL body content

## [1.3.1] - 2026-04-08

### Improved

- **Refactored language server into modular architecture**
  - Extracted `renpy_data.py` — pure-data constants (`RENPY_KEYWORDS`, `RENPY_TRANSITIONS`, `RENPY_TRANSFORMS`, `KEYWORD_DOCS`, `count_words()`) with zero runtime dependencies
  - Extracted `workspace_index.py` — `WorkspaceIndex` class with dependency injection, avoiding circular imports
  - Reduced `lsp_server.py` from ~3200 lines to ~2600 lines for better maintainability

## [1.3.0] - 2026-04-03

### Added

- **Translation ID hover for dialogue lines**
  - Hovering over any `say` or narrator say line now shows the Ren'Py translation identifier (e.g. `start_a1b2c3d4`)
  - Uses the same MD5-based algorithm as Ren'Py itself, so IDs match what `renpy.translation` generates
  - Works with labeled and unlabeled blocks, character names, and narrator dialogue

## [1.2.4] - 2026-03-17

### Fixed

- **Fixed syntax highlighting for Chinese (Unicode) character names in say statements**
  - The `say-statements` TextMate grammar now uses `\p{L}` (Unicode letter) in character name patterns, so non-ASCII identifiers like `小明 "你好"` are correctly highlighted

- **Formatter now normalizes spacing between character name and dialogue to exactly 1 space**
  - e.g. `小明    "你好"` → `小明 "你好"` on format
  - Works with ASCII and Unicode character names, including `character.` prefix

## [1.2.3] - 2026-03-13

### Improved

- **Label hover now shows leading comment block as description**
  - Comments immediately following `label name:` are extracted and displayed in the hover popup
  - Each comment line is shown on a separate line with proper Markdown line breaks

### Fixed

- **Removed erroneous dialogue text from label hover**
  - Fixed `textDocument/definition` returning a self-referential location for label/screen definitions, which caused VS Code to render a source-code preview (including unrelated dialogue) in the hover popup
  - Label/screen definition lines now only return jump/call usage locations (or `None` if no usages exist)

- **Fixed Windows label name self-conflict**
  - Path normalization (`os.path.normcase`) prevents the same file from being indexed under multiple URI variants on case-insensitive file systems
  - Duplicate label/screen checks now use case-insensitive URI comparison

- **Fixed format request blocking on diagnostics**
  - `didOpen`/`didSave` diagnostics now run in a background thread, so formatting and other requests are no longer blocked
  - Added `_cache_lock` and `_diag_lock` for thread safety

- **Improved unused-label check performance**
  - Replaced full workspace file scan with pre-indexed jump/call targets (`_WorkspaceIndex.get_used_labels()`)

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
