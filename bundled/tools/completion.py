"""Context-aware completion for the Ren'Py LSP server."""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from lsprotocol import types

from ast_parser import Node, RpyParser
from renpy_data import (
    RENPY_KEYWORDS,
    RENPY_TRANSITIONS,
    RENPY_TRANSFORMS,
    RENPY_SCREEN_DISPLAYABLES,
    RENPY_SCREEN_PROPERTIES,
    RENPY_ATL_PROPERTIES,
    RENPY_STYLE_PROPERTIES,
)

import server_context as ctx


# Ren'Py keyword/transition/transform lists are in renpy_data.py.

_COMPLETION_NAME_RE = r"[\w.\-\u4e00-\u9fff\u3400-\u4dbf ]*"
_COMPLETION_IDENTIFIER_RE = r"[\w.\u4e00-\u9fff\u3400-\u4dbf]*"

# Dotted define/default path right before the cursor, with an optional
# partial member after the last dot ("music.", "music.un", "music.sub.x").
_RE_DOTTED_MEMBER_PREFIX = re.compile(
    r"([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf][\w\u4e00-\u9fff\u3400-\u4dbf]*"
    r"(?:\.[\w\u4e00-\u9fff\u3400-\u4dbf]+)*)\.[\w\u4e00-\u9fff\u3400-\u4dbf]*$"
)


def _add_completion_item(
    items: List[types.CompletionItem],
    seen: set,
    label: str,
    kind: types.CompletionItemKind,
    detail: Optional[str] = None,
) -> None:
    """Append a completion item once, preserving first-seen ordering."""
    if not label or label in seen:
        return
    seen.add(label)
    items.append(types.CompletionItem(label=label, kind=kind, detail=detail))


def _add_workspace_symbol_completions(
    items: List[types.CompletionItem],
    seen: set,
    symbols: Dict[str, List[Tuple[str, Node]]],
    *,
    kind: types.CompletionItemKind,
    detail_label: str,
    expression_attr: Optional[str] = None,
) -> None:
    """Append completions from a workspace symbol map."""
    for name in sorted(symbols):
        entries = symbols[name]
        if not entries:
            continue
        target_uri, node = entries[0]
        fname = os.path.basename(ctx._path_from_uri(target_uri))
        detail = f"{detail_label} ({fname}:{node.lineno})"
        if expression_attr:
            expression = getattr(node, expression_attr, None)
            if expression:
                detail = f"{detail_label}: {expression}"
        _add_completion_item(items, seen, name, kind, detail)


def _add_keyword_completions(
    items: List[types.CompletionItem],
    seen: set,
    names: List[str],
    *,
    kind: types.CompletionItemKind,
    detail: Optional[str] = None,
) -> None:
    for name in names:
        _add_completion_item(items, seen, name, kind, detail)


def _current_symbol_map(
    uri: str, nodes: List[Node]
) -> Dict[str, List[Tuple[str, Node]]]:
    symbols: Dict[str, List[Tuple[str, Node]]] = {}
    for node in nodes:
        name = getattr(node, "name", "")
        if name:
            symbols.setdefault(name, []).append((uri, node))
    return symbols


def _merge_current_symbols(
    workspace_symbols: Dict[str, List[Tuple[str, Node]]],
    uri: str,
    nodes: List[Node],
) -> Dict[str, List[Tuple[str, Node]]]:
    merged = {name: list(entries) for name, entries in workspace_symbols.items()}
    for name, entries in _current_symbol_map(uri, nodes).items():
        existing = merged.setdefault(name, [])
        existing_keys = {(entry_uri, node.lineno) for entry_uri, node in existing}
        for entry in entries:
            if (entry[0], entry[1].lineno) not in existing_keys:
                existing.append(entry)
    return merged


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _enclosing_completion_block(lines: List[str], line_no: int, col: int) -> Optional[str]:
    """Return the nearest enclosing screen/transform/style block for completion."""
    if line_no >= len(lines):
        return None
    current_indent = _line_indent(lines[line_no][:col])
    if current_indent <= 0:
        return None

    for i in range(line_no - 1, -1, -1):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = _line_indent(raw)
        if indent >= current_indent:
            continue
        current_indent = indent
        if re.match(r"^screen\s+[a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf]\w*", stripped):
            return "screen"
        if re.match(r"^transform\s+[a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf]\w*", stripped):
            return "transform"
        if re.match(r"^style\s+[a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf]\w*", stripped):
            return "style"

    return None


def _prefix_matches(prefix: str, pattern: str) -> bool:
    return re.match(pattern, prefix, re.IGNORECASE) is not None


def _add_label_completions(
    items: List[types.CompletionItem],
    seen: set,
    uri: str,
    parser: RpyParser,
) -> None:
    labels = _merge_current_symbols(
        ctx._get_all_workspace_labels(), uri, parser.get_all_labels()
    )
    _add_workspace_symbol_completions(
        items,
        seen,
        labels,
        kind=types.CompletionItemKind.Function,
        detail_label="label",
    )


def _add_transform_completions(
    items: List[types.CompletionItem],
    seen: set,
    uri: str,
    parser: RpyParser,
) -> None:
    _add_keyword_completions(
        items,
        seen,
        RENPY_TRANSFORMS,
        kind=types.CompletionItemKind.Constant,
        detail="transform position",
    )
    transforms = _merge_current_symbols(
        ctx._get_all_workspace_transforms(), uri, parser.get_all_transforms()
    )
    _add_workspace_symbol_completions(
        items,
        seen,
        transforms,
        kind=types.CompletionItemKind.Function,
        detail_label="transform",
    )


def _add_screen_completions(
    items: List[types.CompletionItem],
    seen: set,
    uri: str,
    parser: RpyParser,
) -> None:
    screens = _merge_current_symbols(
        ctx._get_all_workspace_screens(), uri, parser.get_all_screens()
    )
    _add_workspace_symbol_completions(
        items,
        seen,
        screens,
        kind=types.CompletionItemKind.Class,
        detail_label="screen",
    )


def _add_style_completions(
    items: List[types.CompletionItem],
    seen: set,
    uri: str,
    parser: RpyParser,
) -> None:
    styles = _merge_current_symbols(
        ctx._get_all_workspace_styles(), uri, parser.get_all_styles()
    )
    _add_workspace_symbol_completions(
        items,
        seen,
        styles,
        kind=types.CompletionItemKind.Property,
        detail_label="style",
    )


def _add_image_completions(
    items: List[types.CompletionItem],
    seen: set,
    uri: str,
    parser: RpyParser,
) -> None:
    images = _merge_current_symbols(
        ctx._get_all_workspace_images(), uri, parser.get_all_images()
    )
    _add_workspace_symbol_completions(
        items,
        seen,
        images,
        kind=types.CompletionItemKind.File,
        detail_label="image",
    )
    ctx._ensure_image_cache()
    explicit_names = {name.lower() for name in images}
    for auto_name in sorted(ctx._image_cache):
        if auto_name.lower() in explicit_names:
            continue
        _add_completion_item(
            items,
            seen,
            auto_name,
            types.CompletionItemKind.File,
            "image (auto)",
        )


def _add_audio_define_completions(
    items: List[types.CompletionItem],
    seen: set,
    uri: str,
    parser: RpyParser,
) -> None:
    defines = _merge_current_symbols(
        ctx._get_all_workspace_defines(), uri, parser.get_all_defines()
    )
    for dname in sorted(defines):
        if not dname.startswith("audio."):
            continue
        entries = defines[dname]
        if not entries:
            continue
        target_uri, node = entries[0]
        fname = os.path.basename(ctx._path_from_uri(target_uri))
        _add_completion_item(
            items,
            seen,
            dname[len("audio.") :],
            types.CompletionItemKind.Variable,
            f"{dname} ({fname}:{node.lineno})",
        )


def _add_namespace_member_completions(
    items: List[types.CompletionItem],
    seen: set,
    uri: str,
    parser: RpyParser,
    namespace: str,
) -> None:
    """Complete members of a dotted define/default namespace.

    ``define music.track1 = "..."`` → typing ``music.`` offers ``track1``.
    Nested namespaces (``music.sub.x``) contribute the intermediate segment
    (``sub``) so completion works level by level.
    """
    prefix = namespace + "."
    namespaces: set = set()
    for getter, fallback_getter, detail_label in (
        (ctx._get_all_workspace_defines, parser.get_all_defines, "define"),
        (ctx._get_all_workspace_defaults, parser.get_all_defaults, "default"),
    ):
        symbols = _merge_current_symbols(getter(), uri, fallback_getter())
        for name in sorted(symbols):
            if not name.startswith(prefix):
                continue
            remainder = name[len(prefix) :]
            if not remainder:
                continue
            if "." in remainder:
                head = remainder.split(".", 1)[0]
                if head not in namespaces:
                    namespaces.add(head)
                    _add_completion_item(
                        items,
                        seen,
                        head,
                        types.CompletionItemKind.Module,
                        f"{detail_label} namespace {prefix}{head}",
                    )
                continue
            entries = symbols[name]
            if not entries:
                continue
            target_uri, node = entries[0]
            fname = os.path.basename(ctx._path_from_uri(target_uri))
            detail = f"{detail_label} {name} ({fname}:{node.lineno})"
            if node.expression:
                detail = f"{detail_label} {name}: {node.expression}"
            _add_completion_item(
                items,
                seen,
                remainder,
                types.CompletionItemKind.Variable,
                detail,
            )


def _add_variable_completions(
    items: List[types.CompletionItem],
    seen: set,
    uri: str,
    parser: RpyParser,
) -> None:
    defines = _merge_current_symbols(
        ctx._get_all_workspace_defines(), uri, parser.get_all_defines()
    )
    defaults = _merge_current_symbols(
        ctx._get_all_workspace_defaults(), uri, parser.get_all_defaults()
    )
    _add_workspace_symbol_completions(
        items,
        seen,
        defines,
        kind=types.CompletionItemKind.Variable,
        detail_label="define",
        expression_attr="expression",
    )
    _add_workspace_symbol_completions(
        items,
        seen,
        defaults,
        kind=types.CompletionItemKind.Variable,
        detail_label="default",
        expression_attr="expression",
    )


def _completion_items_for_context(
    uri: str,
    parser: RpyParser,
    lines: List[str],
    line_no: int,
    col: int,
) -> List[types.CompletionItem]:
    line_text = lines[line_no] if line_no < len(lines) else ""
    prefix = line_text[:col]
    items: List[types.CompletionItem] = []
    seen: set = set()

    # Keep trailing spaces in prefix matching. Removing them breaks trigger
    # contexts like "jump " and "with ".

    # Dotted define/default namespace members first: "music." → "music.*".
    # Falls through to the regular contexts when the namespace is unknown.
    ns_match = _RE_DOTTED_MEMBER_PREFIX.search(prefix)
    if ns_match is not None:
        _add_namespace_member_completions(
            items, seen, uri, parser, ns_match.group(1)
        )
        if items:
            return items

    if _prefix_matches(
        prefix,
        rf"^\s*(?:show|hide|call)\s+screen\s+{_COMPLETION_IDENTIFIER_RE}$",
    ) or _prefix_matches(prefix, rf"^\s*use\s+{_COMPLETION_IDENTIFIER_RE}$"):
        _add_screen_completions(items, seen, uri, parser)
    elif _prefix_matches(
        prefix,
        rf"^\s*(?:jump|call)\s+(?:expression\s+)?{_COMPLETION_IDENTIFIER_RE}$",
    ):
        _add_label_completions(items, seen, uri, parser)
    elif _prefix_matches(prefix, rf"^.*\bwith\s+{_COMPLETION_IDENTIFIER_RE}$"):
        _add_keyword_completions(
            items,
            seen,
            RENPY_TRANSITIONS,
            kind=types.CompletionItemKind.Constant,
            detail="transition",
        )
    elif _prefix_matches(prefix, rf"^.*\bat\s+{_COMPLETION_IDENTIFIER_RE}$"):
        _add_transform_completions(items, seen, uri, parser)
    elif _prefix_matches(
        prefix,
        rf"^\s*style\s+(?:{_COMPLETION_IDENTIFIER_RE}\s+is\s+)?{_COMPLETION_IDENTIFIER_RE}$",
    ):
        _add_style_completions(items, seen, uri, parser)
    elif _prefix_matches(
        prefix,
        rf"^\s*(?:show|scene|hide)\s+{_COMPLETION_NAME_RE}$",
    ) and not re.search(
        r"\b(?:at|with|behind|as|onlayer|zorder)\b",
        prefix,
        re.IGNORECASE,
    ):
        if not _prefix_matches(prefix, r"^\s*(?:show|hide)\s+screen\b"):
            _add_image_completions(items, seen, uri, parser)
    elif _prefix_matches(
        prefix,
        rf"^\s*(?:play|queue)\s+\w+\s+{_COMPLETION_IDENTIFIER_RE}$",
    ):
        _add_audio_define_completions(items, seen, uri, parser)
    else:
        block = _enclosing_completion_block(lines, line_no, col)
        if block == "screen":
            _add_keyword_completions(
                items,
                seen,
                RENPY_SCREEN_DISPLAYABLES,
                kind=types.CompletionItemKind.Keyword,
                detail="screen displayable",
            )
            _add_keyword_completions(
                items,
                seen,
                RENPY_SCREEN_PROPERTIES,
                kind=types.CompletionItemKind.Property,
                detail="screen property",
            )
        elif block == "transform":
            _add_keyword_completions(
                items,
                seen,
                RENPY_ATL_PROPERTIES,
                kind=types.CompletionItemKind.Property,
                detail="ATL statement",
            )
        elif block == "style":
            _add_keyword_completions(
                items,
                seen,
                RENPY_STYLE_PROPERTIES,
                kind=types.CompletionItemKind.Property,
                detail="style property",
            )
        else:
            _add_keyword_completions(
                items,
                seen,
                RENPY_KEYWORDS,
                kind=types.CompletionItemKind.Keyword,
            )
            _add_variable_completions(items, seen, uri, parser)
            _add_label_completions(items, seen, uri, parser)

    return items

