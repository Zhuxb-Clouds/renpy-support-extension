"""Ren'Py Language Server — LSP features powered by ast_parser."""

from __future__ import annotations

import sys
import os
import re

# Ensure the bundled/tools directory is on sys.path so we can import ast_parser.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lsprotocol import types
from pygls.lsp.server import LanguageServer

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

from typing import Dict, List, Optional, Tuple, Union
import glob
from pathlib import Path
from urllib.parse import quote as url_quote
from urllib.parse import unquote as url_unquote

MAX_WORKERS = 4
LSP_SERVER = LanguageServer(
    name="renpy-server", version="1.0.0", max_workers=MAX_WORKERS
)


# ─────────────────────── Cache / Index ───────────────────────────────────

# Per-URI parse cache so we don't re-parse on every request.
_parse_cache: Dict[str, Tuple[str, Script, RpyParser]] = {}


def _get_parse(uri: str, source: Optional[str] = None) -> Tuple[Script, RpyParser]:
    """Return cached (ast, parser) for *uri*, re-parsing only when source changes."""
    doc = LSP_SERVER.workspace.get_text_document(uri)
    text = source if source is not None else doc.source
    cached = _parse_cache.get(uri)
    if cached and cached[0] == text:
        return cached[1], cached[2]
    parser = RpyParser(text)
    ast = parser.parse()
    _parse_cache[uri] = (text, ast, parser)
    return ast, parser


def _uri_from_path(path: str) -> str:
    """Convert a filesystem path to a file:// URI."""
    return Path(os.path.abspath(path)).as_uri()


def _path_from_uri(uri: str) -> str:
    """Convert a file:// URI to a filesystem path."""
    if uri.startswith("file://"):
        return url_unquote(uri[7:])
    return uri


def _get_workspace_rpy_files() -> List[str]:
    """Return all .rpy / .rpym file paths in the workspace."""
    results: List[str] = []
    for folder in LSP_SERVER.workspace.folders.values():
        root = _path_from_uri(folder.uri)
        results.extend(glob.glob(os.path.join(root, "**", "*.rpy"), recursive=True))
        results.extend(glob.glob(os.path.join(root, "**", "*.rpym"), recursive=True))
    return results


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
    uri = _uri_from_path(filepath)
    cached = _parse_cache.get(uri)
    if cached:
        return uri, cached[1], cached[2]
    try:
        text = Path(filepath).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    parser = RpyParser(text)
    ast = parser.parse()
    _parse_cache[uri] = (text, ast, parser)
    return uri, ast, parser


def _get_all_workspace_labels() -> Dict[str, List[Tuple[str, "Label"]]]:
    """Return {label_name: [(uri, Label), ...]} across all workspace .rpy files."""
    result: Dict[str, List[Tuple[str, Label]]] = {}
    for fp in _get_workspace_rpy_files():
        uri, ast, parser = _get_parse_for_file(fp)
        for lb in parser.get_all_labels():
            result.setdefault(lb.name, []).append((uri, lb))
    return result


def _get_all_workspace_defines() -> Dict[str, List[Tuple[str, "Define"]]]:
    """Return {name: [(uri, Define), ...]} across all workspace .rpy files."""
    result: Dict[str, List[Tuple[str, Define]]] = {}
    for fp in _get_workspace_rpy_files():
        uri, ast, parser = _get_parse_for_file(fp)
        for d in parser.get_all_defines():
            result.setdefault(d.name, []).append((uri, d))
    return result


def _get_all_workspace_defaults() -> Dict[str, List[Tuple[str, "Default"]]]:
    """Return {name: [(uri, Default), ...]} across all workspace .rpy files."""
    result: Dict[str, List[Tuple[str, Default]]] = {}
    for fp in _get_workspace_rpy_files():
        uri, ast, parser = _get_parse_for_file(fp)
        for d in parser.get_all_defaults():
            result.setdefault(d.name, []).append((uri, d))
    return result


def _get_all_workspace_screens() -> Dict[str, List[Tuple[str, "ScreenDef"]]]:
    """Return {name: [(uri, ScreenDef), ...]} across all workspace .rpy files."""
    result: Dict[str, List[Tuple[str, ScreenDef]]] = {}
    for fp in _get_workspace_rpy_files():
        uri, ast, parser = _get_parse_for_file(fp)
        for s in parser.get_all_screens():
            result.setdefault(s.name, []).append((uri, s))
    return result


def _get_all_workspace_images() -> Dict[str, List[Tuple[str, "ImageDef"]]]:
    """Return {image_name: [(uri, ImageDef), ...]} across all workspace .rpy files."""
    result: Dict[str, List[Tuple[str, ImageDef]]] = {}
    for fp in _get_workspace_rpy_files():
        uri, ast, parser = _get_parse_for_file(fp)
        for img in parser.get_all_images():
            result.setdefault(img.name, []).append((uri, img))
    return result


def _get_all_workspace_transforms() -> Dict[str, List[Tuple[str, "TransformDef"]]]:
    """Return {name: [(uri, TransformDef), ...]} across all workspace .rpy files."""
    result: Dict[str, List[Tuple[str, TransformDef]]] = {}
    for fp in _get_workspace_rpy_files():
        uri, ast, parser = _get_parse_for_file(fp)
        for node in parser._collect(ast, TransformDef):
            result.setdefault(node.name, []).append((uri, node))
    return result


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


def _resolve_image_name_to_file(image_name: str) -> Optional[str]:
    """Try to find an image file matching *image_name* via Ren'Py auto-detection.

    Ren'Py scans ``config.image_directories`` (default ``['images']``) and
    creates image names from file paths: ``images/bg/room.png`` → ``bg room``.
    Also supports flat naming: ``images/bg_room.png`` → ``bg_room``.
    """
    IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".avif", ".svg")
    for search_dir in _get_renpy_search_dirs():
        images_dir = os.path.join(search_dir, "images")
        if not os.path.isdir(images_dir):
            continue
        # Walk images directory looking for matching files
        for dirpath, _dirnames, filenames in os.walk(images_dir):
            for fn in filenames:
                base, ext = os.path.splitext(fn)
                if ext.lower() not in IMAGE_EXTENSIONS:
                    continue
                # Build Ren'Py auto-image name: relative path with / replaced by space
                rel = os.path.relpath(os.path.join(dirpath, fn), images_dir)
                rel_no_ext = os.path.splitext(rel)[0]
                auto_name = rel_no_ext.replace(os.sep, " ").replace("/", " ")
                if auto_name.lower() == image_name.lower():
                    return os.path.abspath(os.path.join(dirpath, fn))
                # Also try matching just the filename without extension
                if base.lower() == image_name.lower():
                    return os.path.abspath(os.path.join(dirpath, fn))
    return None


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
    """Create a Location pointing to a node."""
    return types.Location(
        uri=uri,
        range=types.Range(
            start=types.Position(line=node.lineno - 1, character=0),
            end=types.Position(line=node.lineno - 1, character=999),
        ),
    )


def _publish_diagnostics(uri: str):
    """Parse the document and push diagnostics."""
    ast, parser = _get_parse(uri)
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

    LSP_SERVER.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=diags)
    )


# ─────────────────────── Document Sync ───────────────────────────────────


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: LanguageServer, params: types.DidOpenTextDocumentParams):
    _publish_diagnostics(params.text_document.uri)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: LanguageServer, params: types.DidChangeTextDocumentParams):
    _publish_diagnostics(params.text_document.uri)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: LanguageServer, params: types.DidCloseTextDocumentParams):
    uri = params.text_document.uri
    _parse_cache.pop(uri, None)
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=[])
    )


# ─────────────────────── Document Symbols ────────────────────────────────


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
def document_symbols(
    ls: LanguageServer, params: types.DocumentSymbolParams
) -> List[types.DocumentSymbol]:
    ast, parser = _get_parse(params.text_document.uri)
    return _build_symbols(ast.body)


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


# ─────────────────────── Go to Definition ────────────────────────────────


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DEFINITION)
def goto_definition(
    ls: LanguageServer, params: types.DefinitionParams
) -> Optional[List[types.Location]]:
    uri = params.text_document.uri
    doc = ls.workspace.get_text_document(uri)
    pos = params.position
    line_text = doc.lines[pos.line] if pos.line < len(doc.lines) else ""
    word = _word_at_position(line_text, pos.character)
    ast, parser = _get_parse(uri)

    # ── 1) AST-based resolution: find node at cursor line ──
    lineno = pos.line + 1  # 1-based
    nodes = _find_nodes_at_line(parser, lineno)
    for node in nodes:
        loc = _resolve_node_definition(
            node, source_uri=uri, line_text=line_text, col=pos.character
        )
        if loc:
            return loc if isinstance(loc, list) else [loc]

    # ── 2) Quoted string → file path ──
    quoted = _extract_quoted_string(line_text, pos.character)
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
        return [_make_node_location(u, lb) for u, lb in all_labels[word]]

    # Defines / Defaults
    all_defines = _get_all_workspace_defines()
    if word in all_defines:
        return [_make_node_location(u, d) for u, d in all_defines[word]]
    all_defaults = _get_all_workspace_defaults()
    if word in all_defaults:
        return [_make_node_location(u, d) for u, d in all_defaults[word]]

    # Screens
    all_screens = _get_all_workspace_screens()
    if word in all_screens:
        return [_make_node_location(u, s) for u, s in all_screens[word]]

    # Images
    all_images = _get_all_workspace_images()
    if word in all_images:
        return [_make_node_location(u, img) for u, img in all_images[word]]

    # Transforms
    all_transforms = _get_all_workspace_transforms()
    if word in all_transforms:
        return [_make_node_location(u, t) for u, t in all_transforms[word]]

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

    # ── Play / Queue → audio file ──
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
        return None

    if isinstance(node, QueueMusic):
        filename = node.filename.strip()
        if filename:
            resolved = _resolve_renpy_file(filename, source_uri=source_uri)
            if resolved:
                return _make_file_location(resolved)
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

# Ren'Py keywords for completion.
_RENPY_KEYWORDS = [
    "label",
    "jump",
    "call",
    "return",
    "pass",
    "menu",
    "if",
    "elif",
    "else",
    "while",
    "for",
    "in",
    "define",
    "default",
    "image",
    "transform",
    "screen",
    "style",
    "scene",
    "show",
    "hide",
    "with",
    "at",
    "behind",
    "play",
    "stop",
    "queue",
    "voice",
    "init",
    "python",
    "translate",
    "pause",
    "nvl",
    "window",
    "show screen",
    "hide screen",
    "call screen",
]

_RENPY_TRANSITIONS = [
    "dissolve",
    "fade",
    "pixellate",
    "move",
    "moveinright",
    "moveinleft",
    "moveintop",
    "moveinbottom",
    "moveoutright",
    "moveoutleft",
    "moveouttop",
    "moveoutbottom",
    "ease",
    "easeinright",
    "easeinleft",
    "easeintop",
    "easeinbottom",
    "easeoutright",
    "easeoutleft",
    "easeouttop",
    "easeoutbottom",
    "zoomin",
    "zoomout",
    "vpunch",
    "hpunch",
    "blinds",
    "squares",
    "wipeleft",
    "wiperight",
    "wipeup",
    "wipedown",
    "None",
]

_RENPY_TRANSFORMS = [
    "left",
    "right",
    "center",
    "truecenter",
    "topleft",
    "topright",
    "top",
    "bottom",
    "offscreenleft",
    "offscreenright",
    "default",
    "reset",
]


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
    line_prefix = line_text[: pos.character].strip()

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
        for t in _RENPY_TRANSITIONS:
            items.append(
                types.CompletionItem(
                    label=t,
                    kind=types.CompletionItemKind.Constant,
                    detail="transition",
                )
            )
    elif line_prefix.endswith("at "):
        # Complete transforms
        for t in _RENPY_TRANSFORMS:
            items.append(
                types.CompletionItem(
                    label=t,
                    kind=types.CompletionItemKind.Constant,
                    detail="transform position",
                )
            )
        # Also suggest user-defined transforms — not implemented yet for cross-file.
    elif line_prefix.endswith(("show screen ", "hide screen ", "call screen ")):
        # Complete screen names
        for s in parser.get_all_screens():
            items.append(
                types.CompletionItem(
                    label=s.name,
                    kind=types.CompletionItemKind.Class,
                    detail=f"screen (line {s.lineno})",
                )
            )
    else:
        # General: keywords + characters + labels
        for kw in _RENPY_KEYWORDS:
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

    return types.CompletionList(is_incomplete=False, items=items)


# ─────────────────────── Hover ───────────────────────────────────────────

_KEYWORD_DOCS = {
    "label": "**label** *name*:\n\nDefines a named point in the script that can be jumped to or called.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/label.html)",
    "jump": "**jump** *label_name*\n\nTransfers control to the named label. Does not return.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/label.html#jump-statement)",
    "call": "**call** *label_name* [**from** *id*]\n\nCalls the named label as a subroutine. Use `return` to come back.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/label.html#call-statement)",
    "return": "**return** [*expression*]\n\nReturns from a `call` statement, optionally with a value.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/label.html#return-statement)",
    "menu": "**menu** [*name*]:\n\nDisplays a menu of choices to the player.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/menus.html)",
    "define": "**define** *name* = *expression*\n\nDefines a name at init time. Commonly used for `Character()` definitions.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/python.html#define-statement)",
    "default": "**default** *name* = *expression*\n\nSets the default value of a variable, created at game start.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/python.html#default-statement)",
    "scene": "**scene** *image* [**at** *transform*] [**with** *transition*]\n\nClears all images and shows a new background.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/displaying_images.html#scene-statement)",
    "show": "**show** *image* [**at** *transform*] [**with** *transition*]\n\nShows an image on the screen.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/displaying_images.html#show-statement)",
    "hide": "**hide** *image* [**with** *transition*]\n\nHides the named image.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/displaying_images.html#hide-statement)",
    "with": "**with** *transition*\n\nApplies a transition effect (e.g., dissolve, fade).\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/displaying_images.html#with-statement)",
    "play": '**play** *channel* "*file*" [**fadein** *sec*]\n\nPlays audio on the specified channel.\n\n[📖 Ren\'Py Docs](https://www.renpy.org/doc/html/audio.html#play-statement)',
    "stop": "**stop** *channel* [**fadeout** *sec*]\n\nStops audio on the specified channel.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/audio.html#stop-statement)",
    "voice": '**voice** "*file*"\n\nPlays a voice file for the next line of dialogue.\n\n[📖 Ren\'Py Docs](https://www.renpy.org/doc/html/voice.html)',
    "screen": "**screen** *name*([*params*]):\n\nDefines a screen for the screen language.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/screens.html)",
    "transform": "**transform** *name*([*params*]):\n\nDefines an ATL transform.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-statement)",
    "image": "**image** *name* = *expression*\n\nDefines an image that can be used with `show` or `scene`.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/displaying_images.html#image-statement)",
    "init": "**init** [*priority*] [**python**]:\n\nRuns code at initialization time, before the game starts.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/python.html#init-python-statement)",
    "python": "**python**:\n\nA block of Python code to execute.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/python.html)",
    "if": "**if** *condition*:\n\nConditional branch. Can be followed by `elif` and `else`.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/conditional.html)",
    "elif": "**elif** *condition*:\n\nAlternative branch in an if/elif/else chain.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/conditional.html)",
    "else": "**else**:\n\nFinal fallback branch in an if/elif/else chain.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/conditional.html)",
    "while": "**while** *condition*:\n\nRepeats the indented block as long as the condition is true.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/conditional.html#while-statement)",
    "for": "**for** *var* **in** *iterable*:\n\nIterates over items in a collection.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/conditional.html#while-statement)",
    "pass": "**pass**\n\nA no-op statement. Used as a placeholder.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/label.html)",
    "translate": "**translate** *language* *identifier*:\n\nProvides a translation for a block of dialogue.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/translation.html)",
    "style": "**style** *name* [**is** *parent*]:\n\nDefines or modifies a style.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style.html)",
    # ── ATL keywords ──
    "contains": "**contains**:\n\nIn ATL, creates a child displayable within a transform. Multiple `contains` blocks display layered children.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#contains-statement)",
    "parallel": "**parallel**:\n\nRuns multiple ATL blocks simultaneously.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#parallel-statement)",
    "block": "**block**:\n\nGroups a set of ATL statements together as one unit.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#block-statement)",
    "choice": "**choice** [*weight*]:\n\nIn ATL, randomly picks one of several branches to execute.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#choice-statement)",
    "linear": "**linear** *duration*\n\nATL warper: interpolates properties linearly over *duration* seconds.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#warpers)",
    "ease": "**ease** *duration*\n\nATL warper: interpolates with ease-in/ease-out (slow start and end).\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#warpers)",
    "easein": "**easein** *duration*\n\nATL warper: slow at the start, fast at the end.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#warpers)",
    "easeout": "**easeout** *duration*\n\nATL warper: fast at the start, slow at the end.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#warpers)",
    "repeat": "**repeat** [*count*]\n\nRepeats the ATL block. Without a count, repeats forever.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#repeat-statement)",
    "pause": "**pause** [*duration*]\n\nPauses execution for the given number of seconds.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#pause-statement)",
    # ── ATL / Style properties ──
    "xpos": "**xpos** *value*\n\nHorizontal position in pixels.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-xpos)",
    "ypos": "**ypos** *value*\n\nVertical position in pixels.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-ypos)",
    "xanchor": "**xanchor** *value*\n\nHorizontal anchor point (0.0 = left, 0.5 = center, 1.0 = right).\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-xanchor)",
    "yanchor": "**yanchor** *value*\n\nVertical anchor point (0.0 = top, 0.5 = center, 1.0 = bottom).\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-yanchor)",
    "xalign": "**xalign** *value*\n\nSets both `xpos` and `xanchor` to the same value. 0.0 = left, 0.5 = center, 1.0 = right.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-xalign)",
    "yalign": "**yalign** *value*\n\nSets both `ypos` and `yanchor` to the same value. 0.0 = top, 0.5 = center, 1.0 = bottom.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-yalign)",
    "align": "**align** (*xalign*, *yalign*)\n\nShorthand for setting `xalign` and `yalign` together.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-align)",
    "xoffset": "**xoffset** *pixels*\n\nHorizontal offset from the computed position, in pixels.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-xoffset)",
    "yoffset": "**yoffset** *pixels*\n\nVertical offset from the computed position, in pixels.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-yoffset)",
    "xsize": "**xsize** *pixels*\n\nSets the width of the displayable.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-xsize)",
    "ysize": "**ysize** *pixels*\n\nSets the height of the displayable.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-ysize)",
    "xysize": "**xysize** (*width*, *height*)\n\nShorthand for setting `xsize` and `ysize` together.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-xysize)",
    "rotate": "**rotate** *degrees*\n\nRotates the displayable by the given number of degrees.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-properties)",
    "zoom": "**zoom** *factor*\n\nScales the displayable by the given factor (1.0 = no change).\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-properties)",
    "xzoom": "**xzoom** *factor*\n\nHorizontal scale factor. Negative values flip horizontally.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-properties)",
    "yzoom": "**yzoom** *factor*\n\nVertical scale factor. Negative values flip vertically.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-properties)",
    "alpha": "**alpha** *value*\n\nOpacity of the displayable (0.0 = transparent, 1.0 = opaque).\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-properties)",
    "crop": "**crop** (*x*, *y*, *w*, *h*)\n\nCrops the displayable to the given rectangle.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-properties)",
    "blur": "**blur** *radius*\n\nApplies a Gaussian blur with the given radius.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-properties)",
}


@LSP_SERVER.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: LanguageServer, params: types.HoverParams) -> Optional[types.Hover]:
    uri = params.text_document.uri
    doc = ls.workspace.get_text_document(uri)
    line_text = (
        doc.lines[params.position.line] if params.position.line < len(doc.lines) else ""
    )
    word = _word_at_position(line_text, params.position.character)
    if not word:
        return None

    # 1) Check keyword docs
    if word in _KEYWORD_DOCS:
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown,
                value=_KEYWORD_DOCS[word],
            )
        )

    ast, parser = _get_parse(uri)

    # 2) Check labels across workspace
    all_labels = _get_all_workspace_labels()
    if word in all_labels:
        target_uri, lb = all_labels[word][0]
        fname = os.path.basename(_path_from_uri(target_uri))
        info = f"**label** `{lb.name}`\n\nDefined in `{fname}` at line {lb.lineno}"
        if lb.parameters:
            info += f"\n\nParameters: `{lb.parameters}`"
        return types.Hover(
            contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=info)
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


@LSP_SERVER.feature(types.TEXT_DOCUMENT_FORMATTING)
def format_document(ls: LanguageServer, params: types.DocumentFormattingParams):
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


# ─────────────────────── Entry Point ─────────────────────────────────────

if __name__ == "__main__":
    LSP_SERVER.start_io()
