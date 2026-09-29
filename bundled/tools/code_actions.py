"""Quick fixes (``textDocument/codeAction``) for Ren'Py diagnostics.

Matched by the machine-readable diagnostic ``code`` emitted by
``diagnostics._collect_full_diagnostics`` — never by message text.

* ``undefined-label`` → create the missing label at the end of the file.
* ``unused-label``    → delete the whole (unreferenced) label block.
"""

from __future__ import annotations

import re
from typing import List, Optional

from lsprotocol import types

from ast_parser import Label, RpyParser

_RE_UNDEFINED_LABEL = re.compile(r'^Label "(.+?)" is not defined')
_RE_UNUSED_LABEL = re.compile(r'^Label "(.+?)" is defined but never used')


def _diag_code(diag) -> Optional[str]:
    """Normalize a diagnostic's code (str, int, or enum) to a plain string."""
    code = getattr(diag, "code", None)
    if code is None:
        return None
    return str(getattr(code, "value", code))


def compute_code_actions(
    uri: str,
    diagnostics,
    lines: List[str],
    parser: RpyParser,
) -> List[types.CodeAction]:
    """Return quick fixes for the renpy-lsp diagnostics in *diagnostics*.

    *diagnostics* is the list VS Code sends in the code-action context
    (the diagnostics overlapping the cursor's line).
    """
    actions: List[types.CodeAction] = []
    for diag in diagnostics or []:
        if getattr(diag, "source", None) != "renpy-lsp":
            continue
        code = _diag_code(diag)
        if code == "undefined-label":
            m = _RE_UNDEFINED_LABEL.match(diag.message)
            if m:
                actions.append(_create_label_action(uri, m.group(1), lines, diag))
        elif code == "unused-label":
            m = _RE_UNUSED_LABEL.match(diag.message)
            if m:
                action = _delete_label_action(
                    uri, m.group(1), diag.range.start.line, lines, parser, diag
                )
                if action:
                    actions.append(action)
    return actions


def _create_label_action(
    uri: str, name: str, lines: List[str], diag
) -> types.CodeAction:
    """Append ``label name:`` with a ``pass`` body at the end of the file."""
    eof = types.Position(line=len(lines), character=0)
    # Separate from the last statement unless the file is effectively empty.
    prefix = "\n\n" if any(line.strip() for line in lines) else ""
    text = f"{prefix}label {name}:\n    pass\n"
    return types.CodeAction(
        title=f'Create label "{name}"',
        kind=types.CodeActionKind.QuickFix,
        diagnostics=[diag],
        edit=types.WorkspaceEdit(
            changes={
                uri: [
                    types.TextEdit(
                        range=types.Range(start=eof, end=eof),
                        new_text=text,
                    )
                ]
            }
        ),
    )


def _delete_label_action(
    uri: str,
    name: str,
    start_line0: int,
    lines: List[str],
    parser: RpyParser,
    diag,
) -> Optional[types.CodeAction]:
    """Delete an unused label together with its whole body block."""
    for node in parser._collect(parser.root, Label):
        if node.lineno - 1 != start_line0 or node.name != name:
            continue
        # The parser's end_lineno may include trailing blank lines that
        # belong to the block scan; don't swallow the separator before the
        # next statement.
        end_line0 = node.end_lineno
        while end_line0 - 1 > start_line0 and end_line0 - 1 < len(lines) and not lines[
            end_line0 - 1
        ].strip():
            end_line0 -= 1
        # Remove from the label's first line up to the start of the line
        # after its block (lineno/end_lineno are 1-based, Position 0-based).
        rng = types.Range(
            start=types.Position(line=node.lineno - 1, character=0),
            end=types.Position(line=end_line0, character=0),
        )
        return types.CodeAction(
            title=f'Delete unused label "{name}"',
            kind=types.CodeActionKind.QuickFix,
            diagnostics=[diag],
            edit=types.WorkspaceEdit(
                changes={
                    uri: [
                        types.TextEdit(range=rng, new_text=""),
                    ]
                }
            ),
        )
    return None
