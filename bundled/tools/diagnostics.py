"""Diagnostics for the Ren'Py LSP server.

Two tiers:

* light — syntax-only checks on the edited file (parser errors, empty ATL
  blocks).  Runs debounced on every change.
* full — adds cross-workspace reference checks (undefined jump/call labels,
  duplicate definitions, missing image files, unused labels, and undefined
  image/transform/style references).  Runs on open/save via a coalescing
  background queue.

Every diagnostic carries a machine-readable ``code`` so ``code_actions``
can match quick fixes without regex-ing messages.
"""

from __future__ import annotations

import os
import threading
import time as _time
from typing import Dict, List

from lsprotocol import types

from ast_parser import Camera, Hide, RpyParser, Scene, Show
from renpy_data import RENPY_TRANSFORMS, RENPY_BUILTIN_STYLES

import server_context as ctx
from server_context import LSP_SERVER, _log

# Labels that are script entry points — never referenced via jump/call.
ENTRY_LABELS = {"start", "main_menu", "splashscreen", "after_load", "quit"}

# Clauses that the parser's show/scene regex cannot capture and therefore
# end up glued onto the image name — strip them before resolving.
_IMAGE_CLAUSES = (" as ", " onlayer ", " zorder ", " behind ")

# Displayables that Ren'Py provides in every game without an ``image``
# statement (``scene black``, ``show window`` …).
_BUILTIN_IMAGES = {"black", "white", "window"}


def _line_range(lineno: int) -> types.Range:
    return types.Range(
        start=types.Position(line=lineno - 1, character=0),
        end=types.Position(line=lineno - 1, character=999),
    )


def _diag(
    lineno: int,
    message: str,
    severity: "types.DiagnosticSeverity",
    code: str,
    **kwargs,
) -> types.Diagnostic:
    return types.Diagnostic(
        range=_line_range(lineno),
        message=message,
        severity=severity,
        source="renpy-lsp",
        code=code,
        **kwargs,
    )


# ── Light (syntax-only) checks ───────────────────────────────────────────


def _collect_light_diagnostics(parser: RpyParser) -> List[types.Diagnostic]:
    """Syntax checks that need no workspace data: parser errors and
    show/scene/hide statements with a colon but no ATL body."""
    diags: List[types.Diagnostic] = []

    for lineno, msg in parser.errors:
        diags.append(
            _diag(lineno, msg, types.DiagnosticSeverity.Warning, "unknown-statement")
        )

    for node in parser.get_empty_block_errors():
        stmt_type = type(node).__name__.lower()
        diags.append(
            _diag(
                node.lineno,
                f'"{stmt_type}" statement ends with ":" but has no indented block',
                types.DiagnosticSeverity.Error,
                "empty-atl-block",
            )
        )

    return diags


# ── Full (cross-workspace) checks ────────────────────────────────────────


def _check_undefined_labels(uri: str, parser: RpyParser, diags: List) -> None:
    all_labels = ctx._get_all_workspace_labels()
    # Also include labels from the current document (covers the case where
    # the file is outside the workspace folders or URI format differs).
    for lb in parser.get_all_labels():
        if lb.name not in all_labels:
            all_labels[lb.name] = [(uri, lb)]
    for j in parser.get_all_jumps():
        if not j.is_expression and j.target not in all_labels:
            diags.append(
                _diag(
                    j.lineno,
                    f'Label "{j.target}" is not defined in the project',
                    types.DiagnosticSeverity.Warning,
                    "undefined-label",
                )
            )
    for c in parser.get_all_calls():
        if not c.is_expression and c.target not in all_labels:
            diags.append(
                _diag(
                    c.lineno,
                    f'Label "{c.target}" is not defined in the project',
                    types.DiagnosticSeverity.Warning,
                    "undefined-label",
                )
            )


def _check_duplicate_definitions(uri: str, diags: List) -> None:
    """Warn when a label or screen name is defined in more than one place."""
    checks = (
        ("labels", "Label", "duplicate-label"),
        ("screens", "Screen", "duplicate-screen"),
    )
    for getter_name, noun, code in checks:
        getter = getattr(ctx, f"_get_all_workspace_{getter_name}")
        for name, locations in getter().items():
            if len(locations) <= 1:
                continue
            for loc_uri, node in locations:
                if not ctx._same_file_uri(loc_uri, uri):
                    continue
                other_files = [
                    os.path.basename(ctx._path_from_uri(u))
                    for u, n in locations
                    if not ctx._same_file_uri(u, uri) or n.lineno != node.lineno
                ]
                if other_files:
                    diags.append(
                        _diag(
                            node.lineno,
                            f'{noun} "{name}" is also defined in: {", ".join(other_files)}',
                            types.DiagnosticSeverity.Warning,
                            code,
                        )
                    )


def _check_missing_image_files(uri: str, parser: RpyParser, diags: List) -> None:
    """Check that ``image name = "path"`` definitions point at real files."""
    from server_context import _try_extract_path

    for img in parser.get_all_images():
        if not img.expression:
            continue
        file_path = _try_extract_path(img.expression)
        if not file_path:
            continue
        resolved = ctx._resolve_renpy_file(file_path, source_uri=uri)
        if not resolved:
            diags.append(
                _diag(
                    img.lineno,
                    f'Image file not found: "{file_path}"',
                    types.DiagnosticSeverity.Warning,
                    "missing-image-file",
                )
            )


def _check_unused_labels(uri: str, parser: RpyParser, diags: List) -> None:
    all_used_labels = ctx._workspace_index.get_used_labels()
    # Also include the current document's targets — covers the case where
    # the file is opened outside of a workspace folder or the workspace
    # scanner hasn't discovered it yet.
    for j in parser.get_all_jumps():
        if not j.is_expression:
            all_used_labels.add(j.target)
    for c in parser.get_all_calls():
        if not c.is_expression:
            all_used_labels.add(c.target)

    for lb in parser.get_all_labels():
        if lb.name in all_used_labels or lb.name in ENTRY_LABELS:
            continue
        # Skip translation variant labels (contain dots like "label.1")
        if "." in lb.name and lb.name.split(".")[-1].isdigit():
            continue
        diags.append(
            _diag(
                lb.lineno,
                f'Label "{lb.name}" is defined but never used',
                types.DiagnosticSeverity.Hint,
                "unused-label",
                tags=[types.DiagnosticTag.Unnecessary],
            )
        )


def _merge_current_file_symbols(
    workspace_map: Dict[str, list], uri: str, current_nodes: list
) -> Dict[str, list]:
    merged = {name: list(entries) for name, entries in workspace_map.items()}
    for node in current_nodes:
        merged.setdefault(node.name, []).append((uri, node))
    return merged


def _strip_image_clauses(image: str) -> str:
    lowered = image.lower()
    for clause in _IMAGE_CLAUSES:
        idx = lowered.find(clause)
        if idx > 0:
            image = image[:idx]
            lowered = image.lower()
    return image.strip()


def _image_is_known(image: str, merged_images: Dict[str, list]) -> bool:
    tag = image.split()[0] if " " in image else image
    if image in _BUILTIN_IMAGES or tag in _BUILTIN_IMAGES:
        return True
    if image in merged_images:
        return True
    # Attribute lists: ``show eileen vhappy`` is satisfied by any image
    # whose name is the tag ``eileen`` or starts with ``eileen ``.
    for name in merged_images:
        if name == tag or name.startswith(tag + " "):
            return True
    # Ren'Py auto-detection from the images/ directory.
    if ctx._resolve_image_name_to_file(image):
        return True
    return False


def _check_image_references(uri: str, parser: RpyParser, diags: List) -> None:
    """Warn on ``show``/``scene``/``hide`` of images that are neither defined
    via ``image`` statements nor auto-detected under ``images/``."""
    merged = _merge_current_file_symbols(
        ctx._get_all_workspace_images(), uri, parser.get_all_images()
    )
    for cls in (Scene, Show, Hide):
        for node in parser._collect(parser.root, cls):
            raw = (node.image or "").strip()
            if not raw:
                continue  # bare ``scene``
            lowered = raw.lower()
            if lowered.startswith(("expression", "layer ", "text ", '"', "'")):
                continue
            image = _strip_image_clauses(raw)
            if not image or _image_is_known(image, merged):
                continue
            diags.append(
                _diag(
                    node.lineno,
                    f'Image "{image}" is not defined in the project',
                    types.DiagnosticSeverity.Warning,
                    "undefined-image",
                )
            )


def _check_transform_references(uri: str, parser: RpyParser, diags: List) -> None:
    """Warn when an ``at`` clause names a transform that is not defined.

    Acceptable targets: ``transform`` statements, built-in positional
    transforms, ``define``/``default`` names (e.g. ``define zoom =
    Transform(...)``), and any expression containing ``(``/``.`` (inline
    ``Transform(...)``, attribute access) which we do not try to resolve.
    """
    known = set(ctx._get_all_workspace_transforms())
    for t in parser.get_all_transforms():
        known.add(t.name)
    for getter, fallback in (
        (ctx._get_all_workspace_defines, parser.get_all_defines),
        (ctx._get_all_workspace_defaults, parser.get_all_defaults),
    ):
        known.update(getter())
        known.update(d.name for d in fallback())
    builtin = set(RENPY_TRANSFORMS)

    # Collect unknown names first, then resolve them against the workspace
    # with a single batch scan — one full-workspace walk total, not one
    # per unknown name.
    unknown: Dict[str, object] = {}  # name → first node reporting it
    for cls in (Scene, Show, Camera):
        for node in parser._collect(parser.root, cls):
            at = getattr(node, "at_transform", None)
            if not at:
                continue
            for name in at.split(","):
                name = name.strip()
                if not name or "(" in name or "." in name:
                    continue
                if name in known or name in builtin:
                    continue
                unknown.setdefault(name, node)

    if unknown:
        python_names = ctx._all_workspace_python_names()
        for name, node in unknown.items():
            if name in python_names:
                continue
            diags.append(
                _diag(
                    node.lineno,
                    f'Transform "{name}" is not defined in the project',
                    types.DiagnosticSeverity.Warning,
                    "undefined-transform",
                )
            )


def _check_style_references(uri: str, parser: RpyParser, diags: List) -> None:
    """Warn when ``style x is parent`` points at a style that is neither
    defined in the project nor one of Ren'Py's built-in styles."""
    known = set(ctx._get_all_workspace_styles())
    for s in parser.get_all_styles():
        known.add(s.name)
    known |= set(RENPY_BUILTIN_STYLES)

    for s in parser.get_all_styles():
        parent = (s.parent or "").strip()
        if not parent or parent in known:
            continue
        diags.append(
            _diag(
                s.lineno,
                f'Style "{parent}" is not defined in the project',
                types.DiagnosticSeverity.Warning,
                "undefined-style",
            )
        )


def _collect_full_diagnostics(uri: str, parser: RpyParser) -> List[types.Diagnostic]:
    """All diagnostics for *uri*: syntax checks plus cross-workspace checks.

    Pure with respect to the workspace index — callers publish the result.
    """
    diags: List[types.Diagnostic] = []
    diags.extend(_collect_light_diagnostics(parser))
    _check_undefined_labels(uri, parser, diags)
    _check_duplicate_definitions(uri, diags)
    _check_missing_image_files(uri, parser, diags)
    _check_unused_labels(uri, parser, diags)
    _check_image_references(uri, parser, diags)
    _check_transform_references(uri, parser, diags)
    _check_style_references(uri, parser, diags)
    return diags


# ── Publishing ───────────────────────────────────────────────────────────


def _publish_diagnostics_light(uri: str):
    """Fast diagnostics: only current-file syntax checks (no workspace scan).

    Called from ``didChange`` (debounced).  This covers parser errors and
    empty-ATL-block errors — the things the user wants instant feedback on.
    """
    if not ctx._diagnostics_enabled():
        return
    _log.info("_publish_diagnostics_light: %s", ctx._short_uri(uri))
    t0 = _time.monotonic()
    _ast, parser = ctx._get_parse(uri)
    # Update the workspace index for this single file so subsequent
    # queries (hover, goto-def, …) see the latest symbols.
    ctx._workspace_index.update_file(uri)
    diags = _collect_light_diagnostics(parser)

    elapsed = (_time.monotonic() - t0) * 1000
    _log.info(
        "_publish_diagnostics_light: %s → %d diagnostic(s) in %.1f ms",
        ctx._short_uri(uri),
        len(diags),
        elapsed,
    )
    LSP_SERVER.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=diags)
    )


def _publish_diagnostics(uri: str):
    """Full diagnostics: parse the document and push all diagnostics.

    This includes cross-workspace checks (undefined labels, duplicate
    definitions, unused labels, missing image files, undefined
    image/transform/style references).  Called from ``didOpen`` and
    ``didSave``.
    """
    if not ctx._diagnostics_enabled():
        return
    _log.info("_publish_diagnostics: %s", ctx._short_uri(uri))
    t0 = _time.monotonic()
    _ast, parser = ctx._get_parse(uri)
    # Ensure workspace index is up-to-date for the current file
    ctx._workspace_index.update_file(uri)
    diags = _collect_full_diagnostics(uri, parser)

    elapsed = (_time.monotonic() - t0) * 1000
    _log.info(
        "_publish_diagnostics: %s → %d diagnostic(s) in %.1f ms",
        ctx._short_uri(uri),
        len(diags),
        elapsed,
    )
    LSP_SERVER.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=diags)
    )


# ── Background scheduling ────────────────────────────────────────────────

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


def cancel_pending_light_diagnostics(uri: str) -> None:
    """Cancel a pending debounced light-diagnostics run for *uri*, if any."""
    old = _debounce_timers.pop(uri, None)
    if old is not None:
        old.cancel()


def _schedule_full_diagnostics(uri: str) -> None:
    """Queue *uri* for background index update + diagnostics.

    Multiple saves within ``_DIAG_COALESCE_DELAY`` are batched into a single
    diagnostic pass so that e.g. "Format All Files" doesn't spawn N threads.
    """
    if not ctx._diagnostics_enabled():
        return
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
                            ctx._workspace_index.update_file(batch_uri)
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
    if not ctx._diagnostics_enabled():
        return
    cancel_pending_light_diagnostics(uri)

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
