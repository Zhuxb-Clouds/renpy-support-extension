"""Ren'Py Language Server — LSP features powered by ast_parser.

Feature handlers live here; shared state and helpers live in
``server_context``, diagnostics in ``diagnostics``, completion in
``completion``, and quick fixes in ``code_actions``.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

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
import api_docs
from translation import _renpy_translate_id, _find_say_at_line

from typing import Dict, List, Optional, Tuple, Union
import time as _time


# ── Runtime settings / cache / workspace index live in server_context. ──


@LSP_SERVER.feature(types.INITIALIZE)
def initialize(ls: LanguageServer, params: types.InitializeParams) -> None:
    ctx._remember_main_loop()
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
    # No parse here: requests parse lazily (cached per content hash), so a
    # burst of keystrokes costs nothing until something needs the AST.  The
    # diagnostics pass follows through the debounced background queue.
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
            ctx.invalidate_asset_caches(change_path)
            if is_rpy:
                ctx._workspace_index.add_file(change_path)
        elif change.type == types.FileChangeType.Deleted:
            ctx.invalidate_asset_caches(change_path)
            if is_rpy:
                ctx._workspace_index.remove_file_from_list(change_path)
                ctx._workspace_index.remove_file(change.uri)
                with _cache_lock:
                    _parse_cache.pop(change.uri, None)
        elif change.type == types.FileChangeType.Changed and is_rpy:
            # The editor buffer is authoritative for open documents (a save
            # also fires this event) — only evict files changed on disk
            # behind our back (git checkout, external editors).
            if _is_open_document(ls, change.uri):
                continue
            with _cache_lock:
                _parse_cache.pop(change.uri, None)
            ctx._workspace_index.remove_file(change.uri)


def _is_open_document(ls: LanguageServer, uri: str) -> bool:
    try:
        open_uris = list(ls.workspace.text_documents.keys())
    except Exception:
        return False
    return any(ctx._same_file_uri(uri, open_uri) for open_uri in open_uris)


# ─────────────────────── Symbol references (index-backed) ───────────────


def _ref_locations(kind: str, name: str) -> List[types.Location]:
    """Locations of every *kind* reference to *name* (exact name ranges)."""
    return [
        types.Location(
            uri=ref_uri,
            range=ctx._make_range(ref_uri, ref.line, ref.col, ref.length),
        )
        for ref_uri, ref in ctx._workspace_index.get_references(kind, name)
    ]


def _resolve_label_word(word: str, parser, line0: int) -> str:
    """Expand a local label reference (``.sub``) to its full name."""
    if not word.startswith("."):
        return word
    current = None
    for lb in parser.get_all_labels():
        if lb.lineno - 1 > line0:
            break
        current = lb.name.split(".", 1)[0]
    return f"{current}{word}" if current else word


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
        # Label definition → show all usages (jump/call, Jump("…") …)
        # Always return here — usages if found, else None.  Never fall
        # through to the general label lookup (step 4) which would return a
        # self-referential definition and cause VS Code to show a
        # source-code preview in hover.
        if isinstance(node, Label):
            return _dedup_locations(_ref_locations("label", node.name)) or None
        # Screen definition → show all call/show/hide screen and use usages
        if isinstance(node, ScreenDef):
            return _dedup_locations(_ref_locations("screen", node.name)) or None

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
    word = _resolve_label_word(word, parser, pos.line)

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
    types.CompletionOptions(
        trigger_characters=[" ", ".", "(", '"', "'"], resolve_provider=True
    ),
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
    trigger = params.context.trigger_character if params.context else None
    items = _completion_items_for_context(
        uri, parser, doc.lines, pos.line, col, trigger_character=trigger
    )

    _log.debug("completion: %s → %d item(s)", _short_uri(uri), len(items))
    return types.CompletionList(is_incomplete=False, items=items)


@LSP_SERVER.feature(types.COMPLETION_ITEM_RESOLVE)
def completion_resolve(
    ls: LanguageServer, item: types.CompletionItem
) -> types.CompletionItem:
    """Attach keyword documentation lazily (only for the focused item)."""
    data = item.data if isinstance(item.data, dict) else None
    doc = KEYWORD_DOCS.get(data.get("doc", "")) if data else None
    if doc is None and data and data.get("api"):
        doc = api_docs.hover_markdown(data["api"])
    if doc:
        item.documentation = types.MarkupContent(
            kind=types.MarkupKind.Markdown, value=doc
        )
    return item


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

    # 0) Media preview for show/scene/image/play/queue/voice lines.
    if word:
        _ast0, parser0 = ctx._get_parse(uri)
        media = _media_preview_hover(uri, parser0, params.position.line, line_text)
        if media is not None:
            return media

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

    # 1b) Dotted engine APIs (config.*, gui.*, renpy.*) — engine docs take
    # precedence over workspace symbols, which cannot define dotted names.
    if "." in word:
        api = api_docs.hover_markdown(word)
        if api:
            return types.Hover(
                contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=api)
            )

    ast, parser = ctx._get_parse(uri)
    word = _resolve_label_word(word, parser, params.position.line)

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

    # 8) Engine API documentation (style properties, displayables, vars).
    api = api_docs.hover_markdown(word)
    if api:
        return types.Hover(
            contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=api)
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
    """Find all references to the label/screen/define/default at cursor."""
    uri = params.text_document.uri
    doc = ls.workspace.get_text_document(uri)
    pos = params.position
    line_text = doc.lines[pos.line] if pos.line < len(doc.lines) else ""
    col = _utf16_col_to_utf32(line_text, pos.character)
    word = _word_at_position(line_text, col)
    _log.info("references: %s L%d word=%r", _short_uri(uri), pos.line + 1, word)
    if not word:
        return None
    _ast, parser = ctx._get_parse(uri)
    word = _resolve_label_word(word, parser, pos.line)
    include_decl = params.context.include_declaration

    results: List[types.Location] = []
    for kind, getter in (
        ("label", ctx._get_all_workspace_labels),
        ("screen", ctx._get_all_workspace_screens),
    ):
        definitions = getter()
        if word in definitions:
            if include_decl:
                results.extend(_make_node_location(u, n) for u, n in definitions[word])
            results.extend(_ref_locations(kind, word))
            return _dedup_ranges(results) or None

    # Defines / defaults: whole-word occurrences, served from the per-file
    # word map (open buffers included) instead of re-reading every file.
    all_defines = ctx._get_all_workspace_defines()
    all_defaults = ctx._get_all_workspace_defaults()
    if word in all_defines or word in all_defaults:
        decl_lines = set()
        for defs in (all_defines.get(word, ()), all_defaults.get(word, ())):
            for def_uri, d in defs:
                decl_lines.add((ctx._normalize_path_key(ctx._path_from_uri(def_uri)), d.lineno))
                if include_decl:
                    results.append(_make_node_location(def_uri, d))
        pattern = re.compile(rf"(?<![\w.]){re.escape(word)}(?!\w)")
        for fp in ctx._get_workspace_rpy_files():
            file_uri, _file_ast, file_parser = ctx._get_parse_for_file(fp)
            line_numbers = ctx._file_word_lines(file_uri).get(word)
            if not line_numbers:
                continue
            norm = ctx._normalize_path_key(fp)
            file_lines = file_parser.lines
            for lineno in sorted(set(line_numbers)):
                if (norm, lineno) in decl_lines or lineno > len(file_lines):
                    continue
                raw = file_lines[lineno - 1]
                for m in pattern.finditer(raw):
                    results.append(
                        types.Location(
                            uri=file_uri,
                            range=ctx._make_range(
                                file_uri, lineno, m.start(), len(word), lines=file_lines
                            ),
                        )
                    )
        return _dedup_ranges(results) or None

    return None


def _dedup_ranges(locations: List[types.Location]) -> List[types.Location]:
    """Remove locations with an identical (file, start position)."""
    seen: set = set()
    result: List[types.Location] = []
    for loc in locations:
        key = (
            ctx._normalize_path_key(ctx._path_from_uri(loc.uri)),
            loc.range.start.line,
            loc.range.start.character,
        )
        if key not in seen:
            seen.add(key)
            result.append(loc)
    return result


# ─────────────────────── Workspace Symbols ───────────────────────────────

_WORKSPACE_SYMBOL_SOURCES = (
    (ctx._get_all_workspace_labels, types.SymbolKind.Function),
    (ctx._get_all_workspace_screens, types.SymbolKind.Function),
    (ctx._get_all_workspace_transforms, types.SymbolKind.Function),
    (ctx._get_all_workspace_images, types.SymbolKind.Variable),
    (ctx._get_all_workspace_styles, types.SymbolKind.Class),
    (ctx._get_all_workspace_defines, types.SymbolKind.Constant),
    (ctx._get_all_workspace_defaults, types.SymbolKind.Variable),
)


@LSP_SERVER.feature(types.WORKSPACE_SYMBOL)
def workspace_symbol(
    ls: LanguageServer, params: types.WorkspaceSymbolParams
) -> List[types.SymbolInformation]:
    """Case-insensitive substring search over all workspace symbols
    (labels, screens, transforms, images, styles, defines, defaults)."""
    query = params.query.strip().lower()
    _log.debug("workspaceSymbol: %r", query)
    results: List[types.SymbolInformation] = []
    for getter, kind in _WORKSPACE_SYMBOL_SOURCES:
        try:
            symbols = getter()
        except RuntimeError:
            # Workspace not initialized yet — nothing to search.
            continue
        for name, entries in symbols.items():
            if query and query not in name.lower():
                continue
            target_uri, node = entries[0]
            results.append(
                types.SymbolInformation(
                    name=name,
                    kind=kind,
                    location=_make_node_location(target_uri, node),
                    container_name=os.path.basename(ctx._path_from_uri(target_uri)),
                )
            )
    results.sort(key=lambda s: (s.name.lower(), s.location.uri))
    return results[:500]


# ─────────────────────── Media Preview ───────────────────────────────────

# Media files referenced by show/scene/image defines and play/queue/voice.
_MEDIA_FILE_RE = re.compile(
    r'["\']([^"\']+?\.(?:png|jpg|jpeg|webp|gif|bmp|webm|mp4|mkv|avi|mov'
    r"|ogg|opus|mp3|wav))['\"]",
    re.IGNORECASE,
)
_IMAGE_EXT_RE = re.compile(r"\.(png|jpg|jpeg|webp|gif|bmp)$", re.IGNORECASE)
_AUDIO_EXT_RE = re.compile(r"\.(ogg|opus|mp3|wav)$", re.IGNORECASE)
_VIDEO_EXT_RE = re.compile(r"\.(webm|mp4|mkv|avi|mov)$", re.IGNORECASE)


def _preview_media_command(file_uri: str, kind: str) -> str:
    """Command-link markdown that opens the media preview panel."""
    import json as _json
    import urllib.parse

    args = urllib.parse.quote(_json.dumps([file_uri, kind]))
    label = "▶ Preview video" if kind == "video" else "▶ Preview audio"
    return f"[{label}](command:renpy-lsp.previewMedia?{args})"


def _resolve_image_files(image_name: str, source_uri: str) -> List[str]:
    """Absolute file paths behind an image name (define expression paths)."""
    images = dict(
        (name, entries)
        for name, entries in ctx._get_all_workspace_images().items()
    )
    entries = images.get(image_name)
    if entries is None:
        tag = image_name.split(" ")[0]
        for name, candidate in images.items():
            if name == tag or name.startswith(tag + " "):
                entries = candidate
                break
    if not entries:
        return []

    target_uri, node = entries[0]
    files: List[str] = []
    for m in _MEDIA_FILE_RE.finditer(getattr(node, "expression", "") or ""):
        resolved = ctx._resolve_renpy_file(m.group(1), source_uri=target_uri or source_uri)
        if resolved:
            files.append(resolved)
    return files


def _media_preview_hover(
    uri: str, parser, line0: int, line_text: str
) -> Optional[types.Hover]:
    """Hover with an image preview or an audio/video preview link for
    ``show``/``scene``/``image``/``play``/``queue``/``voice`` lines."""
    if not ctx._settings["previews"]["enabled"]:
        return None
    stripped = line_text.strip()
    if not stripped or stripped.startswith("#"):
        return None

    parts: List[str] = []

    m = re.match(r"(?:show|scene)\s+(.+)$", stripped, re.IGNORECASE)
    if m:
        from diagnostics import _strip_image_clauses

        name = _strip_image_clauses(m.group(1)).strip()
        name = " ".join(name.split())
        for path in _resolve_image_files(name, uri)[:4]:
            if _IMAGE_EXT_RE.search(path):
                parts.append(f"![preview]({Path(path).as_uri()})")
    elif re.match(r"image\s+", stripped, re.IGNORECASE):
        for img in parser.get_all_images():
            if img.lineno - 1 != line0:
                continue
            for fm in _MEDIA_FILE_RE.finditer(img.expression or ""):
                resolved = ctx._resolve_renpy_file(fm.group(1), source_uri=uri)
                if resolved and _IMAGE_EXT_RE.search(resolved):
                    parts.append(f"![preview]({Path(resolved).as_uri()})")
    else:
        m = re.match(r"(?:play|queue|voice)\s+", stripped, re.IGNORECASE)
        if m:
            fm = _MEDIA_FILE_RE.search(stripped)
            resolved = None
            if fm:
                resolved = ctx._resolve_renpy_file(fm.group(1), source_uri=uri)
            else:
                # ``play music <name>`` → audio.<name> define.
                token = re.match(
                    r"(?:play|queue)\s+\w+\s+([\w.]+)", stripped, re.IGNORECASE
                )
                if token and token.group(1).lower() not in ("fadein", "fadeout"):
                    defines = ctx._get_all_workspace_defines()
                    audio_name = token.group(1)
                    entries = defines.get(
                        f"audio.{audio_name}"
                    ) or defines.get(audio_name)
                    if entries:
                        expr = getattr(entries[0][1], "expression", "") or ""
                        fm2 = _MEDIA_FILE_RE.search(expr)
                        if fm2:
                            resolved = ctx._resolve_renpy_file(
                                fm2.group(1), source_uri=entries[0][0]
                            )
            if resolved:
                if _AUDIO_EXT_RE.search(resolved):
                    parts.append(_preview_media_command(Path(resolved).as_uri(), "audio"))
                elif _VIDEO_EXT_RE.search(resolved):
                    parts.append(_preview_media_command(Path(resolved).as_uri(), "video"))

    if not parts:
        return None
    return types.Hover(
        contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value="\n\n".join(parts))
    )





# ─────────────────────── Call Hierarchy ──────────────────────────────────


def _label_hierarchy_item(uri: str, lb) -> types.CallHierarchyItem:
    """Call-hierarchy item for a label definition."""
    _ast, parser = ctx._get_parse(uri)
    return types.CallHierarchyItem(
        name=lb.name,
        kind=types.SymbolKind.Function,
        uri=uri,
        range=types.Range(
            start=types.Position(line=lb.lineno - 1, character=0),
            end=types.Position(
                line=min(max(lb.end_lineno, lb.lineno), len(parser.lines)) - 1,
                character=999,
            ),
        ),
        selection_range=ctx._make_range(
            uri, lb.lineno, getattr(lb, "name_col", 0) or 0, len(lb.name)
        ),
    )


def _label_at(parser, line1: int, name: Optional[str] = None):
    """Label whose block spans *line1* (1-based), optionally by name."""
    best = None
    for lb in parser.get_all_labels():
        if name is not None and lb.name != name:
            continue
        if lb.lineno <= line1 <= max(lb.end_lineno, lb.lineno):
            if best is None or lb.lineno > best.lineno:
                best = lb
    return best


def _enclosing_label(uri: str, line1: int):
    """Label containing *line1* in the file *uri*, if any."""
    try:
        _ast, parser = ctx._get_parse(uri)
    except Exception:
        return None
    return _label_at(parser, line1)


def _iter_jump_call(node):
    """Yield every Jump/Call node in a label's body, nested blocks included."""
    from ast_parser import Call, Jump

    for child in getattr(node, "body", None) or []:
        if isinstance(child, (Jump, Call)) and not child.is_expression:
            yield child
        yield from _iter_jump_call(child)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_PREPARE_CALL_HIERARCHY)
def prepare_call_hierarchy(
    ls: LanguageServer, params: types.CallHierarchyPrepareParams
) -> Optional[List[types.CallHierarchyItem]]:
    """Prepare the label at the cursor (definition or jump/call site)."""
    uri = params.text_document.uri
    doc = ls.workspace.get_text_document(uri)
    pos = params.position
    line_text = doc.lines[pos.line] if pos.line < len(doc.lines) else ""
    col = _utf16_col_to_utf32(line_text, pos.character)
    word = _word_at_position(line_text, col)
    _ast, parser = ctx._get_parse(uri)

    # A word under the cursor resolves to its target label first, so
    # preparing on a ``jump``/``call`` site follows the reference.
    if word:
        word = _resolve_label_word(word, parser, pos.line)
        entries = ctx._get_all_workspace_labels().get(word)
        if entries:
            lb_uri, target = entries[0]
            return [_label_hierarchy_item(lb_uri, target)]
        for lb in parser.get_all_labels():
            if lb.name == word:
                return [_label_hierarchy_item(uri, lb)]
    lb = _label_at(parser, pos.line + 1)
    if lb is not None:
        return [_label_hierarchy_item(uri, lb)]
    return None


@LSP_SERVER.feature(types.CALL_HIERARCHY_INCOMING_CALLS)
def incoming_calls(
    ls: LanguageServer, params: types.CallHierarchyIncomingCallsParams
) -> List[types.CallHierarchyIncomingCall]:
    """Labels that jump/call to *params.item*."""
    label = params.item.name
    groups: Dict[Tuple[str, int], Tuple[object, List[types.Range]]] = {}
    for ref_uri, ref in ctx._workspace_index.get_references("label", label):
        caller = _enclosing_label(ref_uri, ref.line)
        if caller is None:
            continue
        key = (ref_uri, caller.lineno)
        rng = ctx._make_range(ref_uri, ref.line, ref.col, ref.length)
        if key in groups:
            groups[key][1].append(rng)
        else:
            groups[key] = (caller, [rng])
    return [
        types.CallHierarchyIncomingCall(
            from_=_label_hierarchy_item(uri, lb),
            from_ranges=ranges,
        )
        for (uri, _lineno), (lb, ranges) in groups.items()
    ]


@LSP_SERVER.feature(types.CALL_HIERARCHY_OUTGOING_CALLS)
def outgoing_calls(
    ls: LanguageServer, params: types.CallHierarchyOutgoingCallsParams
) -> List[types.CallHierarchyOutgoingCall]:
    """Labels that *params.item* jumps/calls to."""
    uri = params.item.uri
    try:
        _ast, parser = ctx._get_parse(uri)
    except RuntimeError:
        return []
    lb = _label_at(parser, params.item.range.start.line + 1, params.item.name)
    if lb is None:
        return []

    all_labels = ctx._get_all_workspace_labels()
    groups: Dict[Tuple[str, int], Tuple[object, List[types.Range]]] = {}
    for site in _iter_jump_call(lb):
        for target_uri, target in all_labels.get(site.target, ()):
            key = (target_uri, target.lineno)
            rng = ctx._make_range(
                uri, site.lineno, getattr(site, "name_col", 0) or 0, len(site.target)
            )
            if key in groups:
                groups[key][1].append(rng)
            else:
                groups[key] = (target, [rng])
            break
    return [
        types.CallHierarchyOutgoingCall(
            to=_label_hierarchy_item(t_uri, target),
            from_ranges=ranges,
        )
        for (t_uri, _lineno), (target, ranges) in groups.items()
    ]


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


_RE_RENAME_NAME = re.compile(r"^[a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf][\w\u4e00-\u9fff\u3400-\u4dbf]*$")


def _rename_target(ls: LanguageServer, uri: str, position: types.Position):
    """Resolve the label/screen under the cursor.

    Returns ``(kind, full_name, line_text, span)`` where *span* is the
    (start, end) Python column range of the renamable segment — the last
    dotted component, so ``start.sub`` / ``.sub`` rename just ``sub``.
    """
    doc = ls.workspace.get_text_document(uri)
    line_text = doc.lines[position.line] if position.line < len(doc.lines) else ""
    line_text = line_text.rstrip("\r\n")
    col = _utf16_col_to_utf32(line_text, position.character)
    start, end = _word_boundaries(line_text, col)
    word = line_text[start:end]
    if not word:
        return None
    _ast, parser = ctx._get_parse(uri)
    full = _resolve_label_word(word, parser, position.line)
    if full in ctx._get_all_workspace_labels():
        kind = "label"
    elif full in ctx._get_all_workspace_screens():
        kind = "screen"
    else:
        return None
    last_dot = word.rfind(".")
    if last_dot >= 0:
        start += last_dot + 1
    return kind, full, line_text, (start, end)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_PREPARE_RENAME)
def prepare_rename(
    ls: LanguageServer, params: types.PrepareRenameParams
) -> Optional[types.Range]:
    """Check if rename is allowed at the cursor position."""
    uri = params.text_document.uri
    target = _rename_target(ls, uri, params.position)
    _log.info("prepareRename: %s L%d → %s", _short_uri(uri), params.position.line + 1,
              target[1] if target else None)
    if target is None:
        return None
    _kind, _full, line_text, (start, end) = target
    return types.Range(
        start=types.Position(
            line=params.position.line, character=ctx._utf32_col_to_utf16(line_text, start)
        ),
        end=types.Position(
            line=params.position.line, character=ctx._utf32_col_to_utf16(line_text, end)
        ),
    )


def _word_boundaries(line: str, col: int) -> Tuple[int, int]:
    """Return (start, end) positions of the (possibly dotted) name at col."""
    if col >= len(line):
        col = len(line) - 1
    if col < 0:
        return (0, 0)

    def is_name_char(ch: str) -> bool:
        return ch.isalnum() or ch in "_."

    start = col
    while start > 0 and is_name_char(line[start - 1]):
        start -= 1
    end = col
    while end < len(line) and is_name_char(line[end]):
        end += 1
    return (start, end)


@LSP_SERVER.feature(types.TEXT_DOCUMENT_RENAME)
def rename(
    ls: LanguageServer, params: types.RenameParams
) -> Optional[types.WorkspaceEdit]:
    """Rename a label or screen — definitions and every reference
    (statements, ``use``, and ``Jump("…")``-style strings) — workspace-wide.

    For a dotted label only the last component is renamed: ``start.sub`` →
    ``start.new`` and local references ``.sub`` → ``.new``.
    """
    uri = params.text_document.uri
    target = _rename_target(ls, uri, params.position)
    new_segment = params.new_name.strip().lstrip(".")
    if target is None or not _RE_RENAME_NAME.match(new_segment):
        return None
    kind, old_full, _line_text, _span = target
    old_segment = old_full.rsplit(".", 1)[-1]
    _log.info("rename: %s %r → %r", kind, old_full, new_segment)

    edits: Dict[str, List[types.TextEdit]] = {}
    seen: set = set()

    def add_edit(edit_uri: str, lineno: int, col: int, text: str, lines) -> None:
        # *text* is the name as written at (lineno, col): the full name, a
        # local ``.sub`` form, or the bare segment of a definition.
        if not text.endswith(old_segment):
            return
        seg_col = col + len(text) - len(old_segment)
        key = (ctx._normalize_path_key(ctx._path_from_uri(edit_uri)), lineno, seg_col)
        if key in seen:
            return
        seen.add(key)
        edits.setdefault(edit_uri, []).append(
            types.TextEdit(
                range=ctx._make_range(edit_uri, lineno, seg_col, len(old_segment), lines=lines),
                new_text=new_segment,
            )
        )

    definitions = (
        ctx._get_all_workspace_labels() if kind == "label" else ctx._get_all_workspace_screens()
    )
    for def_uri, node in definitions.get(old_full, ()):
        lines = ctx._source_lines(def_uri)
        raw = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
        span = ctx._node_name_span(node, raw)
        if span is not None:
            col, length = span
            add_edit(def_uri, node.lineno, col, raw[col : col + length], lines)

    for ref_uri, ref in ctx._workspace_index.get_references(kind, old_full):
        lines = ctx._source_lines(ref_uri)
        raw = lines[ref.line - 1] if 0 < ref.line <= len(lines) else ""
        add_edit(ref_uri, ref.line, ref.col, raw[ref.col : ref.col + ref.length], lines)

    return types.WorkspaceEdit(changes=edits) if edits else None


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
    ctx.invalidate_asset_caches()

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
