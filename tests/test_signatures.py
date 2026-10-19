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


def test_constructor_signature_inside_multiline_call(monkeypatch) -> None:
    src = """
        define shake = Transform(
            Text("hi"),
            xalign=1.0,
        )
        """
    lines = dedent(src).strip("\n").splitlines()
    l = next(i for i, l in enumerate(lines) if "xalign" in l)
    result = help_at(monkeypatch, src, l, len(lines[l]))

    assert result is not None
    (sig,) = result.signatures
    assert sig.label.startswith("Transform(")
    assert "xalign=…" in sig.label


def test_no_constructor_signature_on_plain_expression(monkeypatch) -> None:
    result = help_at(
        monkeypatch,
        "label start:\n    $ x = 1 + 2",
        1,
        len("    $ x = 1 + "),
    )
    assert result is None


def test_constructor_signature_active_parameter_by_keyword(monkeypatch) -> None:
    src = """
        define logo = Transform(
            Text("hi", font="foo.ttf", size=16, color="#fff"),
        )
        """
    lines = dedent(src).strip("\n").splitlines()
    l = next(i for i, l in enumerate(lines) if "size=16" in l)
    col = lines[l].index("size=16") + len("size=16")
    result = help_at(monkeypatch, src, l, col)
    assert result is not None
    assert result.signatures[0].parameters[result.active_parameter].label == "size=…"


def test_constructor_signature_after_trailing_comma(monkeypatch) -> None:
    src = """
        define logo = Fixed(
            Image("a.png"),
            fit_first=True,
        )
        """
    lines = dedent(src).strip("\n").splitlines()
    l = next(i for i, l in enumerate(lines) if "fit_first" in l)
    result = help_at(monkeypatch, src, l, len(lines[l]))
    # Cursor after "fit_first=True, " → the next positional slot (index 1).
    assert result is not None and result.active_parameter >= 1
