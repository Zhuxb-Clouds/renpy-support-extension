"""Context-aware completion for the Ren'Py LSP server.

Every item carries an explicit ``text_edit`` whose range covers exactly the
text being completed, so names containing spaces, dots, dashes or CJK
characters (``eileen happy``, ``audio.theme``, ``日内-彩票站``) replace the
typed prefix instead of being appended to it.

Nothing is offered inside comments or ordinary strings (dialogue), nor in
the middle of a statement whose context is unknown — the space trigger
character must not pop up keyword lists while writing dialogue.
"""

from __future__ import annotations

import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

from lsprotocol import types

from ast_parser import Node, RpyParser
from renpy_data import (
    KEYWORD_DOCS,
    RENPY_API_MEMBERS,
    RENPY_ATL_PROPERTIES,
    RENPY_CONFIG_MEMBERS,
    RENPY_KEYWORDS,
    RENPY_SCREEN_DISPLAYABLES,
    RENPY_SCREEN_PROPERTIES,
    RENPY_SHOW_CLAUSES,
    RENPY_STYLE_PROPERTIES,
    RENPY_TRANSFORM_CLASSES,
    RENPY_TRANSFORMS,
    RENPY_TRANSITION_CLASSES,
    RENPY_TRANSITIONS,
)

import server_context as ctx


# Ren'Py keyword/transition/transform lists are in renpy_data.py.

_CJK = r"\u4e00-\u9fff\u3400-\u4dbf"
_COMPLETION_NAME_RE = rf"[\w.\-{_CJK} ]*"
_COMPLETION_IDENTIFIER_RE = rf"[\w.{_CJK}]*"

# Dotted define/default path right before the cursor, with an optional
# partial member after the last dot ("music.", "music.un", "music.sub.x").
_RE_DOTTED_MEMBER_PREFIX = re.compile(
    rf"([a-zA-Z_{_CJK}][\w{_CJK}]*"
    rf"(?:\.[\w{_CJK}]+)*)\.[\w{_CJK}]*$"
)

# Characters that make up a completable name (labels may contain dots,
# image names dashes).
_NAME_CHARS = re.compile(rf"[\w.\-{_CJK}]*$")
_MEMBER_CHARS = re.compile(rf"[\w{_CJK}]*$")

# A string argument naming a label / screen, cursor inside the quotes.
_RE_LABEL_STRING_ARG = re.compile(
    r"""\b(?:Jump|Call|renpy\.jump|renpy\.call|renpy\.call_in_new_context)"""
    rf"""\(\s*["']([\w.{_CJK}]*)$"""
)
_RE_SCREEN_STRING_ARG = re.compile(
    r"""\b(?:Show|ShowMenu|Hide|ToggleScreen|renpy\.show_screen|"""
    r"""renpy\.call_screen|renpy\.hide_screen|renpy\.get_screen)"""
    rf"""\(\s*["']([\w{_CJK}]*)$"""
)

# Statements whose remainder is a Python expression.
_RE_EXPRESSION_CONTEXT = re.compile(
    r"^\s*(?:\$|if\b|elif\b|while\b|for\b|return\b"
    r"|(?:define|default)\s+[\w.]+\s*(?:\+|\|)?=)"
)

_RE_TRANSITION_EXPR = re.compile(
    r"^\s*(?:" + "|".join(RENPY_TRANSITION_CLASSES) + r")\s*\("
)
_RE_TRANSFORM_EXPR = re.compile(
    r"^\s*(?:" + "|".join(RENPY_TRANSFORM_CLASSES) + r")\s*\("
)

# Static members of engine namespaces (merged with define/default members).
_STATIC_NAMESPACE_MEMBERS: Dict[str, Tuple[List[str], types.CompletionItemKind]] = {
    "renpy": (RENPY_API_MEMBERS, types.CompletionItemKind.Function),
    "config": (RENPY_CONFIG_MEMBERS, types.CompletionItemKind.Property),
}

# sortText groups: symbols from the current file, then the workspace, then
# engine built-ins.
_SORT_LOCAL = "0"
_SORT_WORKSPACE = "1"
_SORT_BUILTIN = "2"


# ── Item helpers ─────────────────────────────────────────────────────────


def _add_completion_item(
    items: List[types.CompletionItem],
    seen: set,
    label: str,
    kind: types.CompletionItemKind,
    detail: Optional[str] = None,
    sort_group: str = _SORT_WORKSPACE,
    data: Optional[dict] = None,
) -> None:
    """Append a completion item once, preserving first-seen ordering."""
    if not label or label in seen:
        return
    seen.add(label)
    items.append(
        types.CompletionItem(
            label=label,
            kind=kind,
            detail=detail,
            sort_text=f"{sort_group}{label}",
            data=data,
        )
    )


def _add_workspace_symbol_completions(
    items: List[types.CompletionItem],
    seen: set,
    uri: str,
    symbols: Dict[str, List[Tuple[str, Node]]],
    *,
    kind: types.CompletionItemKind,
    detail_label: str,
    expression_attr: Optional[str] = None,
    names: Optional[Iterable[str]] = None,
) -> None:
    """Append completions from a workspace symbol map."""
    for name in sorted(symbols if names is None else names):
        entries = symbols.get(name)
        if not entries:
            continue
        target_uri, node = entries[0]
        local = any(entry_uri == uri for entry_uri, _n in entries)
        fname = os.path.basename(ctx._path_from_uri(target_uri))
        detail = f"{detail_label} ({fname}:{node.lineno})"
        if expression_attr:
            expression = getattr(node, expression_attr, None)
            if expression:
                detail = f"{detail_label}: {expression}"
        _add_completion_item(
            items,
            seen,
            name,
            kind,
            detail,
            _SORT_LOCAL if local else _SORT_WORKSPACE,
        )


def _add_keyword_completions(
    items: List[types.CompletionItem],
    seen: set,
    names: Iterable[str],
    *,
    kind: types.CompletionItemKind,
    detail: Optional[str] = None,
) -> None:
    for name in names:
        data = {"doc": name} if name in KEYWORD_DOCS else None
        _add_completion_item(items, seen, name, kind, detail, _SORT_BUILTIN, data)


def _merge_current_symbols(
    workspace_symbols: Dict[str, List[Tuple[str, Node]]],
    uri: str,
    nodes: List[Node],
) -> Dict[str, List[Tuple[str, Node]]]:
    """Workspace symbols plus *nodes* of the current document.

    The index already tracks open buffers; the merge only matters for
    documents outside the workspace folders.  The workspace map is a shared
    cache, so it is copied (shallowly) rather than mutated.
    """
    merged = dict(workspace_symbols)
    for node in nodes:
        name = getattr(node, "name", "")
        if not name:
            continue
        entries = merged.get(name)
        if entries is None:
            merged[name] = [(uri, node)]
        elif not any(e_uri == uri and e.lineno == node.lineno for e_uri, e in entries):
            merged[name] = list(entries) + [(uri, node)]
    return merged


def _apply_edit_range(
    items: List[types.CompletionItem], line_no: int, line: str, start: int, end: int
) -> List[types.CompletionItem]:
    """Give every item a text edit replacing ``line[start:end]`` on 0-based
    *line_no*."""
    rng_start = ctx._utf32_col_to_utf16(line, start)
    rng_end = ctx._utf32_col_to_utf16(line, end)
    for item in items:
        item.text_edit = types.TextEdit(
            range=types.Range(
                start=types.Position(line=line_no, character=rng_start),
                end=types.Position(line=line_no, character=rng_end),
            ),
            new_text=item.insert_text or item.label,
        )
        if item.filter_text is None:
            item.filter_text = item.label
    return items



# ── Lexical state of the line prefix ─────────────────────────────────────


def _lexical_state(prefix: str) -> str:
    """``"code"``, ``"string"`` or ``"comment"`` at the end of *prefix*."""
    quote: Optional[str] = None
    i = 0
    n = len(prefix)
    while i < n:
        ch = prefix[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch == '"' or ch == "'":
            quote = ch
        elif ch == "#":
            return "comment"
        i += 1
    return "string" if quote is not None else "code"


# ── Context helpers ──────────────────────────────────────────────────────


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _enclosing_completion_block(lines: List[str], line_no: int, col: int) -> Optional[str]:
    """Return the nearest enclosing screen/transform/style/python block."""
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
        if re.match(rf"^screen\s+[a-zA-Z_{_CJK}]\w*", stripped):
            return "screen"
        if re.match(rf"^transform\s+[a-zA-Z_{_CJK}]\w*", stripped):
            return "transform"
        if re.match(rf"^style\s+[a-zA-Z_{_CJK}]\w*", stripped):
            return "style"
        if re.match(r"^(?:init(?:\s+-?\d+)?\s+)?python\b[^:]*:\s*(?:#.*)?$", stripped):
            return "python"
        if indent == 0:
            break

    return None


def _prefix_matches(prefix: str, pattern: str) -> bool:
    return re.match(pattern, prefix, re.IGNORECASE) is not None


def _global_label_at(parser: RpyParser, line_no: int) -> Optional[str]:
    """Name of the global label enclosing 0-based *line_no*."""
    current = None
    for lb in parser.get_all_labels():
        if lb.lineno - 1 > line_no:
            break
        current = lb.name.split(".", 1)[0]
    return current


# ── Context-specific providers ───────────────────────────────────────────


def _add_label_completions(
    items: List[types.CompletionItem],
    seen: set,
    uri: str,
    parser: RpyParser,
    partial: str = "",
    line_no: int = 0,
) -> None:
    labels = _merge_current_symbols(
        ctx._get_all_workspace_labels(), uri, parser.get_all_labels()
    )
    if partial.startswith("."):
        # Local labels of the enclosing global label: ``jump .sub``
        glob = _global_label_at(parser, line_no)
        if glob:
            prefix = glob + "."
            for name in sorted(labels):
                if name.startswith(prefix):
                    _add_completion_item(
                        items,
                        seen,
                        name[len(glob) :],
                        types.CompletionItemKind.Function,
                        f"local label {name}",
                        _SORT_LOCAL,
                    )
        return
    _add_workspace_symbol_completions(
        items,
        seen,
        uri,
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
    transforms = _merge_current_symbols(
        ctx._get_all_workspace_transforms(), uri, parser.get_all_transforms()
    )
    _add_workspace_symbol_completions(
        items,
        seen,
        uri,
        transforms,
        kind=types.CompletionItemKind.Function,
        detail_label="transform",
    )
    # ``define bounce = Transform(...)`` is usable in ``at`` too
    defines = _merge_current_symbols(
        ctx._get_all_workspace_defines(), uri, parser.get_all_defines()
    )
    _add_workspace_symbol_completions(
        items,
        seen,
        uri,
        defines,
        kind=types.CompletionItemKind.Function,
        detail_label="define",
        expression_attr="expression",
        names=[
            n
            for n, entries in defines.items()
            if entries and _RE_TRANSFORM_EXPR.match(entries[0][1].expression or "")
        ],
    )
    _add_keyword_completions(
        items,
        seen,
        RENPY_TRANSFORMS,
        kind=types.CompletionItemKind.Constant,
        detail="transform position",
    )


def _add_transition_completions(
    items: List[types.CompletionItem],
    seen: set,
    uri: str,
    parser: RpyParser,
) -> None:
    defines = _merge_current_symbols(
        ctx._get_all_workspace_defines(), uri, parser.get_all_defines()
    )
    _add_workspace_symbol_completions(
        items,
        seen,
        uri,
        defines,
        kind=types.CompletionItemKind.Constant,
        detail_label="transition",
        expression_attr="expression",
        names=[
            n
            for n, entries in defines.items()
            if entries and _RE_TRANSITION_EXPR.match(entries[0][1].expression or "")
        ],
    )
    _add_keyword_completions(
        items,
        seen,
        RENPY_TRANSITIONS,
        kind=types.CompletionItemKind.Constant,
        detail="transition",
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
        uri,
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
        uri,
        styles,
        kind=types.CompletionItemKind.Property,
        detail_label="style",
    )


def _all_image_names(uri: str, parser: RpyParser) -> Dict[str, str]:
    """Image name → detail, explicit definitions first, then auto images."""
    images = _merge_current_symbols(
        ctx._get_all_workspace_images(), uri, parser.get_all_images()
    )
    result: Dict[str, str] = {}
    for name in sorted(images):
        entries = images[name]
        if entries:
            target_uri, node = entries[0]
            fname = os.path.basename(ctx._path_from_uri(target_uri))
            result[name] = f"image ({fname}:{node.lineno})"
    ctx._ensure_image_cache()
    explicit_lower = {name.lower() for name in result}
    for auto_name in sorted(ctx._image_cache):
        if auto_name.lower() not in explicit_lower:
            result[auto_name] = "image (auto)"
    return result


def _add_image_completions(
    items: List[types.CompletionItem],
    seen: set,
    uri: str,
    parser: RpyParser,
    typed: str = "",
) -> None:
    """Complete an image name word by word.

    Ren'Py image names are a tag plus attributes.  While the tag is being
    typed, full names are offered; once the tag is complete
    (``show eileen ``), the remaining attributes of that tag are offered —
    plus the clause keywords (``at``, ``with`` …) that may follow.
    """
    words = typed.split(" ")
    complete = [w for w in words[:-1] if w]
    names = _all_image_names(uri, parser)
    if not complete:
        for name, detail in names.items():
            _add_completion_item(
                items, seen, name, types.CompletionItemKind.File, detail
            )
        return
    tag, typed_attrs = complete[0], set(complete[1:])
    for name, detail in names.items():
        parts = name.split()
        if parts[0] != tag or not typed_attrs <= set(parts[1:]):
            continue
        remaining = [p for p in parts[1:] if p not in typed_attrs]
        if remaining:
            _add_completion_item(
                items,
                seen,
                " ".join(remaining),
                types.CompletionItemKind.EnumMember,
                f"{name} — {detail}",
            )
    _add_keyword_completions(
        items, seen, RENPY_SHOW_CLAUSES, kind=types.CompletionItemKind.Keyword
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
    (``sub``) so completion works level by level.  ``renpy.`` and
    ``config.`` also offer the engine's own members.
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
                _SORT_LOCAL if target_uri == uri else _SORT_WORKSPACE,
            )
    static = _STATIC_NAMESPACE_MEMBERS.get(namespace)
    if static:
        members, kind = static
        for member in members:
            _add_completion_item(
                items, seen, member, kind, f"{namespace}.{member}", _SORT_BUILTIN
            )


def _add_variable_completions(
    items: List[types.CompletionItem],
    seen: set,
    uri: str,
    parser: RpyParser,
    include_python: bool = True,
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
        uri,
        defines,
        kind=types.CompletionItemKind.Variable,
        detail_label="define",
        expression_attr="expression",
    )
    _add_workspace_symbol_completions(
        items,
        seen,
        uri,
        defaults,
        kind=types.CompletionItemKind.Variable,
        detail_label="default",
        expression_attr="expression",
    )
    if include_python:
        try:
            py_defs = ctx._workspace_index.get_python_defs()
        except Exception:  # workspace not initialized (e.g. in tests)
            py_defs = {}
        for name in sorted(py_defs):
            entries = py_defs[name]
            if not entries:
                continue
            entry_uri, lineno, code = entries[0]
            _add_completion_item(
                items,
                seen,
                name,
                types.CompletionItemKind.Variable,
                code,
                _SORT_LOCAL if entry_uri == uri else _SORT_WORKSPACE,
            )


def _add_character_completions(
    items: List[types.CompletionItem],
    seen: set,
    uri: str,
    parser: RpyParser,
) -> None:
    """``define e = Character(...)`` names — the speaker of a say line."""
    defines = _merge_current_symbols(
        ctx._get_all_workspace_defines(), uri, parser.get_all_defines()
    )
    _add_workspace_symbol_completions(
        items,
        seen,
        uri,
        defines,
        kind=types.CompletionItemKind.Variable,
        detail_label="character",
        expression_attr="expression",
        names=[
            n
            for n, entries in defines.items()
            if entries and "Character(" in (entries[0][1].expression or "")
        ],
    )


# ── Entry point ──────────────────────────────────────────────────────────


def _completion_items_for_context(
    uri: str,
    parser: RpyParser,
    lines: List[str],
    line_no: int,
    col: int,
    trigger_character: Optional[str] = None,
) -> List[types.CompletionItem]:
    line_text = lines[line_no] if line_no < len(lines) else ""
    line_text = line_text.rstrip("\r\n")
    col = min(col, len(line_text))
    prefix = line_text[:col]
    items: List[types.CompletionItem] = []
    seen: set = set()

    def finish(start: int) -> List[types.CompletionItem]:
        return _apply_edit_range(items, line_no, line_text, start, col)

    # Keep trailing spaces in prefix matching. Removing them breaks trigger
    # contexts like "jump " and "with ".
    name_start = col - len(_NAME_CHARS.search(prefix).group(0))

    state = _lexical_state(prefix)
    if state == "comment":
        return []
    if state == "string":
        # Only string arguments that name a label or screen complete.
        m = _RE_LABEL_STRING_ARG.search(prefix)
        if m:
            _add_label_completions(items, seen, uri, parser, m.group(1), line_no)
            return finish(m.start(1))
        m = _RE_SCREEN_STRING_ARG.search(prefix)
        if m:
            _add_screen_completions(items, seen, uri, parser)
            return finish(m.start(1))
        return []

    # Dotted define/default namespace members first: "music." → "music.*".
    # Falls through to the regular contexts when the namespace is unknown.
    ns_match = _RE_DOTTED_MEMBER_PREFIX.search(prefix)
    if ns_match is not None:
        _add_namespace_member_completions(
            items, seen, uri, parser, ns_match.group(1)
        )
        if items:
            return finish(col - len(_MEMBER_CHARS.search(prefix).group(0)))

    if _prefix_matches(
        prefix,
        rf"^\s*(?:show|hide|call)\s+screen\s+{_COMPLETION_IDENTIFIER_RE}$",
    ) or _prefix_matches(prefix, rf"^\s*use\s+{_COMPLETION_IDENTIFIER_RE}$"):
        _add_screen_completions(items, seen, uri, parser)
        return finish(name_start)
    if _prefix_matches(
        prefix,
        rf"^\s*(?:jump|call)\s+(?:expression\s+)?{_COMPLETION_IDENTIFIER_RE}$",
    ):
        partial = prefix[name_start:]
        _add_label_completions(items, seen, uri, parser, partial, line_no)
        return finish(name_start)
    if _prefix_matches(prefix, rf"^.*\bwith\s+{_COMPLETION_IDENTIFIER_RE}$"):
        _add_transition_completions(items, seen, uri, parser)
        return finish(name_start)
    if _prefix_matches(prefix, rf"^.*\bat\s+{_COMPLETION_IDENTIFIER_RE}$"):
        _add_transform_completions(items, seen, uri, parser)
        return finish(name_start)
    if _prefix_matches(
        prefix,
        rf"^\s*style\s+(?:{_COMPLETION_IDENTIFIER_RE}\s+is\s+)?{_COMPLETION_IDENTIFIER_RE}$",
    ):
        _add_style_completions(items, seen, uri, parser)
        return finish(name_start)
    image_match = re.match(
        rf"^\s*(?:show|scene|hide)\s+({_COMPLETION_NAME_RE})$", prefix, re.IGNORECASE
    )
    if image_match and not re.search(
        r"\b(?:at|with|behind|as|onlayer|zorder)\s", prefix, re.IGNORECASE
    ):
        if _prefix_matches(prefix, r"^\s*(?:show|hide)\s+screen\b"):
            return []
        typed = image_match.group(1)
        _add_image_completions(items, seen, uri, parser, typed)
        last_word = typed.split(" ")[-1]
        return finish(col - len(last_word))
    if _prefix_matches(
        prefix,
        rf"^\s*(?:play|queue)\s+\w+\s+{_COMPLETION_IDENTIFIER_RE}$",
    ):
        _add_audio_define_completions(items, seen, uri, parser)
        return finish(name_start)

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
        return finish(name_start)
    if block == "transform":
        _add_keyword_completions(
            items,
            seen,
            RENPY_ATL_PROPERTIES,
            kind=types.CompletionItemKind.Property,
            detail="ATL statement",
        )
        return finish(name_start)
    if block == "style":
        _add_keyword_completions(
            items,
            seen,
            RENPY_STYLE_PROPERTIES,
            kind=types.CompletionItemKind.Property,
            detail="style property",
        )
        return finish(name_start)

    # Python expressions: ``$ …``, ``if …``, ``define x = …``, python blocks.
    if block == "python" or _RE_EXPRESSION_CONTEXT.match(prefix):
        if trigger_character == " " and not prefix.strip():
            return []
        _add_variable_completions(items, seen, uri, parser)
        return finish(name_start)

    # Start of a statement: keywords and speaking characters.
    if re.match(rf"^\s*[\w{_CJK}]*$", prefix):
        if trigger_character == " ":
            return []  # typing indentation must not pop up a list
        _add_character_completions(items, seen, uri, parser)
        _add_keyword_completions(
            items,
            seen,
            RENPY_KEYWORDS,
            kind=types.CompletionItemKind.Keyword,
        )
        return finish(name_start)

    # Unknown position inside a statement — offer nothing rather than noise.
    return []
