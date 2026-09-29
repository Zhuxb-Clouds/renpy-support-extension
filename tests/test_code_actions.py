from __future__ import annotations

from textwrap import dedent

from ast_parser import RpyParser
from lsprotocol import types

import code_actions

URI = "file:///workspace/game/script.rpy"


def parse(source: str):
    text = dedent(source).strip("\n") + "\n"
    parser = RpyParser(text)
    parser.parse()
    return text.splitlines(), parser


def make_diag(code: str, message: str, line0: int = 0) -> types.Diagnostic:
    return types.Diagnostic(
        range=types.Range(
            start=types.Position(line=line0, character=0),
            end=types.Position(line=line0, character=999),
        ),
        message=message,
        severity=types.DiagnosticSeverity.Warning,
        source="renpy-lsp",
        code=code,
    )


def edits(action) -> list:
    return action.edit.changes[URI]


# ── undefined label → create ─────────────────────────────────────────────


def test_undefined_label_gets_create_action() -> None:
    lines, parser = parse(
        """
        label start:
            jump missing_place
        """
    )
    diag = make_diag(
        "undefined-label", 'Label "missing_place" is not defined in the project'
    )
    actions = code_actions.compute_code_actions(URI, [diag], lines, parser)

    assert len(actions) == 1
    action = actions[0]
    assert action.title == 'Create label "missing_place"'
    assert action.kind == types.CodeActionKind.QuickFix
    (edit,) = edits(action)
    # Inserted at end of file, separated by a blank line, with a pass body.
    assert edit.range.start.line == len(lines)
    assert edit.new_text == '\n\nlabel missing_place:\n    pass\n'


def test_create_action_on_empty_file_has_no_leading_blank_lines() -> None:
    lines, parser = parse("")
    diag = make_diag("undefined-label", 'Label "solo" is not defined in the project')
    actions = code_actions.compute_code_actions(URI, [diag], lines, parser)
    assert edits(actions[0])[0].new_text == "label solo:\n    pass\n"


# ── unused label → delete ────────────────────────────────────────────────


def test_unused_label_gets_delete_action_covering_whole_block() -> None:
    lines, parser = parse(
        """
        label start:
            "hi"

        label lonely:
            "one"
            "two"

        label again:
            "back"
        """
    )
    diag = make_diag(
        "unused-label",
        'Label "lonely" is defined but never used',
        line0=3,  # "label lonely:" is line 4 (0-based 3)
    )
    actions = code_actions.compute_code_actions(URI, [diag], lines, parser)

    assert len(actions) == 1
    action = actions[0]
    assert action.title == 'Delete unused label "lonely"'
    (edit,) = edits(action)
    # Range starts on the label line and ends at the start of the next label.
    assert edit.range.start.line == 3
    assert edit.range.end.line == 6
    assert edit.new_text == ""


def test_delete_action_at_end_of_file() -> None:
    lines, parser = parse(
        """
        label start:
            "hi"

        label lonely:
            "bye"
        """
    )
    diag = make_diag(
        "unused-label", 'Label "lonely" is defined but never used', line0=3
    )
    actions = code_actions.compute_code_actions(URI, [diag], lines, parser)
    (edit,) = edits(actions[0])
    assert edit.range.start.line == 3
    assert edit.range.end.line == len(lines)


# ── filtering ────────────────────────────────────────────────────────────


def test_other_sources_and_codes_are_ignored() -> None:
    lines, parser = parse("label start:\n    jump x\n")
    foreign = make_diag("undefined-label", 'Label "x" is not defined in the project')
    object.__setattr__(foreign, "source", "other-lsp")
    wrong_code = make_diag("undefined-image", 'Image "x" is not defined in the project')

    actions = code_actions.compute_code_actions(
        URI, [foreign, wrong_code, None], lines, parser
    )
    assert actions == []


def test_diagnostics_without_code_are_ignored() -> None:
    lines, parser = parse("label start:\n    \"hi\"\n")
    bare = types.Diagnostic(
        range=types.Range(
            start=types.Position(line=0, character=0),
            end=types.Position(line=0, character=9),
        ),
        message="Label \"start\" is defined but never used",
        source="renpy-lsp",
    )
    assert code_actions.compute_code_actions(URI, [bare], lines, parser) == []
