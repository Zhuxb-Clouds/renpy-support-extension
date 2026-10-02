"""Shared runtime context for the Ren'Py LSP server.

Owns the single ``LanguageServer`` instance, runtime settings, the parse
cache, the workspace index, and the file/AST helper functions that the
feature modules need.  Everything here used to live in ``lsp_server.py``.

Feature modules (``diagnostics``, ``completion``) reach the workspace
getters and other patchable helpers through this module
(``ctx._get_all_workspace_labels()``) so tests can monkeypatch one
canonical location instead of each importer's binding.
"""

from __future__ import annotations

import os
import re
import sys
import json

# Ensure the bundled/tools directory is on sys.path so we can import ast_parser.
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

# Ensure bundled third-party libraries (pygls, lsprotocol, …) are importable.
_LIBS_DIR = os.path.join(os.path.dirname(_TOOLS_DIR), "libs")
if os.path.isdir(_LIBS_DIR) and _LIBS_DIR not in sys.path:
    sys.path.insert(0, _LIBS_DIR)

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import (
    from_fs_path as _pygls_from_fs_path,
    to_fs_path as _pygls_to_fs_path,
)

from ast_parser import (
    Node,
    RpyParser,
    Script,
    Label,
    Define,
    Default,
    ImageDef,
    TransformDef,
    ScreenDef,
    StyleDef,
    PythonOneliner,
)

from renpy_data import KEYWORD_DOCS  # noqa: F401  (re-exported for hover)
from workspace_index import WorkspaceIndex

from typing import Dict, List, Optional, Tuple
import glob
import threading
import time as _time
from pathlib import Path
from urllib.parse import unquote as url_unquote

# Suppress noisy "Cancel notification for unknown message id" warnings.
# These occur normally when VS Code cancels requests the server already finished.
import logging as _logging

_logging.getLogger("pygls.protocol.json_rpc").setLevel(_logging.ERROR)

MAX_WORKERS = 4
LSP_SERVER = LanguageServer(
    name="renpy-server", version="1.9.0", max_workers=MAX_WORKERS
)

# ── Server logger (prints to stderr, which VS Code captures in the Output channel) ──
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

_log.info("Ren'Py LSP server context module loaded")


# ── Runtime settings ─────────────────────────────────────────────────────

_settings = {
    "formatting": {
        "enabled": True,
        "indentSize": 4,
        "blankLines": "collapse",
    },
    "diagnostics": {
        "enabled": True,
        "fullOnSave": False,
        "severity": {},
    },
}

# Allowed values for the `blankLines` formatting mode:
#   preserve   — leave blank lines untouched
#   collapse   — collapse consecutive blank lines into one (default)
#   betweenSay — like collapse, plus insert one blank line between
#                dialogue/narration lines inside label script blocks
#   strip      — remove all blank lines
_BLANK_LINE_MODES = ("preserve", "collapse", "betweenSay", "strip")

# Style settings as last sent by the VS Code client.  `.renpy-format.json`
# overrides these per key; the merged result lives in _settings["formatting"].
_vscode_style: Dict[str, object] = {"indentSize": 4, "blankLines": "collapse"}

# Path to the workspace-root format config file, if one exists.
_format_config_path: Optional[str] = None

_FORMAT_CONFIG_FILENAME = ".renpy-format.json"


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _coerce_int(value: object, default: int) -> int:
    if isinstance(value, int):
        return value
    return default


def _coerce_choice(value: object, allowed: Tuple[str, ...], default: str) -> str:
    if isinstance(value, str) and value in allowed:
        return value
    return default


# Allowed values for ``diagnostics.severity`` overrides; ``none`` suppresses
# the check entirely.
_SEVERITY_NAMES = ("error", "warning", "information", "hint", "none")


def _coerce_severity_overrides(raw: dict) -> dict:
    """Validate a {check_code: severity_name} map, dropping unknown entries."""
    out: Dict[str, str] = {}
    for code, value in raw.items():
        if isinstance(code, str) and isinstance(value, str):
            name = value.strip().lower()
            if name in _SEVERITY_NAMES:
                out[code] = name
            else:
                _log.warning(
                    "diagnostics.severity: unknown severity %r for %r — ignored",
                    value,
                    code,
                )
    return out


def _extract_renpy_settings(raw_settings: object) -> dict:
    if not isinstance(raw_settings, dict):
        return {}

    settings = raw_settings.get("renpy-lsp", raw_settings)
    if not isinstance(settings, dict):
        return {}

    return settings


def _update_settings(raw_settings: object) -> None:
    settings = _extract_renpy_settings(raw_settings)
    if not settings:
        return

    formatting = settings.get("formatting", {})
    if isinstance(formatting, dict):
        _settings["formatting"]["enabled"] = _coerce_bool(
            formatting.get("enabled"), _settings["formatting"]["enabled"]
        )
        _vscode_style["indentSize"] = _coerce_int(
            formatting.get("indentSize"), _vscode_style["indentSize"]
        )
        _vscode_style["blankLines"] = _coerce_choice(
            formatting.get("blankLines"),
            _BLANK_LINE_MODES,
            str(_vscode_style["blankLines"]),
        )
    elif "formatting.enabled" in settings:
        _settings["formatting"]["enabled"] = _coerce_bool(
            settings.get("formatting.enabled"), _settings["formatting"]["enabled"]
        )

    diagnostics = settings.get("diagnostics", {})
    if isinstance(diagnostics, dict):
        _settings["diagnostics"]["enabled"] = _coerce_bool(
            diagnostics.get("enabled"), _settings["diagnostics"]["enabled"]
        )
        _settings["diagnostics"]["fullOnSave"] = _coerce_bool(
            diagnostics.get("fullOnSave"), _settings["diagnostics"]["fullOnSave"]
        )
        raw_severity = diagnostics.get("severity")
        if isinstance(raw_severity, dict):
            _settings["diagnostics"]["severity"] = _coerce_severity_overrides(
                raw_severity
            )
    elif "diagnostics.enabled" in settings:
        _settings["diagnostics"]["enabled"] = _coerce_bool(
            settings.get("diagnostics.enabled"), _settings["diagnostics"]["enabled"]
        )
        _settings["diagnostics"]["fullOnSave"] = _coerce_bool(
            settings.get("diagnostics.fullOnSave"),
            _settings["diagnostics"]["fullOnSave"],
        )

    _apply_format_config()

    _log.info(
        "settings: formatting.enabled=%s formatting.indentSize=%s "
        "formatting.blankLines=%s diagnostics.enabled=%s diagnostics.fullOnSave=%s "
        "diagnostics.severity=%d override(s)",
        _settings["formatting"]["enabled"],
        _settings["formatting"]["indentSize"],
        _settings["formatting"]["blankLines"],
        _settings["diagnostics"]["enabled"],
        _settings["diagnostics"]["fullOnSave"],
        len(_settings["diagnostics"]["severity"]),
    )


def _find_format_config() -> Optional[str]:
    """Return the path of the first `.renpy-format.json` in a workspace root."""
    try:
        folders = list(LSP_SERVER.workspace.folders.values())
    except Exception:  # workspace not initialized (e.g. in tests)
        return None
    for folder in folders:
        candidate = os.path.join(_path_from_uri(folder.uri), _FORMAT_CONFIG_FILENAME)
        if os.path.isfile(candidate):
            return candidate
    return None


def _load_format_config(path: str) -> dict:
    """Read and validate `.renpy-format.json`; return {} on any problem."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        _log.warning("%s: root value is not an object — ignored", path)
    except (OSError, ValueError) as exc:
        _log.warning("failed to read %s: %s", path, exc)
    return {}


def _apply_format_config() -> None:
    """Rebuild style settings: VS Code values, overridden per key by
    `.renpy-format.json` when one is present in the workspace root."""
    fmt = _settings["formatting"]
    fmt["indentSize"] = _vscode_style["indentSize"]
    fmt["blankLines"] = _vscode_style["blankLines"]
    if not _format_config_path:
        return
    cfg = _load_format_config(_format_config_path)
    if "indentSize" in cfg:
        fmt["indentSize"] = _coerce_int(cfg.get("indentSize"), fmt["indentSize"])
    if "blankLines" in cfg:
        fmt["blankLines"] = _coerce_choice(
            cfg.get("blankLines"), _BLANK_LINE_MODES, str(fmt["blankLines"])
        )
    _log.info(
        "format config %s: indentSize=%s blankLines=%s",
        _format_config_path,
        fmt["indentSize"],
        fmt["blankLines"],
    )


def _refresh_format_config() -> None:
    """(Re)discover and apply `.renpy-format.json` from the workspace root."""
    global _format_config_path
    _format_config_path = _find_format_config()
    _apply_format_config()


def _formatting_enabled() -> bool:
    return bool(_settings["formatting"]["enabled"])


def _diagnostics_enabled() -> bool:
    return bool(_settings["diagnostics"]["enabled"])


def _full_diagnostics_on_save() -> bool:
    return bool(_settings["diagnostics"]["fullOnSave"])


def _diagnostic_severity_overrides() -> Dict[str, str]:
    """Per-check severity overrides: {check_code: "error"|"warning"|
    "information"|"hint"|"none"} — ``none`` suppresses the check."""
    return dict(_settings["diagnostics"].get("severity") or {})

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


def _get_all_workspace_show_tags() -> "set[str]":
    """Return all tags introduced by ``show X as tag`` across the workspace."""
    return _workspace_index.get_show_tags()


def _get_all_workspace_styles() -> Dict[str, List[Tuple[str, "StyleDef"]]]:
    """Return {name: [(uri, StyleDef), ...]} across all workspace .rpy files."""
    return _workspace_index.get_styles()


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


def _all_workspace_python_names() -> set:
    """All Python-level names defined across the workspace (.rpy + *_ren.py).

    One batch scan for callers that need membership tests for *many* names
    (e.g. the transform-reference diagnostic); far cheaper than calling
    ``_find_python_var_across_workspace`` once per name, since each of those
    calls re-walks every file.
    """
    names: set = set()
    for fp in _get_workspace_rpy_files():
        uri, ast, parser = _get_parse_for_file(fp)
        names.update(_find_python_definitions_in_file(uri, parser, ast))
    for fp in _get_workspace_renpy_py_files():
        _uri, defs = _find_python_definitions_in_py_file(fp)
        names.update(defs)
    return names


# uri → (content_hash, {word: [1-based line numbers]})
_word_map_cache: Dict[str, Tuple[int, Dict[str, List[int]]]] = {}

# Variables the Ren'Py engine reads without any script reference — never
# reported as unused.
_ENGINE_READ_NAMES = {"save_name"}


def _word_map_from_text(text: str) -> Dict[str, List[int]]:
    """Word → 1-based line numbers for *text* (pure)."""
    words: Dict[str, List[int]] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        for word in re.findall(r"\w+", line):
            lines_for_word = words.get(word)
            if lines_for_word is None:
                words[word] = [lineno]
            else:
                lines_for_word.append(lineno)
    return words


def _file_word_lines(uri: str) -> Dict[str, List[int]]:
    """Word → line-number map of *uri*'s cached text, cached per version.

    Backs the unused-define diagnostic, which needs to know whether a
    candidate name occurs anywhere outside its own definition line.
    """
    with _cache_lock:
        cached = _parse_cache.get(uri)
    if not cached:
        return {}
    content_hash, text = cached[0], cached[1]
    hit = _word_map_cache.get(uri)
    if hit and hit[0] == content_hash:
        return hit[1]
    words = _word_map_from_text(text)
    _word_map_cache[uri] = (content_hash, words)
    return words


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
