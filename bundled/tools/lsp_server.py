"""Ren'Py Language Server — LSP features powered by ast_parser.

Feature handlers live here; shared state and helpers live in
``server_context``, diagnostics in ``diagnostics``, completion in
``completion``, and quick fixes in ``code_actions``.
"""

from __future__ import annotations

import os
import re
import sys

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

from ast_parser import (
    Label,
    Define,
    Default,
    ImageDef,
    TransformDef,
    ScreenDef,
    StyleDef,
    If,
    Menu,
    MenuItem,
    Say,
    NarratorSay,
    Scene,
    Show,
    Hide,
    PlayMusic,
    QueueMusic,
    Voice,
    Init,
    Translate,
    Comment,
    Node,
    CallScreen,
    ShowScreen,
    HideScreen,
)

from renpy_data import KEYWORD_DOCS, count_words

from server_context import (
    LSP_SERVER,
    _log,
    _settings,
    _update_settings,
    _refresh_format_config,
    _formatting_enabled,
    _diagnostics_enabled,
    _full_diagnostics_on_save,
    _utf16_col_to_utf32,
    _parse_cache,
    _cache_lock,
    _renpy_py_cache,
    _path_to_uri,
    _FORMAT_CONFIG_FILENAME,
    _get_parse,
    _short_uri,
    _uri_from_path,
    _find_nodes_at_line,
    _cursor_on_image_name,
    _extract_quoted_string,
    _make_file_location,
    _make_node_location,
    _dedup_locations,
    _try_extract_path,
    _word_at_position,
)
import server_context as ctx

from diagnostics import (
    _schedule_full_diagnostics,
    forget_document,
    full_diagnostics_needed,
    refresh_open_documents,
)
from completion import _completion_items_for_context
import code_actions
import signatures
import translation
from translation import _renpy_translate_id, _find_say_at_line

from typing import Dict, List, Optional, Tuple, Union
from pathlib import Path
import time as _time


# ── Runtime settings / cache / workspace index live in server_context. ──


@LSP_SERVER.feature(types.INITIALIZE)
def initialize(ls: LanguageServer, params: types.InitializeParams) -> None:
    _update_settings(params.initialization_options)
    _refresh_format_config()


@LSP_SERVER.feature(types.WORKSPACE_DID_CHANGE_CONFIGURATION)
def did_change_configuration(
    ls: LanguageServer, params: types.DidChangeConfigurationParams
) -> None:
    _update_settings(params.settings)
    _refresh_format_config()
    # Severity overrides may have changed — re-run open documents so the
    # panel reflects them without requiring a reopen.
    refresh_open_documents()


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: LanguageServer, params: types.DidOpenTextDocumentParams):
    uri = params.text_document.uri
    _log.info("didOpen: %s", _short_uri(uri))
    # Warm the cache synchronously (fast) so completions/hover work immediately.
    ctx._get_parse(uri)
    # Kick off background index warm-up on the first file open.
    if not ctx._workspace_index.is_ready() and not ctx._workspace_index._warming:
        ctx._workspace_index.warm()
    # Repopulate client state — diagnostics are ephemeral, so after a window
    # reload this is the only chance to publish them.  Background thread.
    if _diagnostics_enabled():
        _schedule_full_diagnostics(uri)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: LanguageServer, params: types.DidChangeTextDocumentParams):
    uri = params.text_document.uri
    _log.debug("didChange: %s", _short_uri(uri))
    # Parse immediately so the cache is warm for completions/hover; the
    # diagnostics pass follows through the coalescing background queue —
    # results track edits without waiting for a save.
    ctx._get_parse(uri)
    if _diagnostics_enabled():
        _schedule_full_diagnostics(uri)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(ls: LanguageServer, params: types.DidSaveTextDocumentParams):
    uri = params.text_document.uri
    _log.info("didSave: %s", _short_uri(uri))
    if not _diagnostics_enabled():
        LSP_SERVER.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(uri=uri, diagnostics=[])
        )
        return
    # Edits already flow through didChange, so a save re-runs the full pass
    # only when the user opted in *and* the content actually changed since.
    if _full_diagnostics_on_save() and full_diagnostics_needed(uri):
        _schedule_full_diagnostics(uri)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: LanguageServer, params: types.DidCloseTextDocumentParams):
    uri = params.text_document.uri
    _log.info("didClose: %s", _short_uri(uri))
    forget_document(uri)
    with _cache_lock:
        _parse_cache.pop(uri, None)
    ctx._workspace_index.remove_file(uri)
    if _diagnostics_enabled():
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
        change_path = ctx._path_from_uri(change.uri)
        if os.path.basename(change_path) == _FORMAT_CONFIG_FILENAME:
            _refresh_format_config()
            continue
        is_rpy = change_path.endswith((".rpy", ".rpym"))
        if change.type == types.FileChangeType.Created:
            if is_rpy:
                ctx._workspace_index.add_file(change_path)
            else:
                # Might be an image file — invalidate image cache
                ctx._image_cache.clear()
        elif change.type == types.FileChangeType.Deleted:
            if is_rpy:
                ctx._workspace_index.remove_file_from_list(change_path)
                ctx._workspace_index.remove_file(change.uri)
                with _cache_lock:
                    _parse_cache.pop(change.uri, None)
            else:
                ctx._image_cache.clear()
        elif change.type == types.FileChangeType.Changed:
            # An external change — evict the old cache entry so next access re-reads.
            with _cache_lock:
                _parse_cache.pop(change.uri, None)
            ctx._workspace_index.remove_file(change.uri)

# ─────────────────────── Document Symbols ────────────────────────────────


@LSP_SERVER.feature(types.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
def document_symbols(
    ls: LanguageServer, params: types.DocumentSymbolParams
) -> List[types.DocumentSymbol]:
    _log.debug("documentSymbol: %s", _short_uri(params.text_document.uri))
    ast, parser = ctx._get_parse(params.text_document.uri)
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
    ast, parser = ctx._get_parse(params.text_document.uri)
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
    ast, parser = ctx._get_parse(uri)

    # ── 0) If cursor is on a label/screen definition, show all usages ──
    lineno = pos.line + 1  # 1-based
    nodes = _find_nodes_at_line(parser, lineno)
    for node in nodes:
        # Label definition → show all jump/call usages
        if isinstance(node, Label):
            results: List[types.Location] = []
            seen: set = set()  # (uri, lineno) to dedupe
            label_name = node.name
            for fp in ctx._get_workspace_rpy_files():
                file_uri, file_ast, file_parser = ctx._get_parse_for_file(fp)
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
            for fp in ctx._get_workspace_rpy_files():
                file_uri, file_ast, file_parser = ctx._get_parse_for_file(fp)
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
                for n in file_parser._collect(file_ast, HideScreen):
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

    # ── 1.5) Translation navigation: dialogue line ⇄ tl entry ──
    trans_locs = translation.find_translation_jump(uri, pos.line, parser)
    if trans_locs:
        return trans_locs

    # ── 2) Quoted string → file path ──
    quoted = _extract_quoted_string(line_text, col)
    if quoted:
        resolved = ctx._resolve_renpy_file(quoted, source_uri=uri)
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
    all_labels = ctx._get_all_workspace_labels()
    if word in all_labels:
        return _dedup_locations(
            [_make_node_location(u, lb) for u, lb in all_labels[word]]
        )

    # Defines / Defaults
    all_defines = ctx._get_all_workspace_defines()
    if word in all_defines:
        return _dedup_locations(
            [_make_node_location(u, d) for u, d in all_defines[word]]
        )
    all_defaults = ctx._get_all_workspace_defaults()
    if word in all_defaults:
        return _dedup_locations(
            [_make_node_location(u, d) for u, d in all_defaults[word]]
        )

    # Screens
    all_screens = ctx._get_all_workspace_screens()
    if word in all_screens:
        return _dedup_locations(
            [_make_node_location(u, s) for u, s in all_screens[word]]
        )

    # Images
    all_images = ctx._get_all_workspace_images()
    if word in all_images:
        return _dedup_locations(
            [_make_node_location(u, img) for u, img in all_images[word]]
        )

    # Transforms
    all_transforms = ctx._get_all_workspace_transforms()
    if word in all_transforms:
        return _dedup_locations(
            [_make_node_location(u, t) for u, t in all_transforms[word]]
        )

    # Python variables (defined in python: blocks or $ one-liners)
    py_vars = ctx._find_python_var_across_workspace(word)
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
        all_images = ctx._get_all_workspace_images()
        if image_name in all_images:
            locs = [_make_node_location(u, img) for u, img in all_images[image_name]]
            # If the image definition has a file expression, also add that
            for target_uri, img in all_images[image_name]:
                if img.expression:
                    file_path = _try_extract_path(img.expression)
                    if file_path:
                        resolved = ctx._resolve_renpy_file(file_path, source_uri=source_uri)
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
        resolved = ctx._resolve_image_name_to_file(image_name)
        if resolved:
            return _make_file_location(resolved)
        return None

    # ── Call Screen / Show Screen / Hide Screen → screen definition ──
    if isinstance(node, (CallScreen, ShowScreen, HideScreen)):
        all_screens = ctx._get_all_workspace_screens()
        sname = node.screen_name.strip()
        if sname in all_screens:
            return [_make_node_location(u, s) for u, s in all_screens[sname]]
        return None

    # ── Voice → voice file ──
    if isinstance(node, Voice):
        resolved = ctx._resolve_renpy_file(node.filename, source_uri=source_uri)
        if resolved:
            return _make_file_location(resolved)
        return None

    # ── Play / Queue → audio file or audio define ──
    if isinstance(node, PlayMusic):
        filename = node.filename.strip()
        if filename:
            resolved = ctx._resolve_renpy_file(filename, source_uri=source_uri)
            if resolved:
                return _make_file_location(resolved)
        # filename might be a define name (e.g.  play music OldTime)
        all_defines = ctx._get_all_workspace_defines()
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
            resolved = ctx._resolve_renpy_file(filename, source_uri=source_uri)
            if resolved:
                return _make_file_location(resolved)
        # filename might be a define name
        all_defines = ctx._get_all_workspace_defines()
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
            resolved = ctx._resolve_renpy_file(file_path, source_uri=source_uri)
            if resolved:
                return _make_file_location(resolved)
        return None

    return None





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
    line_prefix = line_text[:col]
    _log.debug(
        "completion: %s L%d prefix=%r", _short_uri(uri), pos.line + 1, line_prefix[-30:]
    )

    _ast, parser = ctx._get_parse(uri)
    items = _completion_items_for_context(uri, parser, doc.lines, pos.line, col)

    _log.debug("completion: %s → %d item(s)", _short_uri(uri), len(items))
    return types.CompletionList(is_incomplete=False, items=items)


# ─────────────────────── Code Actions ────────────────────────────────────


@LSP_SERVER.feature(types.TEXT_DOCUMENT_CODE_ACTION)
def code_action(
    ls: LanguageServer, params: types.CodeActionParams
) -> Optional[List[types.CodeAction]]:
    """Quick fixes driven by the diagnostics VS Code sends in the context."""
    uri = params.text_document.uri
    doc = ls.workspace.get_text_document(uri)
    _ast, parser = _get_parse(uri)
    actions = code_actions.compute_code_actions(
        uri, params.context.diagnostics, doc.lines, parser
    )
    _log.debug("codeAction: %s → %d action(s)", _short_uri(uri), len(actions))
    return actions or None


# ─────────────────────── Signature Help ──────────────────────────────────


@LSP_SERVER.feature(
    types.TEXT_DOCUMENT_SIGNATURE_HELP,
    types.SignatureHelpOptions(trigger_characters=["(", ",", " "]),
)
def signature_help(
    ls: LanguageServer, params: types.SignatureHelpParams
) -> Optional[types.SignatureHelp]:
    uri = params.text_document.uri
    doc = ls.workspace.get_text_document(uri)
    line_text = (
        doc.lines[params.position.line]
        if params.position.line < len(doc.lines)
        else ""
    )
    col = _utf16_col_to_utf32(line_text, params.position.character)
    result = signatures.compute_signature_help(doc.lines, params.position.line, col)
    _log.debug(
        "signatureHelp: %s L%d → %s",
        _short_uri(uri),
        params.position.line + 1,
        "sig" if result else "none",
    )
    return result


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

    ast, parser = ctx._get_parse(uri)

    # 2) Check labels across workspace
    all_labels = ctx._get_all_workspace_labels()
    if word in all_labels:
        target_uri, lb = all_labels[word][0]
        fname = os.path.basename(ctx._path_from_uri(target_uri))
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
    all_defines = ctx._get_all_workspace_defines()
    if word in all_defines:
        target_uri, d = all_defines[word][0]
        fname = os.path.basename(ctx._path_from_uri(target_uri))
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown,
                value=f"**define** `{d.name}` = `{d.expression}`\n\n`{fname}` line {d.lineno}",
            )
        )

    # 4) Check defaults across workspace
    all_defaults = ctx._get_all_workspace_defaults()
    if word in all_defaults:
        target_uri, d = all_defaults[word][0]
        fname = os.path.basename(ctx._path_from_uri(target_uri))
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown,
                value=f"**default** `{d.name}` = `{d.expression}`\n\n`{fname}` line {d.lineno}",
            )
        )

    # 5) Check screens across workspace
    all_screens = ctx._get_all_workspace_screens()
    if word in all_screens:
        target_uri, s = all_screens[word][0]
        fname = os.path.basename(ctx._path_from_uri(target_uri))
        params_str = f"({s.parameters})" if s.parameters else "()"
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown,
                value=f"**screen** `{s.name}{params_str}`\n\n`{fname}` lines {s.lineno}–{s.end_lineno}",
            )
        )

    # 6) Check Python variables (defined in python: blocks or $ one-liners)
    py_vars = ctx._find_python_var_across_workspace(word)
    if py_vars:
        uri_v, lineno_v, code_v = py_vars[0]
        fname = os.path.basename(ctx._path_from_uri(uri_v))
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


def _strip_comment(text: str) -> str:
    """Return *text* with any trailing ``#`` comment removed (string-aware)."""
    quote: Optional[str] = None
    escaped = False
    for i, ch in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "#":
            return text[:i]
    return text


def _line_is_block_opener(stripped: str) -> bool:
    """True if *stripped* opens an indented block, i.e. its code (ignoring
    strings and trailing comments) ends with ``:``."""
    return _strip_comment(stripped).rstrip().endswith(":")


def _first_word(stripped: str) -> str:
    """Return the leading word of a statement ("" when it starts with a
    non-word character such as a quote)."""
    m = re.match(r"\w+", stripped, re.UNICODE)
    return m.group(0) if m else ""


# Block-opening statements whose body is NOT Ren'Py script flow (dialogue
# never appears inside them).
_NON_SCRIPT_OPENERS = frozenset(
    {
        "python",
        "init",
        "screen",
        "transform",
        "style",
        "image",
        "layeredimage",
        "predict",
        "channel",
    }
)


def _is_say_line(stripped: str) -> bool:
    """Detect dialogue/narration lines for ``betweenSay`` blank-line insertion.

    Matches ``character "..."`` and bare narration ``"..."``, but not block
    openers such as menu options (``"choice":``) nor ``extend`` continuations.
    """
    if not stripped:
        return False
    if _strip_comment(stripped).rstrip().endswith(":"):
        return False
    if stripped[0] in "\"'`":
        return True
    m = _SAY_SPACE_RE.match(stripped)
    return bool(m) and m.group(1) != "extend"


def _is_extend_line(stripped: str) -> bool:
    """True for ``extend "..."`` continuation lines."""
    m = _SAY_SPACE_RE.match(stripped)
    return bool(m) and m.group(1) == "extend"


# Statement keywords whose name may legally contain '-' (image names, labels,
# screens, ...). For these, the name must not be split like a subtraction.
_NAME_KEYWORDS = (
    "image ",
    "label ",
    "screen ",
    "transform ",
    "style ",
    "define ",
    "default ",
    "init ",
    "show ",
    "scene ",
    "hide ",
    "jump ",
    "call ",
)

# Subset whose name is an *image name*: multiple space-separated components
# are allowed, but a component may not begin with '-'.  For these statements
# a dash surrounded by spaces (``便利店 - 内部``) is invalid Ren'Py and is
# collapsed to ``便利店-内部`` by the formatter.
_IMAGE_NAME_KEYWORDS = frozenset({"image ", "show ", "scene ", "hide "})


def _match_statement_name(text: str) -> Optional[Tuple[int, bool]]:
    """If *text* starts with a statement whose name may contain '-', return a
    ``(name_end, is_image_name)`` tuple where *name_end* is the index at which
    the remainder to normalize starts (or ``len(text)`` when the whole line is
    the name) and *is_image_name* marks image-name statements whose dashes
    must not be surrounded by spaces.  Return ``None`` otherwise.

    Definitions (``image``, ``label``, ``screen``, ``transform``, ``style``,
    ``define``, ``default``, ``init``) keep the name up to the ``:`` or ``=``.
    Statements taking a bare name (``show``, ``scene``, ``hide``, ``jump``,
    ``call``) keep the name region verbatim when it contains a '-'.
    """
    stripped = text.lstrip()
    if not stripped:
        return None
    lower = stripped.lower()
    offset = len(text) - len(stripped)
    for kw in _NAME_KEYWORDS:
        if not lower.startswith(kw):
            continue
        i = offset + len(kw)
        is_image_name = kw in _IMAGE_NAME_KEYWORDS
        if kw in ("show ", "scene ", "hide ", "jump ", "call "):
            first_end = i
            while first_end < len(text) and text[first_end] not in " \t":
                first_end += 1
            if text[i:first_end] == "expression":
                return None
            if "-" not in text[i:]:
                return None
            return len(text), is_image_name
        while i < len(text) and text[i] not in ":=":
            i += 1
        return i, is_image_name
    return None


def _collapse_image_name_dashes(text: str) -> str:
    """Collapse spaces around '-' so an invalid ``image 便利店 - 内部:`` becomes
    the valid ``image 便利店-内部:``.

    Ren'Py splits image names on whitespace into components and forbids any
    component that begins with '-'; a dash meant as an in-component separator
    must therefore never be surrounded by spaces.
    """
    return re.sub(r"\s*-\s*", "-", text)


def _normalize_expression_spacing(text: str) -> str:
    """Normalize common expression spacing outside strings and comments."""
    result: List[str] = []
    i = 0
    quote: Optional[str] = None
    escaped = False
    depth = 0
    stripped = text.lstrip()

    matched = _match_statement_name(text)
    if matched is not None:
        name_end, is_image_name = matched
        prefix = text[:name_end]
        if is_image_name:
            prefix = _collapse_image_name_dashes(prefix)
        result.extend(prefix)
        i = name_end

    def _prev_significant() -> str:
        for elem in reversed(result):
            for ch in reversed(elem):
                if ch not in " \t":
                    return ch
        return ""

    def _next_significant(start: int) -> str:
        j = start
        while j < len(text) and text[j] in " \t":
            j += 1
        return text[j] if j < len(text) else ""

    def _append_spaced_operator(operator: str) -> None:
        while result and result[-1] in " \t":
            result.pop()
        result.append(f" {operator} ")

    def _skip_following_spaces(start: int) -> int:
        j = start
        while j < len(text) and text[j] in " \t":
            j += 1
        return j

    def _can_be_binary_operator(operator: str, prev_ch: str, next_ch: str) -> bool:
        if not prev_ch or not next_ch:
            return False
        if prev_ch in "([{,=:+-*/%<>!":
            return False
        if operator in ("+", "-") and next_ch in ")]},=:+-*/%<>":
            return False
        if operator in ("*", "/", "%") and next_ch in ")]},=*/%<>":
            return False
        return True

    def _can_be_assignment(prev_ch: str, next_ch: str) -> bool:
        if depth != 0 or not prev_ch or not next_ch:
            return False
        if prev_ch in "=!<>:" or next_ch == "=":
            return False
        if stripped.startswith(("$", "define ", "default ", "image ", "init offset")):
            return True
        return False

    while i < len(text):
        ch = text[i]

        if quote:
            result.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue

        if ch in ("'", '"'):
            quote = ch
            result.append(ch)
            i += 1
            continue

        if ch == "#":
            result.append(text[i:])
            break

        if ch in "([{":
            depth += 1
            result.append(ch)
            i += 1
            continue

        if ch in ")]}":
            if depth > 0:
                depth -= 1
            result.append(ch)
            i += 1
            continue

        if ch == ",":
            while result and result[-1] in " \t":
                result.pop()
            next_ch = _next_significant(i + 1)
            result.append("," if not next_ch or next_ch in ")]}" else ", ")
            i = _skip_following_spaces(i + 1)
            continue

        two_char = text[i : i + 2]
        if two_char in ("==", "!=", "<=", ">="):
            prev_ch = _prev_significant()
            next_ch = _next_significant(i + 2)
            if prev_ch and next_ch:
                _append_spaced_operator(two_char)
                i = _skip_following_spaces(i + 2)
                continue

        if ch in "=<>":
            prev_ch = _prev_significant()
            next_ch = _next_significant(i + 1)
            if ch == "=" and _can_be_assignment(prev_ch, next_ch):
                _append_spaced_operator(ch)
                i = _skip_following_spaces(i + 1)
                continue
            if ch in "<>" and prev_ch and next_ch and next_ch != "=":
                _append_spaced_operator(ch)
                i = _skip_following_spaces(i + 1)
                continue

        if ch in "+-*/%":
            prev_ch = _prev_significant()
            next_ch = _next_significant(i + 1)
            if _can_be_binary_operator(ch, prev_ch, next_ch):
                _append_spaced_operator(ch)
                i = _skip_following_spaces(i + 1)
                continue
            # Prefix (unary / splat) operator: tighten it to the following
            # operand, collapsing any stray space so ``* args`` → ``*args`` and
            # ``- 400`` → ``-400``.  When the next significant char is itself
            # an operator/closer (e.g. ``a - -b``) this is a binary operator
            # whose RHS starts with a unary op — leave the space untouched.
            if ch in "+-*" and next_ch and next_ch not in ")]},=:+-*/%<>":
                result.append(ch)
                i = _skip_following_spaces(i + 1)
                continue

        result.append(ch)
        i += 1

    return "".join(result)


def _normalize_binary_star_spacing(text: str) -> str:
    """Normalize spaces around binary * outside strings and comments."""
    return _normalize_expression_spacing(text)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_FORMATTING)
def format_document(ls: LanguageServer, params: types.DocumentFormattingParams):
    if not _formatting_enabled():
        _log.info("formatting: disabled by settings")
        return []

    """Re-indent the document using a consistent indent width.

    Strategy:
      1. Detect the file's current indent unit (e.g. 4 spaces).
      2. For every line compute *indent level* = leading_spaces / unit.
      3. Re-emit each line at ``target_indent * level``.
      4. Blank lines follow the ``blankLines`` mode:
         - ``preserve``   — leave blank lines untouched
         - ``collapse``   — collapse consecutive blank lines into one
         - ``betweenSay`` — like ``collapse``, plus insert one blank line
           between dialogue/narration lines inside label script blocks
         - ``strip``      — remove all blank lines
      5. Strip trailing whitespace.
    """
    _refresh_format_config()
    document = ls.workspace.get_text_document(params.text_document.uri)
    source = document.source
    indent_size = _settings["formatting"]["indentSize"]
    if isinstance(indent_size, int) and indent_size > 0:
        tab_size = indent_size
    else:
        tab_size = params.options.tab_size
    use_spaces: bool = params.options.insert_spaces
    blank_mode: str = str(_settings["formatting"]["blankLines"])
    target_indent = " " * tab_size if use_spaces else "\t"

    _log.info(
        "formatting: %s (tabSize=%d blankLines=%s)",
        _short_uri(params.text_document.uri),
        tab_size,
        blank_mode,
    )

    raw_lines = source.splitlines()
    src_unit = _detect_indent_unit(raw_lines)

    formatted: List[str] = []
    pending_blanks = 0
    # (level, is_script) of each enclosing block opener — used by betweenSay
    # to restrict blank-line insertion to label script flow.
    scope_stack: List[Tuple[int, bool]] = []
    prev_level = 0
    prev_is_dialogue = False
    prev_in_script = False

    for raw in raw_lines:
        stripped = raw.strip()

        # ── blank lines: handled according to the blankLines mode ──
        if not stripped:
            if blank_mode == "preserve":
                formatted.append("")
            elif blank_mode == "strip":
                continue
            else:  # collapse / betweenSay — flush before the next code line
                pending_blanks += 1
            continue

        # ── compute indent level from source ──
        spaces = _leading_spaces(raw)
        level = round(spaces / src_unit) if src_unit else 0

        # ── script-scope tracking (betweenSay only) ──
        in_script = False
        cur_is_say = cur_is_extend = False
        if blank_mode == "betweenSay":
            while scope_stack and scope_stack[-1][0] >= level:
                scope_stack.pop()
            in_script = scope_stack[-1][1] if scope_stack else False
            cur_is_say = _is_say_line(stripped)
            cur_is_extend = _is_extend_line(stripped)

        # ── emit blank lines before this line ──
        if formatted:
            if blank_mode == "collapse":
                if pending_blanks > 0:
                    formatted.append("")
            elif blank_mode == "betweenSay":
                same_scope_level = prev_level == level and prev_in_script and in_script
                if cur_is_extend and prev_is_dialogue and same_scope_level:
                    pass  # extend continues the previous line — never blank
                elif pending_blanks > 0 or (
                    same_scope_level
                    and not cur_is_extend
                    and (prev_is_dialogue or cur_is_say)
                ):
                    formatted.append("")
        pending_blanks = 0

        # ── emit with normalized indent ──
        # Normalize: exactly 1 space between character name and dialog string
        m = _SAY_SPACE_RE.match(stripped)
        if m:
            stripped = m.group(1) + " " + m.group(3)
        stripped = _normalize_expression_spacing(stripped)
        formatted.append(target_indent * level + stripped)

        # ── push block opener & remember line for the next iteration ──
        if blank_mode == "betweenSay":
            if _line_is_block_opener(stripped):
                word = _first_word(stripped)
                if word == "label":
                    scope_stack.append((level, True))
                elif word in _NON_SCRIPT_OPENERS:
                    scope_stack.append((level, False))
                else:
                    scope_stack.append((level, in_script))
            prev_level = level
            prev_is_dialogue = cur_is_say or cur_is_extend
            prev_in_script = in_script

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
    all_labels = ctx._get_all_workspace_labels()
    if word in all_labels:
        # Include the definition(s) if requested
        if params.context.include_declaration:
            for target_uri, lb in all_labels[word]:
                results.append(_make_node_location(target_uri, lb))
        # Find all jump/call references
        for fp in ctx._get_workspace_rpy_files():
            file_uri, ast, parser = ctx._get_parse_for_file(fp)
            for j in parser.get_all_jumps():
                if j.target == word:
                    results.append(_make_node_location(file_uri, j))
            for c in parser.get_all_calls():
                if c.target == word:
                    results.append(_make_node_location(file_uri, c))
        return results if results else None

    # Check if it's a screen
    all_screens = ctx._get_all_workspace_screens()
    if word in all_screens:
        if params.context.include_declaration:
            for target_uri, s in all_screens[word]:
                results.append(_make_node_location(target_uri, s))
        # Find call screen / show screen references
        for fp in ctx._get_workspace_rpy_files():
            file_uri, ast, parser = ctx._get_parse_for_file(fp)
            for node in parser._collect(ast, CallScreen):
                if node.screen_name == word:
                    results.append(_make_node_location(file_uri, node))
            for node in parser._collect(ast, ShowScreen):
                if node.screen_name == word:
                    results.append(_make_node_location(file_uri, node))
            for node in parser._collect(ast, HideScreen):
                if node.screen_name == word:
                    results.append(_make_node_location(file_uri, node))
        return results if results else None

    # Check defines/defaults
    all_defines = ctx._get_all_workspace_defines()
    all_defaults = ctx._get_all_workspace_defaults()
    if word in all_defines or word in all_defaults:
        if params.context.include_declaration:
            if word in all_defines:
                for target_uri, d in all_defines[word]:
                    results.append(_make_node_location(target_uri, d))
            if word in all_defaults:
                for target_uri, d in all_defaults[word]:
                    results.append(_make_node_location(target_uri, d))
        # Text search for usages (simple grep)
        for fp in ctx._get_workspace_rpy_files():
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
    all_labels = ctx._get_all_workspace_labels()
    all_screens = ctx._get_all_workspace_screens()

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
    all_labels = ctx._get_all_workspace_labels()
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
        _jump_uris = set(ctx._workspace_index.get_jump_target_uris(old_name))
        _call_uris = set(ctx._workspace_index.get_call_target_uris(old_name))
        _ref_uris = _jump_uris | _call_uris
        for ref_uri in _ref_uris:
            try:
                file_doc = ls.workspace.get_text_document(ref_uri)
            except Exception:
                continue
            fp = ctx._path_from_uri(ref_uri)
            if not fp:
                continue
            _, ast, parser = ctx._get_parse_for_file(fp)

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
    all_screens = ctx._get_all_workspace_screens()
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
        for fp in ctx._get_workspace_rpy_files():
            file_uri, ast, parser = ctx._get_parse_for_file(fp)
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

            for node in parser._collect(ast, HideScreen):
                if node.screen_name == old_name:
                    if file_uri not in changes:
                        changes[file_uri] = []
                    if node.lineno - 1 < len(file_doc.lines):
                        node_line = file_doc.lines[node.lineno - 1]
                        m = re.search(
                            rf"\bhide\s+screen\s+{re.escape(old_name)}\b", node_line
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
    ctx._workspace_index.rebuild()
    files = ctx._workspace_index.get_file_list()
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
    files = ctx._get_workspace_rpy_files()
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
        _, ast, parser = ctx._get_parse_for_file(fp)

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


# ─────────────────────── Translation Report ──────────────────────────────


@LSP_SERVER.command("renpy.translationReport")
def cmd_translation_report() -> Dict[str, object]:
    """Per-language translation coverage (dialogue + strings blocks)."""
    report = translation.translation_report()
    _log.info(
        "translationReport: %d source lines, %d language(s)",
        report["sourceDialogue"],
        len(report["languages"]),
    )
    return report


# ─────────────────────── Entry Point ─────────────────────────────────────

if __name__ == "__main__":
    _log.info("Starting Ren'Py LSP server (stdio)…")
    LSP_SERVER.start_io()
