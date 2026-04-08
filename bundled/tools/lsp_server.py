"""Ren'Py Language Server — LSP features powered by ast_parser."""

from __future__ import annotations

import sys
import os
import re

# Ensure the bundled/tools directory is on sys.path so we can import ast_parser.
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _TOOLS_DIR)

# Ensure bundled third-party libraries (pygls, lsprotocol, …) are importable.
_LIBS_DIR = os.path.join(os.path.dirname(_TOOLS_DIR), "libs")
if os.path.isdir(_LIBS_DIR):
    sys.path.insert(0, _LIBS_DIR)

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import (
    from_fs_path as _pygls_from_fs_path,
    to_fs_path as _pygls_to_fs_path,
)

from ast_parser import (
    RpyParser,
    Script,
    Label,
    Define,
    Default,
    ImageDef,
    TransformDef,
    ScreenDef,
    StyleDef,
    If,
    Elif,
    While,
    For,
    Menu,
    MenuItem,
    Jump,
    Call,
    Return,
    Pass,
    Say,
    NarratorSay,
    Scene,
    Show,
    Hide,
    With,
    PlayMusic,
    StopMusic,
    QueueMusic,
    Voice,
    PythonBlock,
    PythonOneliner,
    Init,
    Translate,
    Comment,
    Unknown,
    Node,
    CallScreen,
    ShowScreen,
)

from renpy_data import (
    RENPY_KEYWORDS,
    RENPY_TRANSITIONS,
    RENPY_TRANSFORMS,
    KEYWORD_DOCS,
    count_words,
)
from workspace_index import WorkspaceIndex

from typing import Dict, Generator, List, Optional, Tuple, Union
import glob
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote as url_quote
from urllib.parse import unquote as url_unquote

MAX_WORKERS = 4
LSP_SERVER = LanguageServer(
    name="renpy-server", version="1.0.0", max_workers=MAX_WORKERS
)

# Suppress noisy "Cancel notification for unknown message id" warnings.
# These occur normally when VS Code cancels requests the server already finished.
import logging as _logging

_logging.getLogger("pygls.protocol.json_rpc").setLevel(_logging.ERROR)

# ── Server logger (prints to stderr, which VS Code captures in the Output channel) ──
import time as _time

_log = _logging.getLogger("renpy-lsp")
_log.setLevel(_logging.DEBUG)
# On Windows the default stderr encoding may not be UTF-8, which garbles CJK
# characters in log output.  Force UTF-8 so diagnostics are readable.
_handler = _logging.StreamHandler(
    open(sys.stderr.fileno(), mode="w", encoding="utf-8", closefd=False)
    if sys.platform == "win32"
    else sys.stderr
)
_handler.setFormatter(
    _logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
)
_log.addHandler(_handler)

_log.info("Ren'Py LSP server module loaded")


# ── UTF-16 → Python (UTF-32) column offset conversion ──────────────────


def _utf16_col_to_utf32(line: str, utf16_col: int) -> int:
    """Convert a UTF-16 character offset to a Python string index.

    LSP positions use UTF-16 code units by default.  Characters outside the
    Basic Multilingual Plane (e.g. emoji) take 2 UTF-16 units but 1 Python
    character.  This helper walks the line to map the offset correctly.
    """
    utf16_pos = 0
    for i, ch in enumerate(line):
        units = 2 if ord(ch) > 0xFFFF else 1
        if utf16_pos + units > utf16_col:
            return i
        utf16_pos += units
    return len(line)


# ─────────────────────── Cache / Index ───────────────────────────────────

# Per-URI parse cache so we don't re-parse on every request.
# Value: (content_hash, source_text, ast, parser)
_parse_cache: Dict[str, Tuple[int, str, Script, RpyParser]] = {}

# Fast path→URI mapping: avoids O(n) scan in _get_parse_for_file.
_path_to_uri: Dict[str, str] = {}

# Lock protecting _parse_cache and _path_to_uri from concurrent access
# (background diagnostics thread vs. main event-loop).
_cache_lock = threading.Lock()


def _normalize_path_key(path: str) -> str:
    """Normalize a filesystem path for use as a dictionary key.

    On Windows (case-insensitive FS) this lowercases the whole path so that
    ``C:\\Foo\\bar.rpy`` and ``c:\\foo\\bar.rpy`` map to the same key.
    On Linux/macOS it's a no-op beyond ``abspath``.
    """
    return os.path.normcase(os.path.abspath(path))


def _same_file_uri(uri1: str, uri2: str) -> bool:
    """Return *True* if two file URIs refer to the same file.

    Fast-path: exact string match.  Slow-path (Windows): normalise
    both sides through *normcase* before comparing.
    """
    if uri1 == uri2:
        return True
    try:
        return _normalize_path_key(_path_from_uri(uri1)) == _normalize_path_key(
            _path_from_uri(uri2)
        )
    except Exception:
        return False


def _get_parse(uri: str, source: Optional[str] = None) -> Tuple[Script, RpyParser]:
    """Return cached (ast, parser) for *uri*, re-parsing only when source changes."""
    doc = LSP_SERVER.workspace.get_text_document(uri)
    text = source if source is not None else doc.source
    text_hash = hash(text)
    with _cache_lock:
        cached = _parse_cache.get(uri)
        if cached and cached[0] == text_hash:
            _log.debug("_get_parse: cache hit for %s", _short_uri(uri))
            return cached[2], cached[3]
    _log.info("_get_parse: parsing %s (%d chars)", _short_uri(uri), len(text))
    t0 = _time.monotonic()
    parser = RpyParser(text)
    ast = parser.parse()
    elapsed = (_time.monotonic() - t0) * 1000
    _log.info(
        "_get_parse: parsed %s in %.1f ms (%d top-level nodes)",
        _short_uri(uri),
        elapsed,
        len(ast.body),
    )
    with _cache_lock:
        _parse_cache[uri] = (text_hash, text, ast, parser)
        # Maintain path→URI mapping (normalized key for Windows compat)
        try:
            norm_key = _normalize_path_key(_path_from_uri(uri))
            _path_to_uri[norm_key] = uri
        except Exception:
            pass
    return ast, parser


def _short_uri(uri: str) -> str:
    """Return a short display name for a URI (just the filename)."""
    return os.path.basename(_path_from_uri(uri))


def _uri_from_path(path: str) -> str:
    """Convert a filesystem path to a file:// URI."""
    result = _pygls_from_fs_path(os.path.abspath(path))
    if result is not None:
        return result
    # Fallback for non-file paths
    return Path(os.path.abspath(path)).as_uri()


def _path_from_uri(uri: str) -> str:
    """Convert a file:// URI to a filesystem path (Windows-safe)."""
    result = _pygls_to_fs_path(uri)
    if result is not None:
        return result
    # Fallback: strip scheme for non-file URIs
    if uri.startswith("file://"):
        return url_unquote(uri[len("file://") :])
    return uri


def _get_workspace_rpy_files() -> List[str]:
    """Return all .rpy / .rpym file paths in the workspace (uses cached list)."""
    return _workspace_index.get_file_list()


def _get_workspace_renpy_py_files() -> List[str]:
    """Return all ``*_ren.py`` file paths in the workspace.

    These are pure-Python files that Ren'Py loads alongside ``.rpy`` scripts.
    They typically contain class and function definitions.
    """
    results: List[str] = []
    for folder in LSP_SERVER.workspace.folders.values():
        root = _path_from_uri(folder.uri)
        results.extend(glob.glob(os.path.join(root, "**", "*_ren.py"), recursive=True))
    return results


def _get_parse_for_file(filepath: str) -> Tuple[str, Script, RpyParser]:
    """Parse (or cache-hit) a file by filesystem path. Returns (uri, ast, parser)."""
    norm_key = _normalize_path_key(filepath)
    with _cache_lock:
        # O(1) lookup via path→URI map
        cached_uri = _path_to_uri.get(norm_key)
        if cached_uri and cached_uri in _parse_cache:
            cached_data = _parse_cache[cached_uri]
            return cached_uri, cached_data[2], cached_data[3]
        # Compute URI and try cache directly
        uri = _uri_from_path(filepath)
        cached_data = _parse_cache.get(uri)
        if cached_data:
            _path_to_uri[norm_key] = uri
            return uri, cached_data[2], cached_data[3]
    # No existing cache entry found — parse and cache
    try:
        text = Path(filepath).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    parser = RpyParser(text)
    ast = parser.parse()
    with _cache_lock:
        _parse_cache[uri] = (hash(text), text, ast, parser)
        _path_to_uri[norm_key] = uri
    return uri, ast, parser


# ─────────────────────── Workspace Index ─────────────────────────────────

# The WorkspaceIndex class lives in workspace_index.py.
# We instantiate it here with injected dependencies (cache, utils).
_workspace_index = WorkspaceIndex(
    server=LSP_SERVER,
    parse_cache=_parse_cache,
    cache_lock=_cache_lock,
    path_to_uri=_path_to_uri,
    path_from_uri_fn=_path_from_uri,
    normalize_path_fn=_normalize_path_key,
    get_parse_for_file_fn=_get_parse_for_file,
)


def _get_all_workspace_labels() -> Dict[str, List[Tuple[str, "Label"]]]:
    """Return {label_name: [(uri, Label), ...]} across all workspace .rpy files."""
    return _workspace_index.get_labels()


def _get_all_workspace_defines() -> Dict[str, List[Tuple[str, "Define"]]]:
    """Return {name: [(uri, Define), ...]} across all workspace .rpy files."""
    return _workspace_index.get_defines()


def _get_all_workspace_defaults() -> Dict[str, List[Tuple[str, "Default"]]]:
    """Return {name: [(uri, Default), ...]} across all workspace .rpy files."""
    return _workspace_index.get_defaults()


def _get_all_workspace_screens() -> Dict[str, List[Tuple[str, "ScreenDef"]]]:
    """Return {name: [(uri, ScreenDef), ...]} across all workspace .rpy files."""
    return _workspace_index.get_screens()


def _get_all_workspace_images() -> Dict[str, List[Tuple[str, "ImageDef"]]]:
    """Return {image_name: [(uri, ImageDef), ...]} across all workspace .rpy files."""
    return _workspace_index.get_images()


def _get_all_workspace_transforms() -> Dict[str, List[Tuple[str, "TransformDef"]]]:
    """Return {name: [(uri, TransformDef), ...]} across all workspace .rpy files."""
    return _workspace_index.get_transforms()


# ── Python variable / class / function definition helpers ──

# Regex patterns for Python definitions
_RE_PY_ASSIGN = re.compile(r"""^\s*([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf]\w*)\s*=[^=]""")
_RE_PY_CLASS = re.compile(r"""^\s*class\s+([a-zA-Z_]\w*)\s*[:(]""")
_RE_PY_DEF = re.compile(r"""^\s*def\s+([a-zA-Z_]\w*)\s*\(""")


def _find_python_definitions_in_file(
    uri: str, parser: RpyParser, ast: Script
) -> Dict[str, List[Tuple[int, str]]]:
    """Find Python variable assignments, class and function definitions
    in python: blocks and $ one-liners.

    Returns {name: [(lineno, code_snippet), ...]}.
    """
    result: Dict[str, List[Tuple[int, str]]] = {}

    # Collect ALL PythonOneliner nodes — this covers both:
    #   - standalone ``$ var = ...`` one-liners
    #   - lines inside ``python:`` blocks (parser stores them as PythonOneliner children)
    for node in parser._collect(ast, PythonOneliner):
        code = node.code
        # Variable assignment:  var = ...
        m = _RE_PY_ASSIGN.match(code)
        if m:
            result.setdefault(m.group(1), []).append((node.lineno, code.strip()))
            continue
        # Class definition:  class Foo(...):
        m = _RE_PY_CLASS.match(code)
        if m:
            result.setdefault(m.group(1), []).append((node.lineno, code.strip()))
            continue
        # Function definition:  def bar(...):
        m = _RE_PY_DEF.match(code)
        if m:
            result.setdefault(m.group(1), []).append((node.lineno, code.strip()))

    return result


# Cache for *_ren.py definitions so we don't re-scan every request.
_renpy_py_cache: Dict[str, Tuple[str, Dict[str, List[Tuple[int, str]]]]] = {}


def _find_python_definitions_in_py_file(
    filepath: str,
) -> Tuple[str, Dict[str, List[Tuple[int, str]]]]:
    """Scan a pure-Python ``*_ren.py`` file for top-level class/def/assignment.

    Returns (uri, {name: [(lineno, code_snippet), ...]}).
    """
    uri = _uri_from_path(filepath)
    try:
        text = Path(filepath).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return uri, {}

    cached = _renpy_py_cache.get(uri)
    if cached and cached[0] == text:
        return uri, cached[1]

    result: Dict[str, List[Tuple[int, str]]] = {}
    for lineno_0, line in enumerate(text.splitlines()):
        lineno = lineno_0 + 1  # 1-based
        # Only match top-level definitions (no leading whitespace) —
        # method-level defs / local vars inside classes are not useful targets.
        if not line or line[0].isspace():
            continue
        m = _RE_PY_CLASS.match(line)
        if m:
            result.setdefault(m.group(1), []).append((lineno, line.strip()))
            continue
        m = _RE_PY_DEF.match(line)
        if m:
            result.setdefault(m.group(1), []).append((lineno, line.strip()))
            continue
        m = _RE_PY_ASSIGN.match(line)
        if m:
            result.setdefault(m.group(1), []).append((lineno, line.strip()))

    _renpy_py_cache[uri] = (text, result)
    return uri, result


def _find_python_var_across_workspace(
    var_name: str,
) -> List[Tuple[str, int, str]]:
    """Return [(uri, lineno, code), ...] for *var_name* across all workspace files.

    Searches .rpy/.rpym files (via AST) and *_ren.py files (via line scanning).
    Matches variable assignments, class definitions, and function definitions.
    """
    results: List[Tuple[str, int, str]] = []
    # 1) .rpy / .rpym files
    for fp in _get_workspace_rpy_files():
        uri, ast, parser = _get_parse_for_file(fp)
        defs = _find_python_definitions_in_file(uri, parser, ast)
        if var_name in defs:
            for lineno, code in defs[var_name]:
                results.append((uri, lineno, code))
    # 2) *_ren.py files
    for fp in _get_workspace_renpy_py_files():
        uri, defs = _find_python_definitions_in_py_file(fp)
        if var_name in defs:
            for lineno, code in defs[var_name]:
                results.append((uri, lineno, code))
    return results


# ── Ren'Py file search helpers ──


def _get_renpy_search_dirs() -> List[str]:
    """Return directories to search for Ren'Py assets (images, audio, etc.).

    Ren'Py uses ``config.searchpath`` which defaults to ``['common', 'game']``.
    The ``game/`` folder is the primary location for all assets.  We also check
    ``config.image_directories`` (default ``['images']``) for auto-detected images.
    """
    dirs: List[str] = []
    for folder in LSP_SERVER.workspace.folders.values():
        root = _path_from_uri(folder.uri)
        # game/ is the canonical Ren'Py asset directory
        game_dir = os.path.join(root, "game")
        if os.path.isdir(game_dir):
            dirs.append(game_dir)
        # Also add workspace root itself (covers non-standard layouts)
        dirs.append(root)
    return dirs


def _resolve_renpy_file(
    filename: str, source_uri: Optional[str] = None
) -> Optional[str]:
    """Resolve a Ren'Py file reference to an absolute filesystem path.

    Search strategy (first match wins):
      1. Relative to ``game/`` and workspace root (``config.searchpath`` defaults).
      2. Relative to the directory of the current ``.rpy`` file.
      3. Recursive glob ``**/<filename>`` across the workspace — this handles
         ``config.searchpath`` with custom directories that we cannot read at
         edit-time.
    Returns *None* if the file cannot be found.
    """
    if not filename:
        return None
    # Normalize separators
    filename = filename.replace("\\", "/")
    # Strip leading ./ if present
    if filename.startswith("./"):
        filename = filename[2:]

    # Collect all search directories
    search_dirs = list(_get_renpy_search_dirs())

    # Also search relative to the current .rpy file's directory — this is
    # important because .rpy files live in game/ and references like
    # "images/bg/xxx.png" are relative to game/.
    if source_uri:
        source_path = _path_from_uri(source_uri)
        source_dir = os.path.dirname(source_path)
        if source_dir and source_dir not in search_dirs:
            search_dirs.insert(0, source_dir)

    # Pass 1: direct relative lookup in known search dirs
    for search_dir in search_dirs:
        candidate = os.path.join(search_dir, filename)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)

    # Pass 2: recursive glob  **/<filename>  across workspace roots.
    # This covers config.searchpath with custom directories.
    for folder in LSP_SERVER.workspace.folders.values():
        root = _path_from_uri(folder.uri)
        pattern = os.path.join(root, "**", filename)
        hits = glob.glob(pattern, recursive=True)
        if hits:
            return os.path.abspath(hits[0])

    return None


# ── Image auto-name cache ──
# Maps lowercased image name → absolute file path.
# Invalidated on file create/delete (see did_change_watched_files).
_image_cache: Dict[str, str] = {}
_image_cache_built = False


def _ensure_image_cache() -> None:
    """Build the image auto-name → filepath index if not yet populated."""
    global _image_cache_built
    if _image_cache_built:
        return
    IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".avif", ".svg")
    cache: Dict[str, str] = {}
    for search_dir in _get_renpy_search_dirs():
        images_dir = os.path.join(search_dir, "images")
        if not os.path.isdir(images_dir):
            continue
        for dirpath, _dirnames, filenames in os.walk(images_dir):
            for fn in filenames:
                base, ext = os.path.splitext(fn)
                if ext.lower() not in IMAGE_EXTENSIONS:
                    continue
                abs_path = os.path.abspath(os.path.join(dirpath, fn))
                rel = os.path.relpath(os.path.join(dirpath, fn), images_dir)
                rel_no_ext = os.path.splitext(rel)[0]
                auto_name = rel_no_ext.replace(os.sep, " ").replace("/", " ").lower()
                if auto_name not in cache:
                    cache[auto_name] = abs_path
                base_lower = base.lower()
                if base_lower not in cache:
                    cache[base_lower] = abs_path
    _image_cache.update(cache)
    _image_cache_built = True
    _log.debug("_ensure_image_cache: indexed %d image entries", len(cache))


def _resolve_image_name_to_file(image_name: str) -> Optional[str]:
    """Try to find an image file matching *image_name* via Ren'Py auto-detection.

    Results are cached in ``_image_cache`` to avoid repeated directory walks.
    """
    _ensure_image_cache()
    return _image_cache.get(image_name.lower())


# ── AST / line analysis helpers ──


def _find_nodes_at_line(parser: RpyParser, lineno: int) -> List[Node]:
    """Return all AST nodes whose ``lineno`` matches *lineno* (1-based)."""
    result: List[Node] = []

    def _walk(node: Node):
        if node.lineno == lineno:
            result.append(node)
        for child in parser._children_of(node):
            _walk(child)

    _walk(parser.root)
    return result


def _cursor_on_image_name(line_text: str, col: int, image_name: str) -> bool:
    """Return True if *col* falls within the image-name span of a scene/show/hide line.

    For ``scene black with ImageDissolve("zc01",0.5)`` only the ``black``
    portion should be navigable.  The image name is the text between the
    keyword (``scene``/``show``/``hide``) and the first clause keyword
    (``at``, ``with``, ``behind``, ``as``, ``onlayer``, ``zorder``) or
    end-of-line.
    """
    m = re.match(r"^(\s*)(scene|show|hide)\s+", line_text, re.IGNORECASE)
    if not m:
        return False
    img_start = m.end()  # first char after "scene " / "show " / "hide "
    # Find where the image name ends — at the first clause keyword or EOL.
    rest = line_text[img_start:]
    clause = re.search(r"\s+(?:at|with|behind|as|onlayer|zorder)\s", rest)
    if clause:
        img_end = img_start + clause.start()
    else:
        # Might end with ':' or just EOL
        stripped = rest.rstrip()
        if stripped.endswith(":"):
            stripped = stripped[:-1].rstrip()
        img_end = img_start + len(stripped)
    return img_start <= col < img_end


def _extract_quoted_string(line: str, col: int) -> Optional[str]:
    """If the cursor is inside or on a quoted string, return its contents."""
    # Find all quoted strings in the line
    for m in re.finditer(r"""(["'])(.*?)\1""", line):
        # Match when cursor is anywhere from the opening quote to the closing quote
        if m.start() <= col <= m.end() - 1:
            return m.group(2)
    return None


def _make_file_location(filepath: str) -> types.Location:
    """Create a Location pointing to line 1 of a file."""
    return types.Location(
        uri=_uri_from_path(filepath),
        range=types.Range(
            start=types.Position(line=0, character=0),
            end=types.Position(line=0, character=0),
        ),
    )


def _make_node_location(uri: str, node: Node) -> types.Location:
    """Create a Location pointing to a node's name."""
    line = node.lineno - 1
    start_char = 0
    end_char = 10000  # Large value, will be clipped by VS Code

    # Try to find the exact position of the node's name
    if hasattr(node, "name") and node.name:
        name = node.name
        # Get the raw line from parse cache or file
        raw_line = ""
        with _cache_lock:
            cached = _parse_cache.get(uri)
        if cached:
            source = cached[1]
            lines = source.splitlines()
            if 0 <= line < len(lines):
                raw_line = lines[line]
        else:
            # Try to read from file
            try:
                path = _path_from_uri(uri)
                if os.path.isfile(path):
                    with open(path, "r", encoding="utf-8") as f:
                        lines = f.read().splitlines()
                    if 0 <= line < len(lines):
                        raw_line = lines[line]
            except Exception:
                pass

        if raw_line:
            idx = raw_line.find(name)
            if idx >= 0:
                start_char = idx
                end_char = idx + len(name)

    return types.Location(
        uri=uri,
        range=types.Range(
            start=types.Position(line=line, character=start_char),
            end=types.Position(line=line, character=end_char),
        ),
    )


def _dedup_locations(locations: List[types.Location]) -> List[types.Location]:
    """Remove duplicate locations based on (file_path, line)."""
    seen: set = set()
    result: List[types.Location] = []
    for loc in locations:
        # Normalize by converting to path for comparison
        try:
            path = _path_from_uri(loc.uri)
        except Exception:
            path = loc.uri
        key = (path, loc.range.start.line)
        if key not in seen:
            seen.add(key)
            result.append(loc)
    return result


def _publish_diagnostics_light(uri: str):
    """Fast diagnostics: only current-file syntax checks (no workspace scan).

    Called from ``didChange`` (debounced).  This covers parser errors and
    empty-ATL-block errors — the things the user wants instant feedback on.
    """
    _log.info("_publish_diagnostics_light: %s", _short_uri(uri))
    t0 = _time.monotonic()
    ast, parser = _get_parse(uri)
    # Update the workspace index for this single file so subsequent
    # queries (hover, goto-def, …) see the latest symbols.
    _workspace_index.update_file(uri)
    diags: List[types.Diagnostic] = []

    # 1) Parser-level errors (Unknown lines).
    for lineno, msg in parser.errors:
        diags.append(
            types.Diagnostic(
                range=types.Range(
                    start=types.Position(line=lineno - 1, character=0),
                    end=types.Position(line=lineno - 1, character=999),
                ),
                message=msg,
                severity=types.DiagnosticSeverity.Warning,
                source="renpy-lsp",
            )
        )

    # 2) Check for empty ATL blocks (show/scene/hide with colon but no body).
    for node in parser.get_empty_block_errors():
        stmt_type = type(node).__name__.lower()
        diags.append(
            types.Diagnostic(
                range=types.Range(
                    start=types.Position(line=node.lineno - 1, character=0),
                    end=types.Position(line=node.lineno - 1, character=999),
                ),
                message=f'"{stmt_type}" statement ends with ":" but has no indented block',
                severity=types.DiagnosticSeverity.Error,
                source="renpy-lsp",
            )
        )

    elapsed = (_time.monotonic() - t0) * 1000
    _log.info(
        "_publish_diagnostics_light: %s → %d diagnostic(s) in %.1f ms",
        _short_uri(uri),
        len(diags),
        elapsed,
    )
    LSP_SERVER.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=diags)
    )


def _publish_diagnostics(uri: str):
    """Full diagnostics: parse the document and push all diagnostics.

    This includes cross-workspace checks (undefined labels, duplicate
    definitions, unused labels, missing image files).  Called from
    ``didOpen`` and ``didSave``.
    """
    _log.info("_publish_diagnostics: %s", _short_uri(uri))
    t0 = _time.monotonic()
    ast, parser = _get_parse(uri)
    # Ensure workspace index is up-to-date for the current file
    _workspace_index.update_file(uri)
    diags: List[types.Diagnostic] = []

    # 1) Parser-level errors (Unknown lines).
    for lineno, msg in parser.errors:
        diags.append(
            types.Diagnostic(
                range=types.Range(
                    start=types.Position(line=lineno - 1, character=0),
                    end=types.Position(line=lineno - 1, character=999),
                ),
                message=msg,
                severity=types.DiagnosticSeverity.Warning,
                source="renpy-lsp",
            )
        )

    # 2) Check jump/call targets exist across the whole workspace.
    all_labels = _get_all_workspace_labels()
    # Also include labels from the current document (covers the case where
    # the file is outside the workspace folders or URI format differs).
    for lb in parser.get_all_labels():
        if lb.name not in all_labels:
            all_labels[lb.name] = [(uri, lb)]
    for j in parser.get_all_jumps():
        if not j.is_expression and j.target not in all_labels:
            diags.append(
                types.Diagnostic(
                    range=types.Range(
                        start=types.Position(line=j.lineno - 1, character=0),
                        end=types.Position(line=j.lineno - 1, character=999),
                    ),
                    message=f'Label "{j.target}" is not defined in the project',
                    severity=types.DiagnosticSeverity.Warning,
                    source="renpy-lsp",
                )
            )
    for c in parser.get_all_calls():
        if not c.is_expression and c.target not in all_labels:
            diags.append(
                types.Diagnostic(
                    range=types.Range(
                        start=types.Position(line=c.lineno - 1, character=0),
                        end=types.Position(line=c.lineno - 1, character=999),
                    ),
                    message=f'Label "{c.target}" is not defined in the project',
                    severity=types.DiagnosticSeverity.Warning,
                    source="renpy-lsp",
                )
            )

    # 3) Check for empty ATL blocks (show/scene/hide with colon but no body).
    for node in parser.get_empty_block_errors():
        stmt_type = type(node).__name__.lower()
        diags.append(
            types.Diagnostic(
                range=types.Range(
                    start=types.Position(line=node.lineno - 1, character=0),
                    end=types.Position(line=node.lineno - 1, character=999),
                ),
                message=f'"{stmt_type}" statement ends with ":" but has no indented block',
                severity=types.DiagnosticSeverity.Error,
                source="renpy-lsp",
            )
        )

    # 4) Check for duplicate definitions across the workspace.
    # Labels
    for name, locations in all_labels.items():
        if len(locations) > 1:
            for loc_uri, label in locations:
                if _same_file_uri(loc_uri, uri):
                    other_files = [
                        os.path.basename(_path_from_uri(u))
                        for u, _ in locations
                        if not _same_file_uri(u, uri) or _.lineno != label.lineno
                    ]
                    if other_files:
                        diags.append(
                            types.Diagnostic(
                                range=types.Range(
                                    start=types.Position(
                                        line=label.lineno - 1, character=0
                                    ),
                                    end=types.Position(
                                        line=label.lineno - 1, character=999
                                    ),
                                ),
                                message=f'Label "{name}" is also defined in: {", ".join(other_files)}',
                                severity=types.DiagnosticSeverity.Warning,
                                source="renpy-lsp",
                            )
                        )

    # Screens
    all_screens = _get_all_workspace_screens()
    for name, locations in all_screens.items():
        if len(locations) > 1:
            for loc_uri, screen in locations:
                if _same_file_uri(loc_uri, uri):
                    other_files = [
                        os.path.basename(_path_from_uri(u))
                        for u, _ in locations
                        if not _same_file_uri(u, uri) or _.lineno != screen.lineno
                    ]
                    if other_files:
                        diags.append(
                            types.Diagnostic(
                                range=types.Range(
                                    start=types.Position(
                                        line=screen.lineno - 1, character=0
                                    ),
                                    end=types.Position(
                                        line=screen.lineno - 1, character=999
                                    ),
                                ),
                                message=f'Screen "{name}" is also defined in: {", ".join(other_files)}',
                                severity=types.DiagnosticSeverity.Warning,
                                source="renpy-lsp",
                            )
                        )

    # 5) Check for missing image files in `image name = "path"` definitions.
    for img in parser.get_all_images():
        if img.expression:
            # Extract path from expression like '"images/bg.png"'
            file_path = _try_extract_path(img.expression)
            if file_path:
                resolved = _resolve_renpy_file(file_path, source_uri=uri)
                if not resolved:
                    diags.append(
                        types.Diagnostic(
                            range=types.Range(
                                start=types.Position(line=img.lineno - 1, character=0),
                                end=types.Position(line=img.lineno - 1, character=999),
                            ),
                            message=f'Image file not found: "{file_path}"',
                            severity=types.DiagnosticSeverity.Warning,
                            source="renpy-lsp",
                        )
                    )

    # 6) Check for unused labels (defined but never jumped/called).
    # Use the pre-indexed jump/call targets from the workspace index.
    all_used_labels = _workspace_index.get_used_labels()
    # Also include the current document's targets — covers the case where
    # the file is opened outside of a workspace folder or the workspace
    # scanner hasn't discovered it yet.
    for j in parser.get_all_jumps():
        if not j.is_expression:
            all_used_labels.add(j.target)
    for c in parser.get_all_calls():
        if not c.is_expression:
            all_used_labels.add(c.target)
    # Special labels that are entry points (never directly called)
    ENTRY_LABELS = {"start", "main_menu", "splashscreen", "after_load", "quit"}

    for lb in parser.get_all_labels():
        if lb.name not in all_used_labels and lb.name not in ENTRY_LABELS:
            # Skip translation variant labels (contain dots like "label.1")
            if "." in lb.name and lb.name.split(".")[-1].isdigit():
                continue
            diags.append(
                types.Diagnostic(
                    range=types.Range(
                        start=types.Position(line=lb.lineno - 1, character=0),
                        end=types.Position(line=lb.lineno - 1, character=999),
                    ),
                    message=f'Label "{lb.name}" is defined but never used',
                    severity=types.DiagnosticSeverity.Hint,
                    source="renpy-lsp",
                    tags=[types.DiagnosticTag.Unnecessary],
                )
            )

    elapsed = (_time.monotonic() - t0) * 1000
    _log.info(
        "_publish_diagnostics: %s → %d diagnostic(s) in %.1f ms",
        _short_uri(uri),
        len(diags),
        elapsed,
    )
    LSP_SERVER.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=diags)
    )


# ─────────────────────── Document Sync ───────────────────────────────────

# Debounce timers for didChange → lightweight diagnostics.
_DEBOUNCE_DELAY = 0.3  # seconds
_debounce_timers: Dict[str, threading.Timer] = {}

# Lock to serialise background diagnostic runs so at most one runs at a time.
_diag_lock = threading.Lock()

# Coalescing queue for full diagnostics — avoids spawning one thread per save.
_diag_queue: Dict[str, float] = {}  # uri → timestamp when queued
_diag_queue_lock = threading.Lock()
_diag_thread_running = False
_DIAG_COALESCE_DELAY = 0.15  # seconds — wait briefly to batch rapid saves


def _schedule_full_diagnostics(uri: str) -> None:
    """Queue *uri* for background index update + diagnostics.

    Multiple saves within ``_DIAG_COALESCE_DELAY`` are batched into a single
    diagnostic pass so that e.g. "Format All Files" doesn't spawn N threads.
    """
    global _diag_thread_running
    with _diag_queue_lock:
        _diag_queue[uri] = _time.monotonic()
        if _diag_thread_running:
            return  # existing thread will pick up the new entry
        _diag_thread_running = True

    def _drain():
        global _diag_thread_running
        try:
            while True:
                # Wait a short window to coalesce rapid saves
                _time.sleep(_DIAG_COALESCE_DELAY)
                with _diag_queue_lock:
                    if not _diag_queue:
                        _diag_thread_running = False
                        return
                    batch = dict(_diag_queue)
                    _diag_queue.clear()
                with _diag_lock:
                    for batch_uri in batch:
                        try:
                            _workspace_index.update_file(batch_uri)
                            _publish_diagnostics(batch_uri)
                        except Exception:
                            _log.exception(
                                "Error in background diagnostics for %s", batch_uri
                            )
        except Exception:
            _log.exception("Error in diagnostics drain thread")
        finally:
            with _diag_queue_lock:
                _diag_thread_running = False

    t = threading.Thread(target=_drain, daemon=True, name="diag-drain")
    t.start()


def _schedule_light_diagnostics(uri: str) -> None:
    """Schedule a debounced lightweight diagnostic run for *uri*."""
    # Cancel any pending timer for this URI
    old = _debounce_timers.pop(uri, None)
    if old is not None:
        old.cancel()

    def _run():
        _debounce_timers.pop(uri, None)
        try:
            _publish_diagnostics_light(uri)
        except Exception:
            _log.exception("Error in debounced light diagnostics for %s", uri)

    timer = threading.Timer(_DEBOUNCE_DELAY, _run)
    timer.daemon = True
    _debounce_timers[uri] = timer
    timer.start()


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: LanguageServer, params: types.DidOpenTextDocumentParams):
    uri = params.text_document.uri
    _log.info("didOpen: %s", _short_uri(uri))
    # Warm the cache synchronously (fast) so completions/hover work immediately.
    _get_parse(uri)
    # Kick off background index warm-up on the first file open.
    if not _workspace_index.is_ready() and not _workspace_index._warming:
        _workspace_index.warm()
    # Run index update + full diagnostics in a background thread.
    _schedule_full_diagnostics(uri)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: LanguageServer, params: types.DidChangeTextDocumentParams):
    _log.debug("didChange: %s", _short_uri(params.text_document.uri))
    # Parse immediately so the cache is warm for completions/hover, but
    # defer diagnostics behind a debounce timer.
    _get_parse(params.text_document.uri)
    _schedule_light_diagnostics(params.text_document.uri)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(ls: LanguageServer, params: types.DidSaveTextDocumentParams):
    uri = params.text_document.uri
    _log.info("didSave: %s", _short_uri(uri))
    # Cancel any pending light-diagnostics timer — we'll do a full pass now.
    old = _debounce_timers.pop(uri, None)
    if old is not None:
        old.cancel()
    # Warm the parse cache synchronously (fast).
    _get_parse(uri)
    # Index update + full diagnostics run in a background thread.
    _schedule_full_diagnostics(uri)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: LanguageServer, params: types.DidCloseTextDocumentParams):
    uri = params.text_document.uri
    _log.info("didClose: %s", _short_uri(uri))
    # Cancel pending timer
    old = _debounce_timers.pop(uri, None)
    if old is not None:
        old.cancel()
    with _cache_lock:
        _parse_cache.pop(uri, None)
    _workspace_index.remove_file(uri)
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=[])
    )


@LSP_SERVER.feature(types.WORKSPACE_DID_CHANGE_WATCHED_FILES)
def did_change_watched_files(
    ls: LanguageServer, params: types.DidChangeWatchedFilesParams
):
    """Handle workspace file create/delete/rename events.

    Invalidates the cached file list and removes deleted files from the index.
    """
    for change in params.changes:
        _log.debug("watchedFile: %s type=%s", _short_uri(change.uri), change.type)
        change_path = _path_from_uri(change.uri)
        is_rpy = change_path.endswith((".rpy", ".rpym"))
        if change.type == types.FileChangeType.Created:
            if is_rpy:
                _workspace_index.add_file(change_path)
            else:
                # Might be an image file — invalidate image cache
                _image_cache.clear()
        elif change.type == types.FileChangeType.Deleted:
            if is_rpy:
                _workspace_index.remove_file_from_list(change_path)
                _workspace_index.remove_file(change.uri)
                with _cache_lock:
                    _parse_cache.pop(change.uri, None)
            else:
                _image_cache.clear()
        elif change.type == types.FileChangeType.Changed:
            # An external change — evict the old cache entry so next access re-reads.
            with _cache_lock:
                _parse_cache.pop(change.uri, None)
            _workspace_index.remove_file(change.uri)


# ─────────────────────── Document Symbols ────────────────────────────────


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
def document_symbols(
    ls: LanguageServer, params: types.DocumentSymbolParams
) -> List[types.DocumentSymbol]:
    _log.debug("documentSymbol: %s", _short_uri(params.text_document.uri))
    ast, parser = _get_parse(params.text_document.uri)
    symbols = _build_symbols(ast.body)
    _log.debug(
        "documentSymbol: %s → %d symbol(s)",
        _short_uri(params.text_document.uri),
        len(symbols),
    )
    return symbols


def _build_symbols(nodes: List[Node]) -> List[types.DocumentSymbol]:
    symbols: List[types.DocumentSymbol] = []
    for node in nodes:
        sym = _node_to_symbol(node)
        if sym:
            symbols.append(sym)
    return symbols


def _node_to_symbol(node: Node) -> Optional[types.DocumentSymbol]:
    rng = types.Range(
        start=types.Position(line=node.lineno - 1, character=0),
        end=types.Position(line=node.end_lineno - 1, character=999),
    )
    sel = types.Range(
        start=types.Position(line=node.lineno - 1, character=0),
        end=types.Position(line=node.lineno - 1, character=999),
    )

    children: List[types.DocumentSymbol] = []

    if isinstance(node, Label):
        children = _build_symbols(node.body)
        return types.DocumentSymbol(
            name=f"label {node.name}",
            kind=types.SymbolKind.Function,
            range=rng,
            selection_range=sel,
            children=children,
        )
    elif isinstance(node, Define):
        return types.DocumentSymbol(
            name=f"define {node.name}",
            detail=node.expression,
            kind=types.SymbolKind.Variable,
            range=rng,
            selection_range=sel,
        )
    elif isinstance(node, Default):
        return types.DocumentSymbol(
            name=f"default {node.name}",
            detail=node.expression,
            kind=types.SymbolKind.Variable,
            range=rng,
            selection_range=sel,
        )
    elif isinstance(node, ScreenDef):
        children = _build_symbols(node.body)
        return types.DocumentSymbol(
            name=f"screen {node.name}",
            kind=types.SymbolKind.Class,
            range=rng,
            selection_range=sel,
            children=children,
        )
    elif isinstance(node, TransformDef):
        return types.DocumentSymbol(
            name=f"transform {node.name}",
            kind=types.SymbolKind.Function,
            range=rng,
            selection_range=sel,
        )
    elif isinstance(node, ImageDef):
        return types.DocumentSymbol(
            name=f"image {node.name}",
            kind=types.SymbolKind.Field,
            range=rng,
            selection_range=sel,
        )
    elif isinstance(node, StyleDef):
        return types.DocumentSymbol(
            name=f"style {node.name}",
            detail=f"is {node.parent}" if node.parent else None,
            kind=types.SymbolKind.Property,
            range=rng,
            selection_range=sel,
        )
    elif isinstance(node, Init):
        children = _build_symbols(node.body)
        prio = node.priority if node.priority is not None else ""
        py = " python" if node.is_python else ""
        return types.DocumentSymbol(
            name=f"init {prio}{py}".strip(),
            kind=types.SymbolKind.Module,
            range=rng,
            selection_range=sel,
            children=children,
        )
    elif isinstance(node, Menu):
        children = _build_symbols(node.body)
        name = f"menu {node.name}" if node.name else "menu"
        return types.DocumentSymbol(
            name=name,
            kind=types.SymbolKind.Enum,
            range=rng,
            selection_range=sel,
            children=children,
        )
    elif isinstance(node, MenuItem):
        children = _build_symbols(node.body)
        return types.DocumentSymbol(
            name=f'"{node.caption}"',
            kind=types.SymbolKind.EnumMember,
            range=rng,
            selection_range=sel,
            children=children,
        )
    elif isinstance(node, Translate):
        children = _build_symbols(node.body)
        return types.DocumentSymbol(
            name=f"translate {node.language} {node.identifier}",
            kind=types.SymbolKind.Namespace,
            range=rng,
            selection_range=sel,
            children=children,
        )
    elif isinstance(node, If):
        children = _build_symbols(node.body)
        return types.DocumentSymbol(
            name=f"if {node.condition}",
            kind=types.SymbolKind.Struct,
            range=rng,
            selection_range=sel,
            children=children,
        )
    return None


# ─────────────────────── Folding Ranges ──────────────────────────────────


@LSP_SERVER.feature(types.TEXT_DOCUMENT_FOLDING_RANGE)
def folding_ranges(
    ls: LanguageServer, params: types.FoldingRangeParams
) -> List[types.FoldingRange]:
    """Return folding ranges for block-level constructs."""
    _log.debug("foldingRange: %s", _short_uri(params.text_document.uri))
    ast, parser = _get_parse(params.text_document.uri)
    ranges: List[types.FoldingRange] = []
    _collect_folding_ranges(ast, ranges)
    _log.debug(
        "foldingRange: %s → %d range(s)",
        _short_uri(params.text_document.uri),
        len(ranges),
    )
    return ranges


def _collect_folding_ranges(node: Node, ranges: List[types.FoldingRange]):
    """Recursively collect folding ranges from the AST."""
    # Only create a fold if the node spans multiple lines and has valid line numbers
    # (Script root node has lineno=0 which is invalid)
    if node.lineno > 0 and node.end_lineno > node.lineno:
        # Determine fold kind
        kind = types.FoldingRangeKind.Region
        if isinstance(node, Comment):
            kind = types.FoldingRangeKind.Comment

        ranges.append(
            types.FoldingRange(
                start_line=node.lineno - 1,  # 0-based
                end_line=node.end_lineno - 1,
                kind=kind,
            )
        )

    # Recurse into children
    if hasattr(node, "body") and isinstance(node.body, list):
        for child in node.body:
            _collect_folding_ranges(child, ranges)

    # Handle If's elif_clauses and else_body
    if isinstance(node, If):
        for ec in node.elif_clauses:
            _collect_folding_ranges(ec, ranges)
        for child in node.else_body:
            _collect_folding_ranges(child, ranges)


# ─────────────────────── Go to Definition ────────────────────────────────


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DEFINITION)
def goto_definition(
    ls: LanguageServer, params: types.DefinitionParams
) -> Optional[List[types.Location]]:
    uri = params.text_document.uri
    doc = ls.workspace.get_text_document(uri)
    pos = params.position
    line_text = doc.lines[pos.line] if pos.line < len(doc.lines) else ""
    col = _utf16_col_to_utf32(line_text, pos.character)
    word = _word_at_position(line_text, col)
    _log.info(
        "gotoDefinition: %s L%d C%d word=%r",
        _short_uri(uri),
        pos.line + 1,
        pos.character,
        word,
    )
    ast, parser = _get_parse(uri)

    # ── 0) If cursor is on a label/screen definition, show all usages ──
    lineno = pos.line + 1  # 1-based
    nodes = _find_nodes_at_line(parser, lineno)
    for node in nodes:
        # Label definition → show all jump/call usages
        if isinstance(node, Label):
            results: List[types.Location] = []
            seen: set = set()  # (uri, lineno) to dedupe
            label_name = node.name
            for fp in _get_workspace_rpy_files():
                file_uri, file_ast, file_parser = _get_parse_for_file(fp)
                for j in file_parser.get_all_jumps():
                    if j.target == label_name:
                        key = (file_uri, j.lineno)
                        if key not in seen:
                            seen.add(key)
                            results.append(_make_node_location(file_uri, j))
                for c in file_parser.get_all_calls():
                    if c.target == label_name:
                        key = (file_uri, c.lineno)
                        if key not in seen:
                            seen.add(key)
                            results.append(_make_node_location(file_uri, c))
            # Always return here — usages if found, else None.
            # Never fall through to the general label lookup (step 4)
            # which would return a self-referential definition and
            # cause VS Code to show a source-code preview in hover.
            return results if results else None

        # Screen definition → show all call screen/show screen usages
        if isinstance(node, ScreenDef):
            results = []
            seen = set()
            screen_name = node.name
            for fp in _get_workspace_rpy_files():
                file_uri, file_ast, file_parser = _get_parse_for_file(fp)
                for n in file_parser._collect(file_ast, CallScreen):
                    if n.screen_name == screen_name:
                        key = (file_uri, n.lineno)
                        if key not in seen:
                            seen.add(key)
                            results.append(_make_node_location(file_uri, n))
                for n in file_parser._collect(file_ast, ShowScreen):
                    if n.screen_name == screen_name:
                        key = (file_uri, n.lineno)
                        if key not in seen:
                            seen.add(key)
                            results.append(_make_node_location(file_uri, n))
            return results if results else None

    # ── 1) AST-based resolution: find node at cursor line ──
    for node in nodes:
        loc = _resolve_node_definition(
            node, source_uri=uri, line_text=line_text, col=col
        )
        if loc:
            return loc if isinstance(loc, list) else [loc]

    # ── 2) Quoted string → file path ──
    quoted = _extract_quoted_string(line_text, col)
    if quoted:
        resolved = _resolve_renpy_file(quoted, source_uri=uri)
        if resolved:
            return [_make_file_location(resolved)]

    if not word:
        return None

    # ── 3) Jump / Call → label ──
    stripped = line_text.strip()
    m = re.match(
        r"^(?:jump|call)\s+(?:expression\s+)?([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf][\w.]*)",
        stripped,
    )
    if m:
        word = m.group(1)

    # ── 4) Symbol lookup across workspace ──

    # Labels
    all_labels = _get_all_workspace_labels()
    if word in all_labels:
        return _dedup_locations(
            [_make_node_location(u, lb) for u, lb in all_labels[word]]
        )

    # Defines / Defaults
    all_defines = _get_all_workspace_defines()
    if word in all_defines:
        return _dedup_locations(
            [_make_node_location(u, d) for u, d in all_defines[word]]
        )
    all_defaults = _get_all_workspace_defaults()
    if word in all_defaults:
        return _dedup_locations(
            [_make_node_location(u, d) for u, d in all_defaults[word]]
        )

    # Screens
    all_screens = _get_all_workspace_screens()
    if word in all_screens:
        return _dedup_locations(
            [_make_node_location(u, s) for u, s in all_screens[word]]
        )

    # Images
    all_images = _get_all_workspace_images()
    if word in all_images:
        return _dedup_locations(
            [_make_node_location(u, img) for u, img in all_images[word]]
        )

    # Transforms
    all_transforms = _get_all_workspace_transforms()
    if word in all_transforms:
        return _dedup_locations(
            [_make_node_location(u, t) for u, t in all_transforms[word]]
        )

    # Python variables (defined in python: blocks or $ one-liners)
    py_vars = _find_python_var_across_workspace(word)
    if py_vars:
        return [
            types.Location(
                uri=u,
                range=types.Range(
                    start=types.Position(line=ln - 1, character=0),
                    end=types.Position(line=ln - 1, character=999),
                ),
            )
            for u, ln, _code in py_vars
        ]

    return None


def _resolve_node_definition(
    node: Node,
    source_uri: Optional[str] = None,
    line_text: Optional[str] = None,
    col: Optional[int] = None,
) -> Optional[Union[types.Location, List[types.Location]]]:
    """Given a parsed AST node, try to resolve its Go-to-Definition target.

    Returns a Location (for a file), a list of Locations (for definitions),
    or None if no target can be resolved.
    """
    # ── Scene / Show / Hide → image definition or image file ──
    if isinstance(node, (Scene, Show, Hide)):
        image_name = node.image.strip()
        if not image_name:
            return None
        # Only navigate when the cursor is actually on the image-name portion.
        if line_text is not None and col is not None:
            if not _cursor_on_image_name(line_text, col, image_name):
                return None
        # 1) Look for an explicit ``image`` definition
        all_images = _get_all_workspace_images()
        if image_name in all_images:
            locs = [_make_node_location(u, img) for u, img in all_images[image_name]]
            # If the image definition has a file expression, also add that
            for target_uri, img in all_images[image_name]:
                if img.expression:
                    file_path = _try_extract_path(img.expression)
                    if file_path:
                        resolved = _resolve_renpy_file(file_path, source_uri=source_uri)
                        if resolved:
                            locs.append(_make_file_location(resolved))
            return locs
        # 2) Also try matching by the image tag (first word)
        tag = image_name.split()[0] if " " in image_name else image_name
        tag_matches = [
            (u, img)
            for name, entries in all_images.items()
            for u, img in entries
            if name == tag or name.startswith(tag + " ")
        ]
        if tag_matches:
            return [_make_node_location(u, img) for u, img in tag_matches]
        # 3) Try to find a matching file via Ren'Py's auto-image detection
        resolved = _resolve_image_name_to_file(image_name)
        if resolved:
            return _make_file_location(resolved)
        return None

    # ── Call Screen / Show Screen → screen definition ──
    if isinstance(node, (CallScreen, ShowScreen)):
        all_screens = _get_all_workspace_screens()
        sname = node.screen_name.strip()
        if sname in all_screens:
            return [_make_node_location(u, s) for u, s in all_screens[sname]]
        return None

    # ── Voice → voice file ──
    if isinstance(node, Voice):
        resolved = _resolve_renpy_file(node.filename, source_uri=source_uri)
        if resolved:
            return _make_file_location(resolved)
        return None

    # ── Play / Queue → audio file or audio define ──
    if isinstance(node, PlayMusic):
        filename = node.filename.strip()
        if filename:
            resolved = _resolve_renpy_file(filename, source_uri=source_uri)
            if resolved:
                return _make_file_location(resolved)
        # filename might be a define name (e.g.  play music OldTime)
        all_defines = _get_all_workspace_defines()
        # Try the raw text after channel as a define name
        if filename in all_defines:
            return [_make_node_location(u, d) for u, d in all_defines[filename]]
        # Also try with "audio." prefix (common Ren'Py convention)
        audio_prefixed = f"audio.{filename}"
        if audio_prefixed in all_defines:
            return [_make_node_location(u, d) for u, d in all_defines[audio_prefixed]]
        return None

    if isinstance(node, QueueMusic):
        filename = node.filename.strip()
        if filename:
            resolved = _resolve_renpy_file(filename, source_uri=source_uri)
            if resolved:
                return _make_file_location(resolved)
        # filename might be a define name
        all_defines = _get_all_workspace_defines()
        if filename in all_defines:
            return [_make_node_location(u, d) for u, d in all_defines[filename]]
        # Also try with "audio." prefix
        audio_prefixed = f"audio.{filename}"
        if audio_prefixed in all_defines:
            return [_make_node_location(u, d) for u, d in all_defines[audio_prefixed]]
        return None

    # ── ImageDef with expression → try to open the image file ──
    if isinstance(node, ImageDef) and node.expression:
        file_path = _try_extract_path(node.expression)
        if file_path:
            resolved = _resolve_renpy_file(file_path, source_uri=source_uri)
            if resolved:
                return _make_file_location(resolved)
        return None

    return None


def _try_extract_path(expression: str) -> Optional[str]:
    """Try to extract a file path from an image expression like ``"path/to/img.png"``."""
    m = re.match(r"""^["'](.+?)["']$""", expression.strip())
    if m:
        return m.group(1)
    return None


def _word_at_position(line: str, col: int) -> str:
    """Extract the word under the cursor.

    Supports alphanumerics, underscores, dots, and CJK characters, plus
    hyphens (common in Ren'Py image names like ``日内-彩票站屏幕``).
    """
    if col >= len(line):
        col = max(0, len(line) - 1)
    if not line:
        return ""

    def _is_word_char(ch: str) -> bool:
        return (
            ch.isalnum()
            or ch in "_."
            or ch == "-"
            or "\u4e00" <= ch <= "\u9fff"
            or "\u3400" <= ch <= "\u4dbf"
        )

    start = col
    while start > 0 and _is_word_char(line[start - 1]):
        start -= 1
    end = col
    while end < len(line) and _is_word_char(line[end]):
        end += 1
    return line[start:end]


# ─────────────────────── Completion ──────────────────────────────────────

# Ren'Py keyword/transition/transform lists are in renpy_data.py:
#   RENPY_KEYWORDS, RENPY_TRANSITIONS, RENPY_TRANSFORMS


@LSP_SERVER.feature(
    types.TEXT_DOCUMENT_COMPLETION,
    types.CompletionOptions(trigger_characters=[" ", "."]),
)
def completions(
    ls: LanguageServer, params: types.CompletionParams
) -> types.CompletionList:
    uri = params.text_document.uri
    doc = ls.workspace.get_text_document(uri)
    pos = params.position
    line_text = doc.lines[pos.line] if pos.line < len(doc.lines) else ""
    col = _utf16_col_to_utf32(line_text, pos.character)
    line_prefix = line_text[:col].strip()
    _log.debug(
        "completion: %s L%d prefix=%r", _short_uri(uri), pos.line + 1, line_prefix[-30:]
    )

    items: List[types.CompletionItem] = []

    ast, parser = _get_parse(uri)

    # Context-aware completion
    if line_prefix.endswith(("jump ", "call ")):
        # Complete label names from the whole workspace
        all_labels = _get_all_workspace_labels()
        for name, entries in all_labels.items():
            target_uri, lb = entries[0]
            # Show which file it's from
            fname = os.path.basename(_path_from_uri(target_uri))
            items.append(
                types.CompletionItem(
                    label=name,
                    kind=types.CompletionItemKind.Function,
                    detail=f"label ({fname}:{lb.lineno})",
                )
            )
    elif line_prefix.endswith("with "):
        # Complete transitions
        for t in RENPY_TRANSITIONS:
            items.append(
                types.CompletionItem(
                    label=t,
                    kind=types.CompletionItemKind.Constant,
                    detail="transition",
                )
            )
    elif line_prefix.endswith("at "):
        # Complete built-in transforms
        for t in RENPY_TRANSFORMS:
            items.append(
                types.CompletionItem(
                    label=t,
                    kind=types.CompletionItemKind.Constant,
                    detail="transform position",
                )
            )
        # User-defined transforms from entire workspace
        all_transforms = _get_all_workspace_transforms()
        for tname, entries in all_transforms.items():
            t_uri, tdef = entries[0]
            fname = os.path.basename(_path_from_uri(t_uri))
            items.append(
                types.CompletionItem(
                    label=tname,
                    kind=types.CompletionItemKind.Function,
                    detail=f"transform ({fname}:{tdef.lineno})",
                )
            )
    elif line_prefix.endswith(("show screen ", "hide screen ", "call screen ")):
        # Complete screen names from workspace
        all_screens = _get_all_workspace_screens()
        for sname, entries in all_screens.items():
            s_uri, sdef = entries[0]
            fname = os.path.basename(_path_from_uri(s_uri))
            items.append(
                types.CompletionItem(
                    label=sname,
                    kind=types.CompletionItemKind.Class,
                    detail=f"screen ({fname}:{sdef.lineno})",
                )
            )
    elif re.match(r"^\s*(?:show|scene|hide)\s+$", line_prefix, re.IGNORECASE):
        # Complete image names after show/scene/hide
        # 1) image definitions from .rpy files
        all_images = _get_all_workspace_images()
        for img_name, entries in all_images.items():
            img_uri, img_def = entries[0]
            fname = os.path.basename(_path_from_uri(img_uri))
            items.append(
                types.CompletionItem(
                    label=img_name,
                    kind=types.CompletionItemKind.File,
                    detail=f"image ({fname}:{img_def.lineno})",
                )
            )
        # 2) auto-detected image files from images/ directory
        _ensure_image_cache()
        seen = {n.lower() for n in all_images}
        for auto_name in sorted(_image_cache):
            if auto_name not in seen:
                items.append(
                    types.CompletionItem(
                        label=auto_name,
                        kind=types.CompletionItemKind.File,
                        detail="image (auto)",
                    )
                )
                seen.add(auto_name)
    elif re.match(r"^\s*(?:play|queue)\s+\w+\s+$", line_prefix, re.IGNORECASE):
        # Complete audio names after "play music ", "play sound ", "queue music ", etc.
        # Suggest define names with "audio." prefix (Ren'Py convention: define audio.xxx = "file.ogg")
        all_defines = _get_all_workspace_defines()
        for dname, entries in all_defines.items():
            if dname.startswith("audio."):
                short = dname[len("audio.") :]
                d_uri, ddef = entries[0]
                fname = os.path.basename(_path_from_uri(d_uri))
                items.append(
                    types.CompletionItem(
                        label=short,
                        kind=types.CompletionItemKind.Variable,
                        detail=f"{dname} ({fname}:{ddef.lineno})",
                    )
                )
    else:
        # General: keywords + characters + labels
        for kw in RENPY_KEYWORDS:
            items.append(
                types.CompletionItem(
                    label=kw,
                    kind=types.CompletionItemKind.Keyword,
                )
            )
        for ch in parser.get_all_characters():
            items.append(
                types.CompletionItem(
                    label=ch.name,
                    kind=types.CompletionItemKind.Variable,
                    detail=ch.expression,
                )
            )
        for lb in parser.get_all_labels():
            items.append(
                types.CompletionItem(
                    label=lb.name,
                    kind=types.CompletionItemKind.Function,
                    detail=f"label (line {lb.lineno})",
                )
            )

    _log.debug("completion: %s → %d item(s)", _short_uri(uri), len(items))
    return types.CompletionList(is_incomplete=False, items=items)


# ─────────────────────── Hover ───────────────────────────────────────────

# Keyword documentation lives in renpy_data.KEYWORD_DOCS.


@LSP_SERVER.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: LanguageServer, params: types.HoverParams) -> Optional[types.Hover]:
    uri = params.text_document.uri
    doc = ls.workspace.get_text_document(uri)
    line_text = (
        doc.lines[params.position.line] if params.position.line < len(doc.lines) else ""
    )
    col = _utf16_col_to_utf32(line_text, params.position.character)
    word = _word_at_position(line_text, col)
    _log.debug("hover: %s L%d word=%r", _short_uri(uri), params.position.line + 1, word)
    if not word:
        return None

    # 1) Check keyword docs
    if word in KEYWORD_DOCS:
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown,
                value=KEYWORD_DOCS[word],
            )
        )

    ast, parser = _get_parse(uri)

    # 2) Check labels across workspace
    all_labels = _get_all_workspace_labels()
    if word in all_labels:
        target_uri, lb = all_labels[word][0]
        fname = os.path.basename(_path_from_uri(target_uri))
        parts = [f"**label** `{lb.name}`"]
        if lb.parameters:
            parts.append(f"Parameters: `{lb.parameters}`")
        # Extract leading comment block from label body as description
        if hasattr(lb, "body") and lb.body:
            comment_lines: List[str] = []
            for child in lb.body:
                if isinstance(child, Comment) and child.text:
                    comment_lines.append(child.text)
                else:
                    break
            if comment_lines:
                parts.append("  \n".join(comment_lines))
        parts.append(f"`{fname}` line {lb.lineno}")
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown, value="\n\n".join(parts)
            )
        )

    # 3) Check defines (characters, etc.) across workspace
    all_defines = _get_all_workspace_defines()
    if word in all_defines:
        target_uri, d = all_defines[word][0]
        fname = os.path.basename(_path_from_uri(target_uri))
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown,
                value=f"**define** `{d.name}` = `{d.expression}`\n\n`{fname}` line {d.lineno}",
            )
        )

    # 4) Check defaults across workspace
    all_defaults = _get_all_workspace_defaults()
    if word in all_defaults:
        target_uri, d = all_defaults[word][0]
        fname = os.path.basename(_path_from_uri(target_uri))
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown,
                value=f"**default** `{d.name}` = `{d.expression}`\n\n`{fname}` line {d.lineno}",
            )
        )

    # 5) Check screens across workspace
    all_screens = _get_all_workspace_screens()
    if word in all_screens:
        target_uri, s = all_screens[word][0]
        fname = os.path.basename(_path_from_uri(target_uri))
        params_str = f"({s.parameters})" if s.parameters else "()"
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown,
                value=f"**screen** `{s.name}{params_str}`\n\n`{fname}` lines {s.lineno}–{s.end_lineno}",
            )
        )

    # 6) Check Python variables (defined in python: blocks or $ one-liners)
    py_vars = _find_python_var_across_workspace(word)
    if py_vars:
        uri_v, lineno_v, code_v = py_vars[0]
        fname = os.path.basename(_path_from_uri(uri_v))
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown,
                value=f"**python variable** `{word}`\n\n```python\n{code_v}\n```\n\n`{fname}` line {lineno_v}",
            )
        )

    # 7) Check if the line is a Say/NarratorSay — show translation ID on hover
    say_hit = _find_say_at_line(uri, params.position.line)
    if say_hit is not None:
        node, label_name = say_hit
        who = node.who if isinstance(node, Say) else None
        tid = _renpy_translate_id(label_name, who, node.what)
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown,
                value=f"**Translation ID**: `{tid}`",
            )
        )

    return None


# ─────────────────────── Translation ID helpers ─────────────────────────


def _say_get_code(who: Optional[str], what: str) -> str:
    """Reproduce Ren'Py ``Say.get_code()`` for simple say statements.

    Our parser's ``Say.what`` is captured verbatim from the source between
    quotes (escapes preserved), which matches the output of Ren'Py's
    ``encode_say_string(parsed_what)``. So we just wrap it in quotes.
    """
    parts: List[str] = []
    if who:
        parts.append(who)
    parts.append('"' + what + '"')
    return " ".join(parts)


def _renpy_translate_id(label: Optional[str], who: Optional[str], what: str) -> str:
    """Compute a Ren'Py-compatible translation identifier.

    Algorithm (from ``renpy/translation/__init__.py`` — ``Restructurer``):
      1. ``code = Say.get_code()``
      2. ``md5.update((code + '\\r\\n').encode('utf-8'))``
      3. ``digest = md5.hexdigest()[:8]``
      4. With label: ``label.replace('.', '_') + '_' + digest``
         Without label: just ``digest``
    """
    code = _say_get_code(who, what)
    md5 = hashlib.md5()
    md5.update((code + "\r\n").encode("utf-8"))
    digest = md5.hexdigest()[:8]
    if label is None:
        return digest
    return label.replace(".", "_") + "_" + digest


def _collect_dialogue_with_labels(
    nodes: List[Node], current_label: Optional[str] = None
) -> Generator[Tuple[Union[Say, NarratorSay], Optional[str]], None, None]:
    """Walk *nodes* recursively, yielding ``(say_node, enclosing_label_name)``."""
    for node in nodes:
        if isinstance(node, Label):
            current_label = node.name
        if isinstance(node, (Say, NarratorSay)):
            yield node, current_label
        # Recurse into children (body, elif, else, menu items, …)
        children: List[Node] = []
        if hasattr(node, "body") and isinstance(node.body, list):
            children.extend(node.body)
        if isinstance(node, If):
            for ec in node.elif_clauses:
                children.append(ec)
                children.extend(ec.body)
            children.extend(node.else_body)
        if isinstance(node, Menu):
            children.extend(node.body)
        if children:
            yield from _collect_dialogue_with_labels(children, current_label)


def _find_say_at_line(
    uri: str, line: int
) -> Optional[Tuple[Union[Say, NarratorSay], Optional[str]]]:
    """Return the Say/NarratorSay node at *line* (0-based) and its enclosing label, if any."""
    ast, parser = _get_parse(uri)
    if ast is None:
        return None
    for node, label_name in _collect_dialogue_with_labels(ast.body):
        if node.lineno - 1 == line:
            return node, label_name
    return None


# ─────────────────────── Formatting (indentation-normalizer) ─────────────


def _detect_indent_unit(lines: List[str]) -> int:
    """Detect the smallest non-zero indentation width used in the file."""
    smallest = None
    for line in lines:
        stripped = line.lstrip()
        if not stripped:
            continue
        leading = len(line) - len(stripped)
        if leading > 0:
            if smallest is None or leading < smallest:
                smallest = leading
    return smallest if smallest else 4  # default 4


def _leading_spaces(line: str) -> int:
    """Count leading space-equivalents (tabs count as 4)."""
    n = 0
    for ch in line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            n += 4
        else:
            break
    return n


# Pattern that matches a say-statement line (after stripping indent):
#   character_name  <spaces>  "dialog..."
# Captures: (character_name)(whitespace)(rest starting with quote)
_SAY_SPACE_RE = re.compile(
    r"^((?:character\.)?\w+)"  # character name (ASCII or Unicode \w)
    r"([ \t]+)"  # whitespace between name and dialog
    r'(r?(?:"|\'|`).*)',  # the dialog string
    re.UNICODE,
)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_FORMATTING)
def format_document(ls: LanguageServer, params: types.DocumentFormattingParams):
    _log.info(
        "formatting: %s (tabSize=%d)",
        _short_uri(params.text_document.uri),
        params.options.tab_size,
    )
    """Re-indent the document using a consistent indent width.

    Strategy:
      1. Detect the file's current indent unit (e.g. 4 spaces).
      2. For every line compute *indent level* = leading_spaces / unit.
      3. Re-emit each line at ``target_indent * level``.
      4. Collapse consecutive blank lines into one.
      5. Strip trailing whitespace.
    """
    document = ls.workspace.get_text_document(params.text_document.uri)
    source = document.source
    tab_size: int = params.options.tab_size
    use_spaces: bool = params.options.insert_spaces
    target_indent = " " * tab_size if use_spaces else "\t"

    raw_lines = source.splitlines()
    src_unit = _detect_indent_unit(raw_lines)

    formatted: List[str] = []
    prev_blank = False

    for raw in raw_lines:
        stripped = raw.strip()

        # ── blank lines: keep at most one ──
        if not stripped:
            if not prev_blank and formatted:
                formatted.append("")
            prev_blank = True
            continue
        prev_blank = False

        # ── compute indent level from source ──
        spaces = _leading_spaces(raw)
        level = round(spaces / src_unit) if src_unit else 0

        # ── emit with normalized indent ──
        # Normalize: exactly 1 space between character name and dialog string
        m = _SAY_SPACE_RE.match(stripped)
        if m:
            stripped = m.group(1) + " " + m.group(3)
        formatted.append(target_indent * level + stripped)

    # Trailing newline
    formatted_text = "\n".join(formatted)
    if formatted_text and not formatted_text.endswith("\n"):
        formatted_text += "\n"

    return [
        types.TextEdit(
            range=types.Range(
                start=types.Position(line=0, character=0),
                end=types.Position(line=len(raw_lines), character=0),
            ),
            new_text=formatted_text,
        )
    ]


# ─────────────────────── Find All References ─────────────────────────────


@LSP_SERVER.feature(types.TEXT_DOCUMENT_REFERENCES)
def find_references(
    ls: LanguageServer, params: types.ReferenceParams
) -> Optional[List[types.Location]]:
    """Find all references to labale/screen/define/default at cursor."""
    uri = params.text_document.uri
    doc = ls.workspace.get_text_document(uri)
    pos = params.position
    line_text = doc.lines[pos.line] if pos.line < len(doc.lines) else ""
    col = _utf16_col_to_utf32(line_text, pos.character)
    word = _word_at_position(line_text, col)
    _log.info("references: %s L%d word=%r", _short_uri(uri), pos.line + 1, word)
    if not word:
        return None

    results: List[types.Location] = []

    # Check if it's a label
    all_labels = _get_all_workspace_labels()
    if word in all_labels:
        # Include the definition(s) if requested
        if params.context.include_declaration:
            for target_uri, lb in all_labels[word]:
                results.append(_make_node_location(target_uri, lb))
        # Find all jump/call references
        for fp in _get_workspace_rpy_files():
            file_uri, ast, parser = _get_parse_for_file(fp)
            for j in parser.get_all_jumps():
                if j.target == word:
                    results.append(_make_node_location(file_uri, j))
            for c in parser.get_all_calls():
                if c.target == word:
                    results.append(_make_node_location(file_uri, c))
        return results if results else None

    # Check if it's a screen
    all_screens = _get_all_workspace_screens()
    if word in all_screens:
        if params.context.include_declaration:
            for target_uri, s in all_screens[word]:
                results.append(_make_node_location(target_uri, s))
        # Find call screen / show screen references
        for fp in _get_workspace_rpy_files():
            file_uri, ast, parser = _get_parse_for_file(fp)
            for node in parser._collect(ast, CallScreen):
                if node.screen_name == word:
                    results.append(_make_node_location(file_uri, node))
            for node in parser._collect(ast, ShowScreen):
                if node.screen_name == word:
                    results.append(_make_node_location(file_uri, node))
        return results if results else None

    # Check defines/defaults
    all_defines = _get_all_workspace_defines()
    all_defaults = _get_all_workspace_defaults()
    if word in all_defines or word in all_defaults:
        if params.context.include_declaration:
            if word in all_defines:
                for target_uri, d in all_defines[word]:
                    results.append(_make_node_location(target_uri, d))
            if word in all_defaults:
                for target_uri, d in all_defaults[word]:
                    results.append(_make_node_location(target_uri, d))
        # Text search for usages (simple grep)
        for fp in _get_workspace_rpy_files():
            file_uri = _uri_from_path(fp)
            try:
                lines = (
                    Path(fp).read_text(encoding="utf-8", errors="replace").splitlines()
                )
            except OSError:
                continue
            for i, line in enumerate(lines):
                # Skip define/default lines (definitions)
                stripped = line.strip()
                if stripped.startswith("define ") or stripped.startswith("default "):
                    continue
                # Check if word appears as identifier
                if re.search(rf"\b{re.escape(word)}\b", line):
                    results.append(
                        types.Location(
                            uri=file_uri,
                            range=types.Range(
                                start=types.Position(line=i, character=0),
                                end=types.Position(line=i, character=len(line)),
                            ),
                        )
                    )
        return results if results else None

    return None


# ─────────────────────── Color Preview ───────────────────────────────────

_RE_HEX_COLOR = re.compile(r'["\']#([0-9a-fA-F]{3,8})["\']')
_RE_RGB_COLOR = re.compile(
    r"Color\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*(\d+))?\s*\)", re.IGNORECASE
)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DOCUMENT_COLOR)
def document_color(
    ls: LanguageServer, params: types.DocumentColorParams
) -> List[types.ColorInformation]:
    """Return color information for hex color strings in the document."""
    _log.debug("documentColor: %s", _short_uri(params.text_document.uri))
    doc = ls.workspace.get_text_document(params.text_document.uri)
    colors: List[types.ColorInformation] = []

    for lineno, line in enumerate(doc.lines):
        # Match hex colors like "#c8ffc8" or "#fff"
        for m in _RE_HEX_COLOR.finditer(line):
            hex_str = m.group(1)
            color = _hex_to_color(hex_str)
            if color:
                start_char = m.start()
                end_char = m.end()
                colors.append(
                    types.ColorInformation(
                        range=types.Range(
                            start=types.Position(line=lineno, character=start_char),
                            end=types.Position(line=lineno, character=end_char),
                        ),
                        color=color,
                    )
                )

        # Match Color(r, g, b) or Color(r, g, b, a)
        for m in _RE_RGB_COLOR.finditer(line):
            r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
            a = int(m.group(4)) if m.group(4) else 255
            colors.append(
                types.ColorInformation(
                    range=types.Range(
                        start=types.Position(line=lineno, character=m.start()),
                        end=types.Position(line=lineno, character=m.end()),
                    ),
                    color=types.Color(
                        red=r / 255.0,
                        green=g / 255.0,
                        blue=b / 255.0,
                        alpha=a / 255.0,
                    ),
                )
            )

    return colors


def _hex_to_color(hex_str: str) -> Optional[types.Color]:
    """Convert hex string to Color. Supports 3, 4, 6, 8 char formats."""
    length = len(hex_str)
    try:
        if length == 3:  # RGB
            r = int(hex_str[0] * 2, 16) / 255.0
            g = int(hex_str[1] * 2, 16) / 255.0
            b = int(hex_str[2] * 2, 16) / 255.0
            return types.Color(red=r, green=g, blue=b, alpha=1.0)
        elif length == 4:  # RGBA
            r = int(hex_str[0] * 2, 16) / 255.0
            g = int(hex_str[1] * 2, 16) / 255.0
            b = int(hex_str[2] * 2, 16) / 255.0
            a = int(hex_str[3] * 2, 16) / 255.0
            return types.Color(red=r, green=g, blue=b, alpha=a)
        elif length == 6:  # RRGGBB
            r = int(hex_str[0:2], 16) / 255.0
            g = int(hex_str[2:4], 16) / 255.0
            b = int(hex_str[4:6], 16) / 255.0
            return types.Color(red=r, green=g, blue=b, alpha=1.0)
        elif length == 8:  # RRGGBBAA
            r = int(hex_str[0:2], 16) / 255.0
            g = int(hex_str[2:4], 16) / 255.0
            b = int(hex_str[4:6], 16) / 255.0
            a = int(hex_str[6:8], 16) / 255.0
            return types.Color(red=r, green=g, blue=b, alpha=a)
    except ValueError:
        pass
    return None


@LSP_SERVER.feature(types.TEXT_DOCUMENT_COLOR_PRESENTATION)
def color_presentation(
    ls: LanguageServer, params: types.ColorPresentationParams
) -> List[types.ColorPresentation]:
    """Return color presentation options when user picks a color."""
    color = params.color
    r = int(color.red * 255)
    g = int(color.green * 255)
    b = int(color.blue * 255)
    a = int(color.alpha * 255)

    presentations: List[types.ColorPresentation] = []

    # Hex format without alpha
    hex_rgb = f'"#{r:02x}{g:02x}{b:02x}"'
    presentations.append(types.ColorPresentation(label=hex_rgb))

    # Hex format with alpha (if not fully opaque)
    if a < 255:
        hex_rgba = f'"#{r:02x}{g:02x}{b:02x}{a:02x}"'
        presentations.append(types.ColorPresentation(label=hex_rgba))

    return presentations


# ─────────────────────── Rename Support ──────────────────────────────────


@LSP_SERVER.feature(types.TEXT_DOCUMENT_PREPARE_RENAME)
def prepare_rename(
    ls: LanguageServer, params: types.PrepareRenameParams
) -> Optional[types.Range]:
    """Check if rename is allowed at the cursor position."""
    uri = params.text_document.uri
    doc = ls.workspace.get_text_document(uri)
    pos = params.position
    line_text = doc.lines[pos.line] if pos.line < len(doc.lines) else ""
    col = _utf16_col_to_utf32(line_text, pos.character)
    word = _word_at_position(line_text, col)
    _log.info("prepareRename: %s L%d word=%r", _short_uri(uri), pos.line + 1, word)
    if not word:
        return None

    # Only allow renaming labels and screens
    all_labels = _get_all_workspace_labels()
    all_screens = _get_all_workspace_screens()

    if word in all_labels or word in all_screens:
        # Find the word boundaries
        start, end = _word_boundaries(line_text, col)
        return types.Range(
            start=types.Position(line=pos.line, character=start),
            end=types.Position(line=pos.line, character=end),
        )

    return None


def _word_boundaries(line: str, col: int) -> Tuple[int, int]:
    """Return (start, end) character positions of the word at col."""
    if col >= len(line):
        col = len(line) - 1
    if col < 0:
        return (0, 0)

    # Find start
    start = col
    while start > 0 and (line[start - 1].isalnum() or line[start - 1] in "_"):
        start -= 1

    # Find end
    end = col
    while end < len(line) and (line[end].isalnum() or line[end] in "_"):
        end += 1

    return (start, end)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_RENAME)
def rename(
    ls: LanguageServer, params: types.RenameParams
) -> Optional[types.WorkspaceEdit]:
    """Rename a label or screen across all workspace files."""
    uri = params.text_document.uri
    doc = ls.workspace.get_text_document(uri)
    pos = params.position
    line_text = doc.lines[pos.line] if pos.line < len(doc.lines) else ""
    col = _utf16_col_to_utf32(line_text, pos.character)
    old_name = _word_at_position(line_text, col)
    new_name = params.new_name
    _log.info("rename: %s %r → %r", _short_uri(uri), old_name, new_name)

    if not old_name or not new_name:
        return None

    changes: Dict[str, List[types.TextEdit]] = {}

    # Rename labels
    all_labels = _get_all_workspace_labels()
    if old_name in all_labels:
        # Rename label definitions
        for target_uri, lb in all_labels[old_name]:
            if target_uri not in changes:
                changes[target_uri] = []
            # Find the label name in the line
            file_doc = ls.workspace.get_text_document(target_uri)
            if lb.lineno - 1 < len(file_doc.lines):
                label_line = file_doc.lines[lb.lineno - 1]
                m = re.search(rf"\blabel\s+{re.escape(old_name)}\b", label_line)
                if m:
                    start_col = m.start() + len("label ")
                    # Skip whitespace
                    while (
                        start_col < len(label_line) and label_line[start_col].isspace()
                    ):
                        start_col += 1
                    end_col = start_col + len(old_name)
                    changes[target_uri].append(
                        types.TextEdit(
                            range=types.Range(
                                start=types.Position(
                                    line=lb.lineno - 1, character=start_col
                                ),
                                end=types.Position(
                                    line=lb.lineno - 1, character=end_col
                                ),
                            ),
                            new_text=new_name,
                        )
                    )

        # Rename jump/call references — only scan files that contain matching targets
        _jump_uris = set(_workspace_index.get_jump_target_uris(old_name))
        _call_uris = set(_workspace_index.get_call_target_uris(old_name))
        _ref_uris = _jump_uris | _call_uris
        for ref_uri in _ref_uris:
            try:
                file_doc = ls.workspace.get_text_document(ref_uri)
            except Exception:
                continue
            fp = _path_from_uri(ref_uri)
            if not fp:
                continue
            _, ast, parser = _get_parse_for_file(fp)

            if ref_uri in _jump_uris:
                for j in parser.get_all_jumps():
                    if j.target == old_name:
                        if ref_uri not in changes:
                            changes[ref_uri] = []
                        if j.lineno - 1 < len(file_doc.lines):
                            jump_line = file_doc.lines[j.lineno - 1]
                            m = re.search(
                                rf"\bjump\s+(?:expression\s+)?{re.escape(old_name)}\b",
                                jump_line,
                            )
                            if m:
                                start_col = jump_line.find(old_name, m.start())
                                if start_col >= 0:
                                    changes[ref_uri].append(
                                        types.TextEdit(
                                            range=types.Range(
                                                start=types.Position(
                                                    line=j.lineno - 1,
                                                    character=start_col,
                                                ),
                                                end=types.Position(
                                                    line=j.lineno - 1,
                                                    character=start_col + len(old_name),
                                                ),
                                            ),
                                            new_text=new_name,
                                        )
                                    )

            if ref_uri in _call_uris:
                for c in parser.get_all_calls():
                    if c.target == old_name:
                        if ref_uri not in changes:
                            changes[ref_uri] = []
                        if c.lineno - 1 < len(file_doc.lines):
                            call_line = file_doc.lines[c.lineno - 1]
                            m = re.search(
                                rf"\bcall\s+(?:expression\s+)?{re.escape(old_name)}\b",
                                call_line,
                            )
                            if m:
                                start_col = call_line.find(old_name, m.start())
                                if start_col >= 0:
                                    changes[ref_uri].append(
                                        types.TextEdit(
                                            range=types.Range(
                                                start=types.Position(
                                                    line=c.lineno - 1,
                                                    character=start_col,
                                                ),
                                                end=types.Position(
                                                    line=c.lineno - 1,
                                                    character=start_col + len(old_name),
                                                ),
                                            ),
                                            new_text=new_name,
                                        )
                                    )

        return types.WorkspaceEdit(changes=changes) if changes else None

    # Rename screens
    all_screens = _get_all_workspace_screens()
    if old_name in all_screens:
        # Rename screen definitions
        for target_uri, s in all_screens[old_name]:
            if target_uri not in changes:
                changes[target_uri] = []
            file_doc = ls.workspace.get_text_document(target_uri)
            if s.lineno - 1 < len(file_doc.lines):
                screen_line = file_doc.lines[s.lineno - 1]
                m = re.search(rf"\bscreen\s+{re.escape(old_name)}\b", screen_line)
                if m:
                    start_col = m.start() + len("screen ")
                    while (
                        start_col < len(screen_line)
                        and screen_line[start_col].isspace()
                    ):
                        start_col += 1
                    end_col = start_col + len(old_name)
                    changes[target_uri].append(
                        types.TextEdit(
                            range=types.Range(
                                start=types.Position(
                                    line=s.lineno - 1, character=start_col
                                ),
                                end=types.Position(
                                    line=s.lineno - 1, character=end_col
                                ),
                            ),
                            new_text=new_name,
                        )
                    )

        # Rename call screen / show screen references
        for fp in _get_workspace_rpy_files():
            file_uri, ast, parser = _get_parse_for_file(fp)
            file_doc = ls.workspace.get_text_document(file_uri)

            for node in parser._collect(ast, CallScreen):
                if node.screen_name == old_name:
                    if file_uri not in changes:
                        changes[file_uri] = []
                    if node.lineno - 1 < len(file_doc.lines):
                        node_line = file_doc.lines[node.lineno - 1]
                        m = re.search(
                            rf"\bcall\s+screen\s+{re.escape(old_name)}\b", node_line
                        )
                        if m:
                            start_col = node_line.find(old_name, m.start())
                            if start_col >= 0:
                                changes[file_uri].append(
                                    types.TextEdit(
                                        range=types.Range(
                                            start=types.Position(
                                                line=node.lineno - 1,
                                                character=start_col,
                                            ),
                                            end=types.Position(
                                                line=node.lineno - 1,
                                                character=start_col + len(old_name),
                                            ),
                                        ),
                                        new_text=new_name,
                                    )
                                )

            for node in parser._collect(ast, ShowScreen):
                if node.screen_name == old_name:
                    if file_uri not in changes:
                        changes[file_uri] = []
                    if node.lineno - 1 < len(file_doc.lines):
                        node_line = file_doc.lines[node.lineno - 1]
                        m = re.search(
                            rf"\bshow\s+screen\s+{re.escape(old_name)}\b", node_line
                        )
                        if m:
                            start_col = node_line.find(old_name, m.start())
                            if start_col >= 0:
                                changes[file_uri].append(
                                    types.TextEdit(
                                        range=types.Range(
                                            start=types.Position(
                                                line=node.lineno - 1,
                                                character=start_col,
                                            ),
                                            end=types.Position(
                                                line=node.lineno - 1,
                                                character=start_col + len(old_name),
                                            ),
                                        ),
                                        new_text=new_name,
                                    )
                                )

        return types.WorkspaceEdit(changes=changes) if changes else None

    return None


# ─────────────────────── Workspace Commands ─────────────────────────────


@LSP_SERVER.command("renpy.refreshWorkspace")
def cmd_refresh_workspace() -> Dict[str, object]:
    """Clear parse cache and re-parse all workspace files."""
    _log.info("command refreshWorkspace: clearing %d cached entries", len(_parse_cache))
    old_count = len(_parse_cache)
    with _cache_lock:
        _parse_cache.clear()
        _path_to_uri.clear()
    _renpy_py_cache.clear()

    # Full rebuild of workspace index (re-globs + re-parses all files)
    t0 = _time.monotonic()
    _workspace_index.rebuild()
    files = _workspace_index.get_file_list()
    elapsed = (_time.monotonic() - t0) * 1000
    _log.info(
        "command refreshWorkspace: re-parsed %d file(s) in %.1f ms", len(files), elapsed
    )

    return {
        "success": True,
        "message": f"Refreshed {len(files)} files (cleared {old_count} cached entries)",
        "fileCount": len(files),
    }


@LSP_SERVER.command("renpy.showStats")
def cmd_show_stats() -> Dict[str, object]:
    """Collect and return project statistics."""
    _log.info("command showStats: collecting statistics")
    files = _get_workspace_rpy_files()
    total_lines = 0
    total_labels = 0
    total_screens = 0
    total_defines = 0
    total_defaults = 0
    total_images = 0
    total_transforms = 0
    total_dialogue_lines = 0
    total_words = 0

    for fp in files:
        _, ast, parser = _get_parse_for_file(fp)

        # Count lines
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                total_lines += len(lines)
        except OSError:
            pass

        # Count various node types
        total_labels += len(parser.get_all_labels())
        total_screens += len(parser.get_all_screens())
        total_defines += len(parser.get_all_defines())
        total_defaults += len(parser.get_all_defaults())
        total_images += len(parser.get_all_images())
        total_transforms += len(parser._collect(ast, TransformDef))

        # Count dialogue (Say nodes) and words
        for node in parser._collect(ast, Say):
            total_dialogue_lines += 1
            total_words += count_words(node.what)
        for node in parser._collect(ast, NarratorSay):
            total_dialogue_lines += 1
            total_words += count_words(node.what)

    return {
        "files": len(files),
        "lines": total_lines,
        "labels": total_labels,
        "screens": total_screens,
        "defines": total_defines,
        "defaults": total_defaults,
        "images": total_images,
        "transforms": total_transforms,
        "dialogueLines": total_dialogue_lines,
        "words": total_words,
    }


# ─────────────────────── Entry Point ─────────────────────────────────────

if __name__ == "__main__":
    _log.info("Starting Ren'Py LSP server (stdio)…")
    LSP_SERVER.start_io()
