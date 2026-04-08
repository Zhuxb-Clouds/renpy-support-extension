"""Incrementally maintained workspace index for Ren'Py .rpy/.rpym files.

Instead of re-globbing and re-parsing *all* files on every request, the
index caches file lists and per-file symbol data.  It is updated
incrementally when individual files change.

External dependencies (server instance, parse cache, utility functions)
are injected via the constructor to avoid circular imports.
"""

from __future__ import annotations

import glob
import logging
import os
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import (
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

from ast_parser import (
    Call,
    Default,
    Define,
    ImageDef,
    Jump,
    Label,
    RpyParser,
    Script,
    ScreenDef,
    TransformDef,
)

_log = logging.getLogger("renpy-lsp")


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
    """

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
    ) -> None:
        self._server = server
        self._parse_cache = parse_cache
        self._cache_lock = cache_lock
        self._path_to_uri = path_to_uri
        self._path_from_uri = path_from_uri_fn
        self._normalize_path_key = normalize_path_fn
        self._get_parse_for_file = get_parse_for_file_fn

        self._lock = threading.Lock()
        # Cached file list
        self._rpy_files: Optional[List[str]] = None
        self._files_dirty = True
        # Per-URI symbol indices  {uri: {name: [node, ...]}}
        self._labels: Dict[str, Dict[str, List[Label]]] = {}
        self._defines: Dict[str, Dict[str, List[Define]]] = {}
        self._defaults: Dict[str, Dict[str, List[Default]]] = {}
        self._screens: Dict[str, Dict[str, List[ScreenDef]]] = {}
        self._images: Dict[str, Dict[str, List[ImageDef]]] = {}
        self._transforms: Dict[str, Dict[str, List[TransformDef]]] = {}
        # Per-URI jump/call targets  {uri: set_of_target_names}
        self._jump_targets: Dict[str, set] = {}
        self._call_targets: Dict[str, set] = {}
        # Track which URIs have been indexed and with which content hash
        self._indexed_hashes: Dict[str, int] = {}
        # ── Aggregation cache (invalidated per-store on update_file) ──
        self._agg_cache: Dict[str, dict] = {}
        # ── Pre-warm support ──
        self._ready = threading.Event()
        self._warming = False

    # ── helpers ──

    def _short_uri(self, uri: str) -> str:
        return os.path.basename(self._path_from_uri(uri))

    # ── File list management ──

    def invalidate_file_list(self) -> None:
        """Mark the file list as stale (call on file create/delete)."""
        with self._lock:
            self._files_dirty = True

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
            for pattern in ("**/*.rpy", "**/*.rpym"):
                for fp in glob.glob(os.path.join(root, pattern), recursive=True):
                    norm_key = self._normalize_path_key(fp)
                    if norm_key not in seen:
                        seen.add(norm_key)
                        results.append(os.path.abspath(fp))
        return results

    # ── Incremental index updates ──

    def update_file(self, uri: str) -> None:
        """Re-index a single file (called after parse cache is updated).

        Uses a single-pass AST traversal instead of 8 separate ``_collect``
        calls, and only invalidates the aggregation caches that actually
        changed.
        """
        with self._cache_lock:
            cached = self._parse_cache.get(uri)
        if not cached:
            return
        content_hash, _text, ast, parser = cached
        if self._indexed_hashes.get(uri) == content_hash:
            return  # Already indexed this version

        # ── Single-pass collection of all needed node types ──
        type_map: dict = {
            Label: [],
            Define: [],
            Default: [],
            ScreenDef: [],
            ImageDef: [],
            TransformDef: [],
            Jump: [],
            Call: [],
        }
        parser._collect_multi(ast, type_map)

        labels: Dict[str, List[Label]] = {}
        for lb in type_map[Label]:
            labels.setdefault(lb.name, []).append(lb)
        defines: Dict[str, List[Define]] = {}
        for d in type_map[Define]:
            defines.setdefault(d.name, []).append(d)
        defaults: Dict[str, List[Default]] = {}
        for d in type_map[Default]:
            defaults.setdefault(d.name, []).append(d)
        screens: Dict[str, List[ScreenDef]] = {}
        for s in type_map[ScreenDef]:
            screens.setdefault(s.name, []).append(s)
        images: Dict[str, List[ImageDef]] = {}
        for img in type_map[ImageDef]:
            images.setdefault(img.name, []).append(img)
        transforms: Dict[str, List[TransformDef]] = {}
        for t in type_map[TransformDef]:
            transforms.setdefault(t.name, []).append(t)
        jt: set = {j.target for j in type_map[Jump] if not j.is_expression}
        ct: set = {c.target for c in type_map[Call] if not c.is_expression}

        with self._lock:
            if self._labels.get(uri) != labels:
                self._labels[uri] = labels
                self._agg_cache.pop("labels", None)
            else:
                self._labels[uri] = labels
            if self._defines.get(uri) != defines:
                self._defines[uri] = defines
                self._agg_cache.pop("defines", None)
            else:
                self._defines[uri] = defines
            if self._defaults.get(uri) != defaults:
                self._defaults[uri] = defaults
                self._agg_cache.pop("defaults", None)
            else:
                self._defaults[uri] = defaults
            if self._screens.get(uri) != screens:
                self._screens[uri] = screens
                self._agg_cache.pop("screens", None)
            else:
                self._screens[uri] = screens
            if self._images.get(uri) != images:
                self._images[uri] = images
                self._agg_cache.pop("images", None)
            else:
                self._images[uri] = images
            if self._transforms.get(uri) != transforms:
                self._transforms[uri] = transforms
                self._agg_cache.pop("transforms", None)
            else:
                self._transforms[uri] = transforms
            if self._jump_targets.get(uri) != jt or self._call_targets.get(uri) != ct:
                self._agg_cache.pop("used_labels", None)
            self._jump_targets[uri] = jt
            self._call_targets[uri] = ct
            self._indexed_hashes[uri] = content_hash
        _log.debug("WorkspaceIndex: updated index for %s", self._short_uri(uri))

    def remove_file(self, uri: str) -> None:
        """Remove a file from the index."""
        with self._lock:
            changed = False
            if uri in self._labels:
                del self._labels[uri]
                changed = True
            if uri in self._defines:
                del self._defines[uri]
                changed = True
            if uri in self._defaults:
                del self._defaults[uri]
                changed = True
            if uri in self._screens:
                del self._screens[uri]
                changed = True
            if uri in self._images:
                del self._images[uri]
                changed = True
            if uri in self._transforms:
                del self._transforms[uri]
                changed = True
            self._jump_targets.pop(uri, None)
            self._call_targets.pop(uri, None)
            self._indexed_hashes.pop(uri, None)
            if changed:
                self._agg_cache.clear()

    def ensure_current(self) -> None:
        """Ensure all workspace files are indexed (lazy full rebuild)."""
        need_work = False
        file_list = self.get_file_list()
        for fp in file_list:
            norm_key = self._normalize_path_key(fp)
            with self._cache_lock:
                cached_uri = self._path_to_uri.get(norm_key)
            if cached_uri and self._indexed_hashes.get(cached_uri):
                continue
            need_work = True
            break
        if not need_work:
            return

        for fp in file_list:
            uri, _ast, _parser = self._get_parse_for_file(fp)
            self.update_file(uri)

    def ensure_current_parallel(self) -> None:
        """Like ensure_current but parses files in parallel threads."""
        file_list = self.get_file_list()
        to_parse: List[str] = []
        for fp in file_list:
            norm_key = self._normalize_path_key(fp)
            with self._cache_lock:
                cached_uri = self._path_to_uri.get(norm_key)
            if cached_uri and self._indexed_hashes.get(cached_uri):
                continue
            to_parse.append(fp)

        if not to_parse:
            return

        _log.info(
            "WorkspaceIndex: parallel-parsing %d / %d files",
            len(to_parse),
            len(file_list),
        )
        t0 = _time.monotonic()

        get_parse = self._get_parse_for_file

        def _parse_one(fp: str) -> Tuple[str, Script, RpyParser]:
            return get_parse(fp)

        # Phase 1: parse files in parallel (each fills _parse_cache)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_parse_one, fp): fp for fp in to_parse}
            results = []
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception:
                    _log.exception("Error parsing %s", futures[fut])

        # Phase 2: index sequentially (fast, < 1ms per file, needs lock)
        for uri, _ast, _parser in results:
            self.update_file(uri)

        elapsed = (_time.monotonic() - t0) * 1000
        _log.info(
            "WorkspaceIndex: parallel index complete — %d files in %.0f ms",
            len(to_parse),
            elapsed,
        )

    def warm(self) -> None:
        """Pre-warm the index in the background.  Called once after server init."""
        if self._warming:
            return
        self._warming = True

        def _do_warm():
            try:
                _log.info("WorkspaceIndex: background warm-up starting")
                t0 = _time.monotonic()
                self.ensure_current_parallel()
                elapsed = (_time.monotonic() - t0) * 1000
                _log.info("WorkspaceIndex: warm-up done in %.0f ms", elapsed)
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
            self._labels.clear()
            self._defines.clear()
            self._defaults.clear()
            self._screens.clear()
            self._images.clear()
            self._transforms.clear()
            self._jump_targets.clear()
            self._call_targets.clear()
            self._indexed_hashes.clear()
            self._agg_cache.clear()
        self.ensure_current()

    # ── Query methods (aggregate across all files) ──

    def _aggregate(
        self, store: Dict[str, Dict[str, list]], cache_key: str
    ) -> Dict[str, List[Tuple[str, object]]]:
        """Merge per-URI sub-dicts into {name: [(uri, node), ...]}.

        Results are cached and invalidated selectively by ``update_file``.
        """
        with self._lock:
            cached = self._agg_cache.get(cache_key)
            if cached is not None:
                return cached
            result: dict = {}
            for uri, name_map in store.items():
                for name, nodes in name_map.items():
                    if name not in result:
                        result[name] = []
                    for n in nodes:
                        result[name].append((uri, n))
            self._agg_cache[cache_key] = result
        return result

    def get_labels(self) -> Dict[str, List[Tuple[str, Label]]]:
        self.ensure_current()
        return self._aggregate(self._labels, "labels")

    def get_defines(self) -> Dict[str, List[Tuple[str, Define]]]:
        self.ensure_current()
        return self._aggregate(self._defines, "defines")

    def get_defaults(self) -> Dict[str, List[Tuple[str, Default]]]:
        self.ensure_current()
        return self._aggregate(self._defaults, "defaults")

    def get_screens(self) -> Dict[str, List[Tuple[str, ScreenDef]]]:
        self.ensure_current()
        return self._aggregate(self._screens, "screens")

    def get_images(self) -> Dict[str, List[Tuple[str, ImageDef]]]:
        self.ensure_current()
        return self._aggregate(self._images, "images")

    def get_transforms(self) -> Dict[str, List[Tuple[str, TransformDef]]]:
        self.ensure_current()
        return self._aggregate(self._transforms, "transforms")

    def get_used_labels(self) -> set:
        """Return the set of all label names that are jump/call targets."""
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
            self._agg_cache["used_labels"] = result
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
