"""Diagnostics for the Ren'Py LSP server.

One full pass = syntax-only checks (parser errors, empty ATL blocks) plus
cross-workspace reference checks (undefined jump/call labels, duplicate
definitions, missing image files, unused labels/defines, and undefined
image/transform/style references).

Triggers:

* ``didOpen`` — repopulates the client (diagnostics are ephemeral, so a
  window reload must re-run the pass).
* ``didChange`` — through a coalescing background queue, so results follow
  edits without waiting for a save.
* ``didSave`` — only when ``diagnostics.fullOnSave`` is enabled *and* the
  content changed since the last pass; otherwise a save re-runs nothing.

Per-check severity overrides (``diagnostics.severity``, including
``"none"`` to suppress a check entirely) are applied when publishing.

Every diagnostic carries a machine-readable ``code`` so ``code_actions``
can match quick fixes without regex-ing messages.
"""

from __future__ import annotations

import os
import threading
import time as _time
from typing import Dict, List, Optional

from lsprotocol import types

from ast_parser import Camera, Hide, RpyParser, Scene, Show
from renpy_data import RENPY_TRANSFORMS, RENPY_BUILTIN_STYLES

import server_context as ctx
from server_context import LSP_SERVER, _log

import translation

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


def _split_top_level(text: str, sep: str = ",") -> List[str]:
    """Split on *sep* only outside parentheses — ``fx_waves(a, b), left``
    must stay one entry instead of yielding ``b)`` / `` left`` fragments."""
    parts: List[str] = []
    current: List[str] = []
    depth = 0
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth <= 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _image_is_known(
    image: str,
    merged_images: Dict[str, list],
    known_tags: "set[str]" = frozenset(),
) -> bool:
    tag = image.split()[0] if " " in image else image
    if image in _BUILTIN_IMAGES or tag in _BUILTIN_IMAGES:
        return True
    if image in known_tags or tag in known_tags:
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
    via ``image`` statements nor auto-detected under ``images/``.

    Tags introduced by ``show X as tag`` count as known targets too —
    ``hide chapter_show_display`` refers to such a tag, not to an image.
    """
    merged = _merge_current_file_symbols(
        ctx._get_all_workspace_images(), uri, parser.get_all_images()
    )
    known_tags = set(ctx._get_all_workspace_show_tags())
    for cls in (Show, Scene):
        for node in parser._collect(parser.root, cls):
            if node.as_tag:
                known_tags.add(node.as_tag)
    for cls in (Scene, Show, Hide):
        for node in parser._collect(parser.root, cls):
            raw = (node.image or "").strip()
            if not raw:
                continue  # bare ``scene``
            lowered = raw.lower()
            if lowered.startswith(
                ("expression", "layer ", "text ", "screen ", '"', "'")
            ):
                continue
            image = _strip_image_clauses(raw)
            if not image or _image_is_known(image, merged, known_tags):
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
            for name in _split_top_level(at):
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


def _check_unused_defines(
    uri: str, parser: RpyParser, diags: List, text: Optional[str] = None
) -> None:
    """Hint on ``define``/``default`` names never used outside their own
    definition line(s).

    Usage is a plain word occurrence in any workspace file (dialogue text
    counts, so false negatives are possible but false positives are not).
    Dotted namespaces and ``_``-prefixed internals are skipped.
    """
    from server_context import _ENGINE_READ_NAMES

    candidates: Dict[str, set] = {}  # name → own definition lines
    for d in parser.get_all_defines() + parser.get_all_defaults():
        name = d.name
        if not name or "." in name or name.startswith("_"):
            continue
        if name in _ENGINE_READ_NAMES:
            continue
        candidates.setdefault(name, set()).add(d.lineno)
    if not candidates:
        return

    # Which names are used outside their definition lines?
    unused = {name: lines for name, lines in candidates.items()}
    file_uris = {uri}  # current file first (covers non-workspace files)
    try:
        workspace_files = ctx._get_workspace_rpy_files()
    except Exception:
        workspace_files = []  # workspace not initialized (e.g. in tests)
    for fp in workspace_files:
        try:
            file_uris.add(ctx._get_parse_for_file(fp)[0])
        except Exception:
            continue
    for file_uri in file_uris:
        if not unused:
            break
        words = ctx._file_word_lines(file_uri)
        if not words and file_uri == uri and text is not None:
            # No cache entry for the current file (e.g. tests) — use the
            # text we were handed instead.
            words = ctx._word_map_from_text(text)
        for name in list(unused):
            own = candidates[name] if file_uri == uri else ()
            for lineno in words.get(name, ()):
                if lineno not in own:
                    unused.pop(name)
                    break

    for d in parser.get_all_defines() + parser.get_all_defaults():
        if d.name in unused:
            diags.append(
                _diag(
                    d.lineno,
                    f'Variable "{d.name}" is defined but never used',
                    types.DiagnosticSeverity.Hint,
                    "unused-define",
                    tags=[types.DiagnosticTag.Unnecessary],
                )
            )


def _collect_full_diagnostics(
    uri: str, parser: RpyParser, text: Optional[str] = None
) -> List[types.Diagnostic]:
    """All diagnostics for *uri*: syntax checks plus cross-workspace checks.

    Pure with respect to the workspace index — callers publish the result.
    *text* is the document source when the parse cache may not hold it.
    Per-check severity overrides (``diagnostics.severity``) are applied.
    """
    diags: List[types.Diagnostic] = []
    diags.extend(_collect_light_diagnostics(parser))
    _check_undefined_labels(uri, parser, diags)
    _check_duplicate_definitions(uri, diags)
    _check_missing_image_files(uri, parser, diags)
    _check_unused_labels(uri, parser, diags)
    _check_unused_defines(uri, parser, diags, text)
    _check_image_references(uri, parser, diags)
    _check_transform_references(uri, parser, diags)
    _check_style_references(uri, parser, diags)
    translation.check_translations(uri, parser, diags)
    return _apply_severity_overrides(diags)


# ── Severity overrides ───────────────────────────────────────────────────

_SEVERITY_ALIASES = {
    "error": types.DiagnosticSeverity.Error,
    "warning": types.DiagnosticSeverity.Warning,
    "information": types.DiagnosticSeverity.Information,
    "hint": types.DiagnosticSeverity.Hint,
}


def _apply_severity_overrides(diags: List[types.Diagnostic]) -> List[types.Diagnostic]:
    """Rewrite severities per ``diagnostics.severity``; drop ``"none"`` ones."""
    overrides = ctx._diagnostic_severity_overrides()
    if not overrides or not diags:
        return diags
    out: List[types.Diagnostic] = []
    for d in diags:
        name = overrides.get(str(d.code) if d.code is not None else "")
        if name is None:
            out.append(d)
            continue
        if name == "none":
            continue  # check suppressed by settings
        severity = _SEVERITY_ALIASES.get(name)
        if severity is not None and severity != d.severity:
            d.severity = severity
        out.append(d)
    return out


# ── Publishing ───────────────────────────────────────────────────────────

# Content hash at the time of the last full-diagnostics publish — lets a
# save skip the pass when nothing changed since (every edit already flows
# through didChange, so an unchanged save is pure repetition).
_last_full_hash: Dict[str, int] = {}


def full_diagnostics_needed(uri: str) -> bool:
    """True when the document content differs from the last full pass."""
    with ctx._cache_lock:
        cached = ctx._parse_cache.get(uri)
    if not cached:
        return True  # never parsed / already closed — nothing to compare
    with _diag_queue_lock:
        return _last_full_hash.get(uri) != cached[0]


def forget_document(uri: str) -> None:
    """Drop bookkeeping for a closed document."""
    with _diag_queue_lock:
        _last_full_hash.pop(uri, None)


def refresh_open_documents() -> None:
    """Re-run full diagnostics for every open ``.rpy`` document — used after
    a settings change so severity overrides apply without reopening files."""
    if not ctx._diagnostics_enabled():
        return
    try:
        documents = list(LSP_SERVER.workspace.text_documents.items())
    except Exception:
        return
    for uri, doc in documents:
        path = getattr(doc, "path", "") or uri
        if path.endswith((".rpy", ".rpym")):
            _schedule_full_diagnostics(uri)


def _publish_diagnostics(uri: str):
    """Full diagnostics: parse the document and push all diagnostics.

    This includes cross-workspace checks (undefined labels, duplicate
    definitions, unused labels, missing image files, undefined
    image/transform/style references).  Called for ``didOpen``, coalesced
    ``didChange`` runs, and content-changing saves.
    """
    if not ctx._diagnostics_enabled():
        return
    _log.info("_publish_diagnostics: %s", ctx._short_uri(uri))
    t0 = _time.monotonic()
    _ast, parser = ctx._get_parse(uri)
    with ctx._cache_lock:
        cached = ctx._parse_cache.get(uri)
    text = cached[1] if cached else None
    # Ensure workspace index is up-to-date for the current file
    ctx._workspace_index.update_file(uri)
    diags = _collect_full_diagnostics(uri, parser, text)

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
    if cached:
        with _diag_queue_lock:
            _last_full_hash[uri] = cached[0]


# ── Background scheduling ────────────────────────────────────────────────

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
