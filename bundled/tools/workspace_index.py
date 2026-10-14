"""Incrementally maintained workspace index for Ren'Py .rpy/.rpym files.

Instead of re-globbing and re-parsing *all* files on every request, the
index caches file lists and per-file symbol data.  It is updated
incrementally when individual files change.

Besides definitions (labels, screens, …) the index stores every
*reference* with its exact position — ``jump``/``call`` targets, ``call``/
``show``/``hide screen`` names, ``use`` statements and screen-action
strings such as ``Jump("x")`` — so find-references, rename and the
"unused" checks are lookups instead of whole-workspace AST walks.

External dependencies (server instance, parse cache, utility functions)
are injected via the constructor to avoid circular imports.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time as _time
from typing import (
    Callable,
    Dict,
    List,
    NamedTuple,
    Optional,
    Tuple,
)

from ast_parser import (
    Call,
    CallScreen,
    Default,
    Define,
    HideScreen,
    ImageDef,
    Jump,
    Label,
    PythonOneliner,
    RpyParser,
    Scene,
    ScreenDef,
    Show,
    ShowScreen,
    StyleDef,
    TransformDef,
    Unknown,
)

_log = logging.getLogger("renpy-lsp")


class Ref(NamedTuple):
    """One reference to a named symbol: ``kind`` is ``"label"`` or
    ``"screen"``; ``line`` is 1-based, ``col`` a 0-based Python index into
    the source line and ``length`` the length of the referencing text (a
    local ``.sub`` is shorter than the resolved ``start.sub``)."""

    kind: str
    name: str
    line: int
    col: int
    length: int


# ``use name`` inside screens (the parser keeps screen bodies as Unknown).
_RE_USE = re.compile(r"^\s*use\s+([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf]\w*)")
# Screen actions / renpy API taking a label or screen name as a string.
_RE_LABEL_STRING = re.compile(
    r"""\b(?:Jump|Call|renpy\.jump|renpy\.call|renpy\.call_in_new_context)"""
    r"""\(\s*(["'])([\w.\u4e00-\u9fff\u3400-\u4dbf]+)\1"""
)
_RE_SCREEN_STRING = re.compile(
    r"""\b(?:Show|ShowMenu|Hide|ToggleScreen|renpy\.show_screen|"""
    r"""renpy\.call_screen|renpy\.hide_screen|renpy\.get_screen)"""
    r"""\(\s*(["'])([\w\u4e00-\u9fff\u3400-\u4dbf]+)\1"""
)


def _ref_from_node(kind: str, name: str, node, lines: List[str]) -> Ref:
    col = node.name_col
    length = node.name_len or len(name)
    if col < 0:
        raw = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
        col = max(raw.find(name), 0)
    return Ref(kind, name, node.lineno, col, length)


def extract_references(parser: RpyParser) -> List[Ref]:
    """Every label/screen reference in *parser*'s tree, in document order."""
    refs: List[Ref] = []
    lines = parser.lines
    for node in parser.iter_nodes():
        if isinstance(node, (Jump, Call)):
            if not node.is_expression and node.target:
                refs.append(_ref_from_node("label", node.target, node, lines))
        elif isinstance(node, (CallScreen, ShowScreen, HideScreen)):
            if node.screen_name:
                refs.append(_ref_from_node("screen", node.screen_name, node, lines))
        elif isinstance(node, (Unknown, PythonOneliner)):
            raw = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
            if not raw:
                continue
            if isinstance(node, Unknown):
                m = _RE_USE.match(raw)
                if m:
                    refs.append(
                        Ref("screen", m.group(1), node.lineno, m.start(1), len(m.group(1)))
                    )
            for regex, kind in (
                (_RE_LABEL_STRING, "label"),
                (_RE_SCREEN_STRING, "screen"),
            ):
                for m in regex.finditer(raw):
                    refs.append(
                        Ref(kind, m.group(2), node.lineno, m.start(2), len(m.group(2)))
                    )
    return refs


# Directories never worth descending into when scanning a workspace.
SKIP_DIRS = frozenset({"node_modules", "__pycache__", "saves", "cache"})


def walk_workspace(root: str):
    """``os.walk`` over *root* that prunes hidden and irrelevant directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
        ]
        yield dirpath, filenames


class WorkspaceIndex:
    """Incrementally maintained index of all workspace .rpy/.rpym files.

    Constructor parameters (dependency injection):
      server              – ``LanguageServer`` instance (for ``workspace.folders``)
      parse_cache         – shared ``{uri: (hash, text, ast, parser)}`` dict
      cache_lock          – ``threading.Lock`` guarding *parse_cache* and *path_to_uri*
      path_to_uri         – shared ``{normalised_path: uri}`` dict
      path_from_uri_fn    – ``(uri) -> filesystem_path``
      normalize_path_fn   – ``(path) -> normalised_path_key``
      get_parse_for_file_fn – ``(filepath) -> (uri, ast, parser)``
      python_defs_fn      – ``(uri, parser, ast) -> {name: [(lineno, code)]}``
                            (optional; Python-level definitions per file)
    """

    # Definition stores: (attribute name, node class)
    _STORES = (
        ("labels", Label),
        ("defines", Define),
        ("defaults", Default),
        ("screens", ScreenDef),
        ("images", ImageDef),
        ("transforms", TransformDef),
        ("styles", StyleDef),
    )

    def __init__(
        self,
        *,
        server: object,
        parse_cache: dict,
        cache_lock: threading.Lock,
        path_to_uri: dict,
        path_from_uri_fn: Callable[[str], str],
        normalize_path_fn: Callable[[str], str],
        get_parse_for_file_fn: Callable,
        python_defs_fn: Optional[Callable] = None,
    ) -> None:
        self._server = server
        self._parse_cache = parse_cache
        self._cache_lock = cache_lock
        self._path_to_uri = path_to_uri
        self._path_from_uri = path_from_uri_fn
        self._normalize_path_key = normalize_path_fn
        self._get_parse_for_file = get_parse_for_file_fn
        self._python_defs = python_defs_fn

        self._lock = threading.RLock()
        # Cached file list
        self._rpy_files: Optional[List[str]] = None
        self._files_dirty = True
        # True once every file in the list has been indexed; reset whenever
        # a file is added or dropped so the next query re-checks the list.
        self._complete = False
        # Per-store, per-URI symbol indices  {store: {uri: {name: [node, ...]}}}
        self._defs: Dict[str, Dict[str, Dict[str, list]]] = {
            store: {} for store, _cls in self._STORES
        }
        # Per-URI references
        self._refs: Dict[str, List[Ref]] = {}
        # Per-URI Python-level definitions {uri: {name: [(lineno, code)]}}
        self._pydefs: Dict[str, Dict[str, List[Tuple[int, str]]]] = {}
        # Per-URI jump/call targets  {uri: set_of_target_names}
        self._jump_targets: Dict[str, set] = {}
        self._call_targets: Dict[str, set] = {}
        # Per-URI tags from ``show X as tag``  {uri: set_of_tags}
        self._show_tags: Dict[str, set] = {}
        # Track which URIs have been indexed and with which content hash
        self._indexed_hashes: Dict[str, int] = {}
        # ── Aggregation cache (invalidated per-store on update_file) ──
        self._agg_cache: Dict[str, object] = {}
        # ── Pre-warm support ──
        self._ready = threading.Event()
        self._warming = False
        # Called with the URI whose *set of defined/referenced names*
        # changed after an edit (not on mere line shifts) — lets open
        # documents refresh cross-file diagnostics.
        self.on_symbols_changed: Optional[Callable[[str], None]] = None

    # Backwards-compatible views of the definition stores.
    @property
    def _labels(self):
        return self._defs["labels"]

    @property
    def _screens(self):
        return self._defs["screens"]

    # ── helpers ──

    def _short_uri(self, uri: str) -> str:
        return os.path.basename(self._path_from_uri(uri))

    # ── File list management ──

    def invalidate_file_list(self) -> None:
        """Mark the file list as stale (call on file create/delete)."""
        with self._lock:
            self._files_dirty = True
            self._complete = False

    def add_file(self, filepath: str) -> None:
        """Incrementally add a file to the cached list (no re-glob)."""
        norm_key = self._normalize_path_key(filepath)
        abs_path = os.path.abspath(filepath)
        with self._lock:
            if self._rpy_files is not None:
                for existing in self._rpy_files:
                    if self._normalize_path_key(existing) == norm_key:
                        return
                self._rpy_files.append(abs_path)
                self._complete = False
                _log.debug("WorkspaceIndex: added file %s", os.path.basename(filepath))

    def remove_file_from_list(self, filepath: str) -> None:
        """Incrementally remove a file from the cached list (no re-glob)."""
        norm_key = self._normalize_path_key(filepath)
        with self._lock:
            if self._rpy_files is not None:
                self._rpy_files = [
                    f
                    for f in self._rpy_files
                    if self._normalize_path_key(f) != norm_key
                ]

    def get_file_list(self) -> List[str]:
        """Return cached workspace .rpy/.rpym files, re-globbing only if dirty."""
        with self._lock:
            if self._files_dirty or self._rpy_files is None:
                self._rpy_files = self._glob_rpy_files()
                self._files_dirty = False
                self._complete = False
                _log.debug(
                    "WorkspaceIndex: re-globbed %d file(s)",
                    len(self._rpy_files),
                )
            return list(self._rpy_files)

    def _glob_rpy_files(self) -> List[str]:
        seen: set = set()
        results: List[str] = []
        for folder in self._server.workspace.folders.values():
            root = self._path_from_uri(folder.uri)
            for dirpath, filenames in walk_workspace(root):
                for fn in filenames:
                    if not fn.endswith((".rpy", ".rpym")):
                        continue
                    fp = os.path.join(dirpath, fn)
                    norm_key = self._normalize_path_key(fp)
                    if norm_key not in seen:
                        seen.add(norm_key)
                        results.append(os.path.abspath(fp))
        return results

    # ── Incremental index updates ──

    def update_file(self, uri: str) -> None:
        """Re-index a single file (called after parse cache is updated).

        Uses a single-pass AST traversal and only invalidates the
        aggregation caches that actually changed.
        """
        with self._cache_lock:
            cached = self._parse_cache.get(uri)
        if not cached:
            return
        content_hash, _text, ast, parser = cached
        if self._indexed_hashes.get(uri) == content_hash:
            return  # Already indexed this version

        # ── Single-pass collection of all needed node types ──
        type_map: dict = {cls: [] for _store, cls in self._STORES}
        type_map.update({Jump: [], Call: [], Show: [], Scene: []})
        parser._collect_multi(ast, type_map)

        new_defs: Dict[str, Dict[str, list]] = {}
        for store, cls in self._STORES:
            by_name: Dict[str, list] = {}
            for node in type_map[cls]:
                by_name.setdefault(node.name, []).append(node)
            new_defs[store] = by_name
        jt: set = {j.target for j in type_map[Jump] if not j.is_expression}
        ct: set = {c.target for c in type_map[Call] if not c.is_expression}
        st: set = {
            n.as_tag
            for cls in (Show, Scene)
            for n in type_map[cls]
            if getattr(n, "as_tag", None)
        }
        refs = extract_references(parser)
        pydefs = self._python_defs(uri, parser, ast) if self._python_defs else {}

        names_changed = False
        with self._lock:
            known_before = uri in self._indexed_hashes
            for store, _cls in self._STORES:
                old = self._defs[store].get(uri)
                new = new_defs[store]
                if old != new:
                    self._agg_cache.pop(store, None)
                    if old is None or old.keys() != new.keys():
                        names_changed = True
                self._defs[store][uri] = new
            old_refs = self._refs.get(uri)
            if old_refs != refs:
                self._agg_cache.pop("refs", None)
                self._agg_cache.pop("used_labels", None)
                old_names = {(r.kind, r.name) for r in old_refs or ()}
                if old_names != {(r.kind, r.name) for r in refs}:
                    names_changed = True
            self._refs[uri] = refs
            if self._jump_targets.get(uri) != jt or self._call_targets.get(uri) != ct:
                self._agg_cache.pop("used_labels", None)
            self._jump_targets[uri] = jt
            self._call_targets[uri] = ct
            if self._show_tags.get(uri) != st:
                self._agg_cache.pop("show_tags", None)
                names_changed = True
            self._show_tags[uri] = st
            old_py = self._pydefs.get(uri)
            if old_py != pydefs:
                self._agg_cache.pop("pydefs", None)
                if old_py is None or old_py.keys() != pydefs.keys():
                    names_changed = True
            self._pydefs[uri] = pydefs
            self._indexed_hashes[uri] = content_hash
        _log.debug("WorkspaceIndex: updated index for %s", self._short_uri(uri))

        callback = self.on_symbols_changed
        if callback and names_changed and (known_before or self._ready.is_set()):
            try:
                callback(uri)
            except Exception:
                _log.exception("WorkspaceIndex: on_symbols_changed failed")

    def remove_file(self, uri: str) -> None:
        """Remove a file from the index."""
        with self._lock:
            changed = False
            for store, _cls in self._STORES:
                if self._defs[store].pop(uri, None) is not None:
                    changed = True
            if self._refs.pop(uri, None) is not None:
                changed = True
            if self._pydefs.pop(uri, None) is not None:
                changed = True
            self._jump_targets.pop(uri, None)
            self._call_targets.pop(uri, None)
            self._show_tags.pop(uri, None)
            self._indexed_hashes.pop(uri, None)
            self._complete = False
            if changed:
                self._agg_cache.clear()

    def ensure_current(self) -> None:
        """Ensure all workspace files are indexed (lazy full rebuild).

        O(1) once the index is complete; the per-file scan only runs after
        the file list changed or a file was dropped from the index.
        """
        if self._complete and not self._files_dirty:
            return
        file_list = self.get_file_list()
        for fp in file_list:
            norm_key = self._normalize_path_key(fp)
            with self._cache_lock:
                cached_uri = self._path_to_uri.get(norm_key)
            if cached_uri and cached_uri in self._indexed_hashes:
                continue
            try:
                uri, _ast, _parser = self._get_parse_for_file(fp)
            except Exception:
                _log.exception("WorkspaceIndex: failed to parse %s", fp)
                continue
            self.update_file(uri)
        with self._lock:
            if not self._files_dirty:
                self._complete = True

    def ensure_current_parallel(self) -> None:
        """Index every workspace file, logging timing.

        Parsing is pure-Python and GIL-bound, so a thread pool brings no
        speed-up — files are parsed sequentially (the warm-up already runs
        on a background thread).
        """
        t0 = _time.monotonic()
        self.ensure_current()
        _log.info(
            "WorkspaceIndex: index complete — %d files in %.0f ms",
            len(self._indexed_hashes),
            (_time.monotonic() - t0) * 1000,
        )

    def warm(self) -> None:
        """Pre-warm the index in the background.  Called once after server init."""
        if self._warming:
            return
        self._warming = True

        def _do_warm():
            try:
                _log.info("WorkspaceIndex: background warm-up starting")
                self.ensure_current_parallel()
            except Exception:
                _log.exception("WorkspaceIndex: warm-up failed")
            finally:
                self._ready.set()
                self._warming = False

        t = threading.Thread(target=_do_warm, daemon=True, name="idx-warm")
        t.start()

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        """Block until the index is ready (or timeout).  Returns True if ready."""
        return self._ready.wait(timeout=timeout)

    def is_ready(self) -> bool:
        """Non-blocking check."""
        return self._ready.is_set()

    def rebuild(self) -> None:
        """Force a full rebuild of the index."""
        with self._lock:
            self._files_dirty = True
            self._complete = False
            for store, _cls in self._STORES:
                self._defs[store].clear()
            self._refs.clear()
            self._pydefs.clear()
            self._jump_targets.clear()
            self._call_targets.clear()
            self._show_tags.clear()
            self._indexed_hashes.clear()
            self._agg_cache.clear()
        self.ensure_current()

    # ── Query methods (aggregate across all files) ──

    def _aggregate(
        self, store: Dict[str, Dict[str, list]], cache_key: str
    ) -> Dict[str, List[Tuple[str, object]]]:
        """Merge per-URI sub-dicts into {name: [(uri, node), ...]}.

        Results are cached and invalidated selectively by ``update_file``.
        Callers must treat the result as read-only.
        """
        with self._lock:
            cached = self._agg_cache.get(cache_key)
            if cached is not None:
                return cached
            result: dict = {}
            for uri, name_map in store.items():
                for name, nodes in name_map.items():
                    bucket = result.setdefault(name, [])
                    for n in nodes:
                        bucket.append((uri, n))
            self._agg_cache[cache_key] = result
        return result

    def _get_store(self, store: str):
        self.ensure_current()
        return self._aggregate(self._defs[store], store)

    def get_labels(self) -> Dict[str, List[Tuple[str, Label]]]:
        return self._get_store("labels")

    def get_defines(self) -> Dict[str, List[Tuple[str, Define]]]:
        return self._get_store("defines")

    def get_defaults(self) -> Dict[str, List[Tuple[str, Default]]]:
        return self._get_store("defaults")

    def get_screens(self) -> Dict[str, List[Tuple[str, ScreenDef]]]:
        return self._get_store("screens")

    def get_images(self) -> Dict[str, List[Tuple[str, ImageDef]]]:
        return self._get_store("images")

    def get_transforms(self) -> Dict[str, List[Tuple[str, TransformDef]]]:
        return self._get_store("transforms")

    def get_styles(self) -> Dict[str, List[Tuple[str, StyleDef]]]:
        return self._get_store("styles")

    def get_python_defs(self) -> Dict[str, List[Tuple[str, int, str]]]:
        """{name: [(uri, lineno, code), ...]} for Python-level definitions
        in ``python:`` blocks and ``$`` lines across the workspace."""
        self.ensure_current()
        with self._lock:
            cached = self._agg_cache.get("pydefs")
            if cached is not None:
                return cached
            result: Dict[str, List[Tuple[str, int, str]]] = {}
            for uri, defs in self._pydefs.items():
                for name, entries in defs.items():
                    bucket = result.setdefault(name, [])
                    for lineno, code in entries:
                        bucket.append((uri, lineno, code))
            self._agg_cache["pydefs"] = result
        return result

    def _ref_map(self) -> Dict[Tuple[str, str], List[Tuple[str, Ref]]]:
        self.ensure_current()
        with self._lock:
            cached = self._agg_cache.get("refs")
            if cached is not None:
                return cached
            result: Dict[Tuple[str, str], List[Tuple[str, Ref]]] = {}
            for uri, refs in self._refs.items():
                for ref in refs:
                    result.setdefault((ref.kind, ref.name), []).append((uri, ref))
            self._agg_cache["refs"] = result
        return result

    def get_references(self, kind: str, name: str) -> List[Tuple[str, Ref]]:
        """All ``(uri, Ref)`` references to the *kind* symbol *name*."""
        return list(self._ref_map().get((kind, name), ()))

    def get_used_labels(self) -> set:
        """Return the set of all label names that are referenced anywhere
        (jump/call targets and ``Jump("…")``-style strings)."""
        self.ensure_current()
        with self._lock:
            cached = self._agg_cache.get("used_labels")
            if cached is not None:
                return set(cached)
            result: set = set()
            for targets in self._jump_targets.values():
                result |= targets
            for targets in self._call_targets.values():
                result |= targets
            for refs in self._refs.values():
                result.update(r.name for r in refs if r.kind == "label")
            self._agg_cache["used_labels"] = result
        return set(result)

    def get_show_tags(self) -> set:
        """Return the set of all tags introduced by ``show X as tag``."""
        self.ensure_current()
        with self._lock:
            cached = self._agg_cache.get("show_tags")
            if cached is not None:
                return set(cached)
            result: set = set()
            for tags in self._show_tags.values():
                result |= tags
            self._agg_cache["show_tags"] = result
        return set(result)

    def get_jump_target_uris(self, label_name: str) -> List[str]:
        """Return URIs of files whose jump targets include *label_name*."""
        with self._lock:
            return [
                uri
                for uri, targets in self._jump_targets.items()
                if label_name in targets
            ]

    def get_call_target_uris(self, label_name: str) -> List[str]:
        """Return URIs of files whose call targets include *label_name*."""
        with self._lock:
            return [
                uri
                for uri, targets in self._call_targets.items()
                if label_name in targets
            ]
