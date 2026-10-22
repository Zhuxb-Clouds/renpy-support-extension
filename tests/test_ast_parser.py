from __future__ import annotations

from textwrap import dedent

from ast_parser import (
    CallScreen,
    Camera,
    HideScreen,
    Label,
    RpyParser,
    Say,
    Show,
    ShowScreen,
    StyleDef,
)


def parse(source: str) -> RpyParser:
    parser = RpyParser(dedent(source).strip() + "\n")
    parser.parse()
    return parser


def test_parser_collects_core_symbols_and_skips_screen_atl_bodies() -> None:
    parser = parse(
        """
        define e = Character("Eileen")
        define audio.theme = "audio/theme.ogg"
        default has_key = False

        image bg room = "images/bg_room.png"

        style menu_button is button:
            color "#ffffff"

        transform bounce(speed=(1, (2, 3))):
            alpha 0.0
            linear 0.25 alpha 1.0

        screen inventory(items=(1, (2, 3))):
            frame:
                text "Inventory"

        label start:
            camera at center with dissolve:
                linear 0.2 zoom 1.1

            for key, value in inventory.items():
                pass

            jump end

        label end:
            return
        """
    )

    assert parser.errors == []
    assert [d.name for d in parser.get_all_defines()] == ["e", "audio.theme"]
    assert [d.name for d in parser.get_all_defaults()] == ["has_key"]
    assert [img.name for img in parser.get_all_images()] == ["bg room"]
    assert [t.name for t in parser.get_all_transforms()] == ["bounce"]
    assert [s.name for s in parser.get_all_screens()] == ["inventory"]
    assert [s.name for s in parser.get_all_styles()] == ["menu_button"]
    assert [lb.name for lb in parser.get_all_labels()] == ["start", "end"]
    assert [j.target for j in parser.get_all_jumps()] == ["end"]

    cameras = parser._collect(parser.root, Camera)
    assert len(cameras) == 1
    assert cameras[0].at_transform == "center"
    assert cameras[0].with_transition == "dissolve"


def test_show_as_clause_is_not_part_of_at_transform() -> None:
    parser = parse(
        """
        label start:
            show noel normal at pos_center as noel with dissolve
            show eileen at left, right as e2 onlayer master
            show eileen as e3 at left
        """
    )

    shows = parser._collect(parser.root, Show)
    assert [s.at_transform for s in shows] == ["pos_center", "left, right", "left"]


def test_parser_recovers_unknown_statement_without_losing_later_symbols() -> None:
    parser = parse(
        """
        label start:
            this is not valid renpy
            jump end

        style warning_text:
            color "#ff0000"

        label end:
            return
        """
    )

    assert parser.errors == [(2, "Unrecognized statement: this is not valid renpy")]
    assert [lb.name for lb in parser.get_all_labels()] == ["start", "end"]
    assert [j.target for j in parser.get_all_jumps()] == ["end"]
    assert parser._collect(parser.root, StyleDef)[0].name == "warning_text"


def test_show_and_hide_screen_statements() -> None:
    parser = parse(
        """
        label start:
            show screen twitter_feed with dissolve
            show screen hud(align=(0.5, 0.5))
            hide screen twitter_feed with dissolve
        """
    )

    shows = parser._collect(parser.root, ShowScreen)
    assert [s.screen_name for s in shows] == ["twitter_feed", "hud"]
    assert shows[0].with_transition == "dissolve"
    assert shows[1].arguments == "align=(0.5, 0.5)"

    hides = parser._collect(parser.root, HideScreen)
    assert [h.screen_name for h in hides] == ["twitter_feed"]
    assert hides[0].with_transition == "dissolve"


# ── Tree walking / continuation regressions ─────────────────────────────


def test_nodes_in_menus_and_elif_are_collected_once() -> None:
    parser = parse(
        """
        label start:
            menu:
                "A":
                    jump a
            if x:
                jump b
            elif y:
                jump c
            else:
                jump d
        """
    )
    assert [j.target for j in parser.get_all_jumps()] == ["a", "b", "c", "d"]


def test_bracket_inside_dialogue_does_not_open_a_continuation() -> None:
    parser = parse(
        """
        label start:
            e "I'm sad :("
            e "still parsed"
            jump ending
        label ending:
            return
        """
    )
    assert [lb.name for lb in parser.get_all_labels()] == ["start", "ending"]
    assert [j.target for j in parser.get_all_jumps()] == ["ending"]
    assert not parser.errors


def test_multiline_brackets_and_strings_still_join() -> None:
    parser = parse(
        """
        define e = Character(
            "Eileen",
            color="#c8ffc8",
        )
        label start:
            e "first line
               second line"
            return
        """
    )
    (define,) = parser.get_all_defines()
    assert define.name == "e" and 'color="#c8ffc8"' in define.expression
    say = parser._collect(parser.root, Say)[0]
    assert say.what == "first line second line"
    assert not parser.errors


def test_multiline_string_at_statement_indent_joins() -> None:
    # Real-world dialogue: the quoted text spans physical lines at the
    # statement's own indent, closing either inline or on its own line.
    # Ren'Py accepts this (strings run to their closing quote across
    # newlines), so the parser must join instead of flagging "Unclosed".
    parser = parse(
        """
        lanfan_os_center "
        因此，这篇文章出现的理由又会是什么呢？\\n{p}
        ——初版的草稿还要经历许多筛选和修改，我会尽可能将自己能想到的灵感一一列举出来。{p}
        难道你能证明，自己能写出一本无需多做思考便创作出的精品小说吗？"
        lanfan_os_center "
        为此，你才会使用反问句来回避断定命题的责任？\\n{p}
        无论如何劝说自己，她依旧能感受到自己抽泣的声音越来越大。{p}
        她试图憋住呼吸，至少不要这么轻易屈服。
        "
        """
    )
    says = parser._collect(parser.root, Say)
    assert len(says) == 2
    assert {s.who for s in says} == {"lanfan_os_center"}
    assert "精品小说" in says[0].what
    assert "憋住呼吸" in says[1].what
    assert not parser.errors


def test_multiline_string_closing_quote_may_dedent() -> None:
    # String content ignores indentation, even for the closing quote.
    parser = parse(
        """
        label start:
            e "first
        second"
            return
        """
    )
    says = parser._collect(parser.root, Say)
    assert [s.what for s in says] == ["first second"]
    assert [lb.name for lb in parser.get_all_labels()] == ["start"]
    assert not parser.errors


def test_unclosed_string_does_not_swallow_following_statements() -> None:
    # A stray unclosed quote is reported, and the following statements
    # (keyword statement, say line, jump) are still parsed normally.
    parser = parse(
        """
        label start:
            $ config.foo = "oops
            show eileen happy
            e "hello"
            jump ending
        label ending:
            return
        """
    )
    assert [lb.name for lb in parser.get_all_labels()] == ["start", "ending"]
    assert [j.target for j in parser.get_all_jumps()] == ["ending"]
    assert parser.errors == [(2, "Unclosed string")]


def test_unclosed_string_before_say_line_reports_and_parses_next() -> None:
    parser = parse(
        """
        e "never closed
        e "next line"
        """
    )
    assert (1, "Unclosed string") in parser.errors
    assert [s.what for s in parser._collect(parser.root, Say)] == ["next line"]


def test_unclosed_bracket_does_not_swallow_the_rest_of_the_file() -> None:
    parser = parse(
        """
        label start:
            $ x = foo(
            e "hello"
            jump ending
        label ending:
            return
        """
    )
    assert [lb.name for lb in parser.get_all_labels()] == ["start", "ending"]
    assert parser.errors == [(2, "Unclosed bracket")]


def test_local_labels_resolve_against_the_global_label() -> None:
    parser = parse(
        """
        label start:
            jump .sub
        label .sub:
            call start.sub
            return
        label other:
            jump .sub
        """
    )
    assert [lb.name for lb in parser.get_all_labels()] == ["start", "start.sub", "other"]
    targets = [j.target for j in parser.get_all_jumps() + parser.get_all_calls()]
    assert targets == ["start.sub", "other.sub", "start.sub"]


def test_name_columns_point_at_the_name_as_written() -> None:
    parser = parse(
        """
        label start:
            call ca
            jump .sub
            call screen inventory
        label .sub:
            return
        """
    )
    lines = parser.lines
    for node in parser.get_all_calls() + parser.get_all_jumps() + parser.get_all_labels():
        written = lines[node.lineno - 1][node.name_col : node.name_col + node.name_len]
        assert written in (node.name if isinstance(node, Label) else node.target, ".sub")
    (screen_call,) = parser._collect(parser.root, CallScreen)
    assert lines[3][screen_call.name_col :] == "inventory"
