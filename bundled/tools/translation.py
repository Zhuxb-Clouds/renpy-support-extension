"""Translation (``tl/``) support: consistency checks, navigation, reporting.

Ren'Py translation layout this module understands:

* ``translate <lang> strings:`` blocks with ``old "…"`` / ``new "…"``
  pairs (the parser surfaces those as ``Say`` nodes with ``who``
  ``"old"``/``"new"``).
* ``translate <lang> <label>_<hash>:`` blocks — one per source say
  statement, body holds the translated line.
* ``translate <lang> labels:`` blocks renaming labels via old/new.

A translation entry is *stale* when its source no longer exists: the
``old`` string (or the say statement identified by ``<label>_<hash>``)
cannot be found in the non-tl dialogue of the project.
"""

from __future__ import annotations

import hashlib
import os
from typing import Dict, Generator, List, Optional, Tuple, Union

from lsprotocol import types

from ast_parser import (
    If,
    Label,
    Menu,
    MenuItem,
    NarratorSay,
    Node,
    RpyParser,
    Say,
    Translate,
)

import server_context as ctx

# Identifier of the two special block kinds.
_STRINGS_ID = "strings"
_LABELS_ID = "labels"


# ── Translation ID computation (matches renpy.translation) ───────────────


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
    ast, parser = ctx._get_parse(uri)
    if ast is None:
        return None
    return _say_at_line_in(parser, line)


def _say_at_line_in(
    parser: RpyParser, line: int
) -> Optional[Tuple[Union[Say, NarratorSay], Optional[str]]]:
    """:meth:`_find_say_at_line` for an already-parsed document."""
    for node, label_name in _collect_dialogue_with_labels(parser.root.body):
        if node.lineno - 1 == line:
            return node, label_name
    return None


# ── tl file helpers ──────────────────────────────────────────────────────


def is_tl_path(path: str) -> bool:
    """True if *path* lives under a Ren'Py ``tl`` translation directory."""
    parts = os.path.normpath(path).replace("\\", "/").split("/")
    return "tl" in parts[:-1]


def _file_content_hash(uri: str) -> Optional[int]:
    with ctx._cache_lock:
        cached = ctx._parse_cache.get(uri)
    return cached[0] if cached else None


# ── Source dialogue index (non-tl files) ─────────────────────────────────

# uri → (content_hash, [entries]); entry = (id, label, who, what, lineno)
_source_dialogue_cache: Dict[str, Tuple[int, list]] = {}


def _source_dialogue_entries(
    uri: str, parser: RpyParser
) -> List[Tuple[str, Optional[str], Optional[str], str, int]]:
    """Dialogue entries of one non-tl file, keyed on the translation id."""
    content_hash = _file_content_hash(uri)
    if content_hash is None:
        return []
    cached = _source_dialogue_cache.get(uri)
    if cached and cached[0] == content_hash:
        return cached[1]

    entries: List[Tuple[str, Optional[str], Optional[str], str, int]] = []
    for node, label in _collect_dialogue_with_labels(parser.root.body):
        who = node.who if isinstance(node, Say) else None
        entries.append(
            (
                _renpy_translate_id(label, who, node.what),
                label,
                who,
                node.what,
                node.lineno,
            )
        )
    _source_dialogue_cache[uri] = (content_hash, entries)
    return entries


def _source_menu_captions(uri: str, parser: RpyParser) -> List[str]:
    """Menu item captions of one file — those also get ``strings`` entries."""
    content_hash = _file_content_hash(uri)
    if content_hash is None:
        return []
    cached = _source_dialogue_cache.get(uri + "#captions")
    if cached and cached[0] == content_hash:
        return cached[1]
    captions = [
        m.caption for m in parser._collect(parser.root, MenuItem) if m.caption
    ]
    _source_dialogue_cache[uri + "#captions"] = (content_hash, captions)
    return captions


def _iter_source_files() -> List[str]:
    """Filesystem paths of workspace .rpy files that are NOT tl files."""
    result = []
    for fp in ctx._get_workspace_rpy_files():
        if not is_tl_path(fp):
            result.append(fp)
    return result


def _iter_tl_files() -> List[str]:
    """Filesystem paths of workspace .rpy files under ``tl``."""
    result = []
    for fp in ctx._get_workspace_rpy_files():
        if is_tl_path(fp):
            result.append(fp)
    return result


def _collect_source_index() -> Tuple[Dict[str, Tuple[str, int]], set, set]:
    """Aggregate the non-tl workspace: (id → (uri, lineno)), text set, label set.

    Texts feed the ``strings``-block staleness check; labels feed the
    ``labels``-block check.  The current file may not be on disk-indexed
    workspace lists, but its own texts are only needed for *source → tl*
    navigation, which uses the parse cache directly.
    """
    by_id: Dict[str, Tuple[str, int]] = {}
    texts: set = set()
    labels: set = set()
    labels.update(ctx._get_all_workspace_labels())
    for fp in _iter_source_files():
        uri, _ast, parser = ctx._get_parse_for_file(fp)
        for entry in _source_dialogue_entries(uri, parser):
            by_id.setdefault(entry[0], (uri, entry[4]))
            texts.add(entry[3])
        texts.update(_source_menu_captions(uri, parser))
    return by_id, texts, labels


# ── tl block index ───────────────────────────────────────────────────────

# uri → (content_hash, [block dicts])
_tl_block_cache: Dict[str, Tuple[int, list]] = {}


def _cached_parser_for(uri: str) -> Optional[RpyParser]:
    """Parser for *uri* from the parse cache, without touching the LSP
    workspace (which is unavailable before initialization / in tests)."""
    with ctx._cache_lock:
        cached = ctx._parse_cache.get(uri)
    return cached[3] if cached else None


def _tl_blocks(uri: str, parser: Optional[RpyParser] = None) -> List[dict]:
    """Index the ``translate`` blocks of one tl file.

    Each block: ``{"node": Translate, "id": str, "language": str,
    "says": [Say...], "old_new": [(old_say, new_say|None, old_lineno), …]}``.
    """
    content_hash = _file_content_hash(uri)
    if parser is None:
        if content_hash is None:
            return []
        parser = _cached_parser_for(uri)
        if parser is None:
            return []
    # Version key: the content hash when known, else the parser identity
    # (parse-cache-less callers, e.g. tests passing a fresh parser).
    version = content_hash if content_hash is not None else ("p", id(parser))
    cached = _tl_block_cache.get(uri)
    if cached and cached[0] == version:
        return cached[1]

    blocks: List[dict] = []
    for tr in parser._collect(parser.root, Translate):
        says = [n for n in tr.body if isinstance(n, (Say, NarratorSay))]
        pairs: List[Tuple[Say, Optional[Say], int]] = []
        pending_old: Optional[Say] = None
        for node in tr.body:
            if isinstance(node, Say) and node.who == "old":
                if pending_old is not None:
                    pairs.append((pending_old, None, pending_old.lineno))
                pending_old = node
            elif isinstance(node, Say) and node.who == "new":
                if pending_old is not None:
                    pairs.append((pending_old, node, pending_old.lineno))
                    pending_old = None
                else:
                    pairs.append((None, node, node.lineno))
        if pending_old is not None:
            pairs.append((pending_old, None, pending_old.lineno))
        blocks.append(
            {
                "node": tr,
                "id": tr.identifier,
                "language": tr.language,
                "says": says,
                "old_new": pairs,
            }
        )
    _tl_block_cache[uri] = (version, blocks)
    return blocks


# ── Diagnostics ──────────────────────────────────────────────────────────


def check_translations(uri: str, parser: RpyParser, diags: List) -> None:
    """Consistency checks for tl files (no-op for source files)."""
    if not is_tl_path(ctx._path_from_uri(uri)):
        return
    by_id, source_texts, label_names = _collect_source_index()

    from diagnostics import _diag

    for tr in parser._collect(parser.root, Translate):
        if tr.identifier in (_STRINGS_ID, _LABELS_ID):
            valid = label_names if tr.identifier == _LABELS_ID else source_texts
            for old, new, lineno in _pairs_of(uri, tr, parser):
                if old is None:
                    diags.append(
                        _diag(
                            lineno,
                            'Translation "new" has no matching "old"',
                            types.DiagnosticSeverity.Error,
                            "translation-missing-old",
                        )
                    )
                    continue
                if new is None:
                    diags.append(
                        _diag(
                            old.lineno,
                            f'Translation "old" has no "new": {old.what[:60]!r}',
                            types.DiagnosticSeverity.Error,
                            "translation-missing-new",
                        )
                    )
                if old.what not in valid:
                    diags.append(
                        _diag(
                            old.lineno,
                            "Original text not found in the project — "
                            "the source line may have changed",
                            types.DiagnosticSeverity.Warning,
                            "translation-stale",
                        )
                    )
        else:
            # Dialogue block: the identifier must map back to a source say.
            if tr.identifier not in by_id:
                says = [n for n in tr.body if isinstance(n, (Say, NarratorSay))]
                if says:
                    diags.append(
                        _diag(
                            says[0].lineno,
                            "Translation source not found — the original "
                            "line may have changed",
                            types.DiagnosticSeverity.Warning,
                            "translation-stale",
                        )
                    )


def _pairs_of(uri: str, tr: Translate, parser: RpyParser) -> list:
    """old/new pairs of one Translate node in *uri* (from the block index)."""
    for block in _tl_blocks(uri, parser):
        if block["node"] is tr:
            return block["old_new"]
    return []


# ── Navigation ───────────────────────────────────────────────────────────


def _make_loc(uri: str, lineno: int) -> types.Location:
    return types.Location(
        uri=uri,
        range=types.Range(
            start=types.Position(line=lineno - 1, character=0),
            end=types.Position(line=lineno - 1, character=999),
        ),
    )


def _find_tl_targets(
    tid: str, source_what: Optional[str], parser: RpyParser
) -> List[types.Location]:
    """Locate tl entries matching a source say (by id, and/or old text)."""
    targets: List[types.Location] = []
    seen: set = set()
    for fp in _iter_tl_files():
        turi, _ast, tl_parser = ctx._get_parse_for_file(fp)
        for block in _tl_blocks(turi, tl_parser):
            hit = None
            if block["id"] not in (_STRINGS_ID, _LABELS_ID) and block["id"] == tid:
                if block["says"]:
                    hit = block["says"][0]
            elif source_what is not None:
                for old, new, _ln in block["old_new"]:
                    if old is not None and old.what == source_what:
                        hit = new if new is not None else old
                        break
            if hit is not None and (turi, hit.lineno) not in seen:
                seen.add((turi, hit.lineno))
                targets.append(_make_loc(turi, hit.lineno))
    return targets


def _find_source_targets(
    uri: str, parser: RpyParser, tid: Optional[str], old_text: Optional[str]
) -> List[types.Location]:
    """Locate source say statements by translation id or by old text."""
    targets: List[types.Location] = []
    seen: set = set()

    def add(u: str, ln: int) -> None:
        if (u, ln) not in seen:
            seen.add((u, ln))
            targets.append(_make_loc(u, ln))

    if tid is not None:
        by_id, _texts, _labels = _collect_source_index()
        if tid in by_id:
            u, ln = by_id[tid]
            add(u, ln)
            return targets
    if old_text is not None:
        for fp in _iter_source_files():
            src_uri, _ast, src_parser = ctx._get_parse_for_file(fp)
            for node, _label in _collect_dialogue_with_labels(src_parser.root.body):
                if node.what == old_text:
                    add(src_uri, node.lineno)
    return targets


def _enclosing_translate(
    parser: RpyParser, node: Node
) -> Optional[Translate]:
    for tr in parser._collect(parser.root, Translate):
        if tr.lineno <= node.lineno <= tr.end_lineno:
            return tr
    return None


def find_translation_jump(
    uri: str, line0: int, parser: RpyParser
) -> Optional[List[types.Location]]:
    """LSP definition targets for say lines / tl entries (None if n/a)."""
    tl_file = is_tl_path(ctx._path_from_uri(uri))

    # Cursor on the ``translate … :`` header of a dialogue block.
    for tr in parser._collect(parser.root, Translate):
        if tr.lineno - 1 == line0:
            if tl_file and tr.identifier not in (_STRINGS_ID, _LABELS_ID):
                return _find_source_targets(uri, parser, tr.identifier, None)
            return None

    say_hit = _say_at_line_in(parser, line0)
    if say_hit is None:
        return None
    node, label = say_hit

    if not tl_file:
        tid = _renpy_translate_id(label, getattr(node, "who", None), node.what)
        return _find_tl_targets(tid, node.what, parser)

    # tl side → source
    if isinstance(node, Say) and node.who in ("old", "new"):
        tr = _enclosing_translate(parser, node)
        old_text = node.what
        if node.who == "new" and tr is not None:
            for old, new, _ln in _pairs_of(uri, tr, parser):
                if new is node and old is not None:
                    old_text = old.what
                    break
        return _find_source_targets(uri, parser, None, old_text)

    tr = _enclosing_translate(parser, node)
    if tr is not None and tr.identifier not in (_STRINGS_ID, _LABELS_ID):
        return _find_source_targets(uri, parser, tr.identifier, None)
    return None


# ── Coverage report ──────────────────────────────────────────────────────


def translation_report() -> dict:
    """Per-language translation coverage across the workspace."""
    by_id, source_texts, _label_names = _collect_source_index()
    source_count = len(by_id)

    languages: Dict[str, dict] = {}

    def agg(language: str) -> dict:
        return languages.setdefault(
            language,
            {
                "language": language,
                "translatedDialogue": 0,
                "staleDialogue": 0,
                "stringsPairs": 0,
                "staleStrings": 0,
                "missingNew": 0,
                "staleSamples": [],
            },
        )

    for fp in _iter_tl_files():
        turi, _ast, parser = ctx._get_parse_for_file(fp)
        for block in _tl_blocks(turi):
            entry = agg(block["language"])
            if block["id"] in (_STRINGS_ID, _LABELS_ID):
                for old, new, lineno in block["old_new"]:
                    if old is None:
                        entry["missingNew"] += 1
                        continue
                    if new is None:
                        entry["missingNew"] += 1
                    valid = (
                        _label_names if block["id"] == _LABELS_ID else source_texts
                    )
                    entry["stringsPairs"] += 1
                    if old.what not in valid:
                        entry["staleStrings"] += 1
                        if len(entry["staleSamples"]) < 8:
                            entry["staleSamples"].append(
                                {
                                    "file": os.path.basename(fp),
                                    "line": old.lineno,
                                    "old": old.what[:80],
                                }
                            )
            else:
                if block["says"] and block["id"] in by_id:
                    entry["translatedDialogue"] += 1
                elif block["says"]:
                    entry["staleDialogue"] += 1
                    if len(entry["staleSamples"]) < 8:
                        entry["staleSamples"].append(
                            {
                                "file": os.path.basename(fp),
                                "line": block["says"][0].lineno,
                                "id": block["id"],
                            }
                        )

    return {
        "sourceDialogue": source_count,
        "languages": sorted(languages.values(), key=lambda e: e["language"]),
    }
