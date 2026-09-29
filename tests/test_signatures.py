from __future__ import annotations

from textwrap import dedent

import server_context as ctx
from ast_parser import RpyParser

import signatures

URI = "file:///workspace/game/script.rpy"


def parse(source: str) -> RpyParser:
    parser = RpyParser(dedent(source).strip("\n") + "\n")
    parser.parse()
    return parser


def symbol_map(parser: RpyParser, getter) -> dict:
    result: dict = {}
    for node in getter(parser):
        result.setdefault(node.name, []).append((URI, node))
    return result


def help_at(monkeypatch, source: str, line_no: int, col: int) -> object:
    parser = parse(source)
    monkeypatch.setattr(
        ctx,
        "_get_all_workspace_labels",
        lambda: symbol_map(parser, RpyParser.get_all_labels),
    )
    monkeypatch.setattr(
        ctx,
        "_get_all_workspace_screens",
        lambda: symbol_map(parser, RpyParser.get_all_screens),
    )
    lines = dedent(source).strip("\n").splitlines()
    return signatures.compute_signature_help(lines, line_no, col)


def test_call_label_signature_resolves_parameters(monkeypatch) -> None:
    src = """
        label talk(who, mood="happy"):
            "hi"

        label start:
            call talk(e, m)
    """
    result = help_at(monkeypatch, src, 4, len("        call talk(e, m"))
    assert result is not None
    (sig,) = result.signatures
    assert sig.label == "call talk(who, mood)"
    assert result.active_parameter == 1


def test_call_screen_signature(monkeypatch) -> None:
    src = """
        screen shop(item, who):
            text "shop"

        label start:
            call screen shop()
    """
    result = help_at(monkeypatch, src, 4, len("        call screen shop("))
    assert result is not None
    (sig,) = result.signatures
    assert sig.label == "call screen shop(item, who)"
    assert result.active_parameter == 0


def test_unknown_call_target_falls_back_to_template(monkeypatch) -> None:
    result = help_at(
        monkeypatch,
        'label start:\n    call nowhere(x',
        1,
        len("    call nowhere(x"),
    )
    assert result is not None
    (sig,) = result.signatures
    assert sig.label.startswith("call")


def test_show_clause_template_active_parameter(monkeypatch) -> None:
    result = help_at(
        monkeypatch,
        'label start:\n    show eileen happy at left with dissolve',
        1,
        len("    show eileen happy at left with"),
    )
    assert result is not None
    (sig,) = result.signatures
    assert sig.label.startswith("show image")
    # "with" is the 2nd clause parameter (image=0, at=1, with=2)
    assert result.active_parameter == 2


def test_play_options_template(monkeypatch) -> None:
    result = help_at(
        monkeypatch,
        'label start:\n    play music "x.ogg" fadeout 1.0',
        1,
        len('    play music "x.ogg" fadeout 1.'),
    )
    assert result is not None
    (sig,) = result.signatures
    assert sig.label.startswith("play")
    assert result.active_parameter == 1  # fadeout


def test_plain_line_has_no_signature(monkeypatch) -> None:
    result = help_at(monkeypatch, 'label start:\n    "hello world"', 1, 8)
    assert result is None
