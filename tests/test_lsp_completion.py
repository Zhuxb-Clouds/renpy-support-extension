from __future__ import annotations

from textwrap import dedent

import server_context as ctx
from ast_parser import RpyParser
from completion import _completion_items_for_context


URI = "file:///workspace/game/script.rpy"


def parse(source: str) -> tuple[list[str], RpyParser]:
    text = dedent(source).strip("\n") + "\n"
    parser = RpyParser(text)
    parser.parse()
    return text.splitlines(), parser


def labels(items) -> set[str]:
    return {item.label for item in items}


def symbol_map(nodes):
    result = {}
    for node in nodes:
        result.setdefault(node.name, []).append((URI, node))
    return result


def patch_workspace_symbols(monkeypatch, parser: RpyParser) -> None:
    monkeypatch.setattr(
        ctx, "_get_all_workspace_labels", lambda: symbol_map(parser.get_all_labels())
    )
    monkeypatch.setattr(
        ctx, "_get_all_workspace_defines",
        lambda: symbol_map(parser.get_all_defines()),
    )
    monkeypatch.setattr(
        ctx, "_get_all_workspace_defaults",
        lambda: symbol_map(parser.get_all_defaults()),
    )
    monkeypatch.setattr(
        ctx, "_get_all_workspace_screens",
        lambda: symbol_map(parser.get_all_screens()),
    )
    monkeypatch.setattr(
        ctx, "_get_all_workspace_images",
        lambda: symbol_map(parser.get_all_images()),
    )
    monkeypatch.setattr(
        ctx, "_get_all_workspace_transforms",
        lambda: symbol_map(parser.get_all_transforms()),
    )
    monkeypatch.setattr(
        ctx, "_get_all_workspace_styles",
        lambda: symbol_map(parser.get_all_styles()),
    )


def complete(lines: list[str], parser: RpyParser, line_no: int):
    return _completion_items_for_context(
        URI, parser, lines, line_no, len(lines[line_no])
    )


def test_label_completion_keeps_trailing_space_context(monkeypatch) -> None:
    lines, parser = parse(
        """
        label start:
            jump 

        label ending:
            return
        """
    )
    patch_workspace_symbols(monkeypatch, parser)

    items = complete(lines, parser, 1)

    assert "ending" in labels(items)
    assert "jump" not in labels(items)


def test_completion_suggests_variables_styles_transforms_and_screens(monkeypatch) -> None:
    lines, parser = parse(
        """
        define e = Character("Eileen")
        define audio.theme = "audio/theme.ogg"
        default has_key = False
        image bg room = "images/bg_room.png"
        style menu_button is button:
            color "#ffffff"
        transform bounce:
            alpha 0.0
        screen inventory():
            text "Inventory"

        label start:
            $ 
            show bg room at 
            call screen 
            style 
            play music 
        """
    )
    patch_workspace_symbols(monkeypatch, parser)

    # ``$`` is a Python expression: variables, but no labels/keywords
    general = labels(complete(lines, parser, 12))
    assert {"e", "audio.theme", "has_key"} <= general
    assert "start" not in general and "jump" not in general

    transforms = labels(complete(lines, parser, 13))
    assert {"center", "bounce"} <= transforms

    screens = labels(complete(lines, parser, 14))
    assert "inventory" in screens

    styles = labels(complete(lines, parser, 15))
    assert "menu_button" in styles

    audio = labels(complete(lines, parser, 16))
    assert "theme" in audio


def test_namespace_member_completion_after_dot(monkeypatch) -> None:
    lines, parser = parse(
        """
        define music.track1 = "audio/music/track1.ogg"
        define music.未命名1 = "audio/music/Foxtail-Grass Studio - 氷だらけのアイスコーヒー.mp3"
        define music.sub.deep = "audio/deep.ogg"
        define gui.text_color = "#ffffff"
        default music.flag = False
        label start:
            $ x = music.
        """
    )
    patch_workspace_symbols(monkeypatch, parser)

    items = complete(lines, parser, 6)
    got = labels(items)
    assert {"track1", "未命名1", "sub", "flag"} <= got
    # Members of other namespaces must not leak in.
    assert "text_color" not in got
    assert "music.track1" not in got


def test_namespace_member_completion_partial_and_nested(monkeypatch) -> None:
    lines, parser = parse(
        """
        define music.未命名1 = "audio/a.mp3"
        define music.sub.deep = "audio/deep.ogg"
        define gui.text_color = "#ffffff"
        label start:
            $ x = music.未
            $ y = music.sub.
            play music music.
        """
    )
    patch_workspace_symbols(monkeypatch, parser)

    partial = labels(complete(lines, parser, 4))
    assert "未命名1" in partial
    assert "sub" in partial

    nested = labels(complete(lines, parser, 5))
    assert nested == {"deep"}

    play_items = labels(complete(lines, parser, 6))
    assert "未命名1" in play_items


def test_unknown_namespace_falls_through_to_normal_context(monkeypatch) -> None:
    lines, parser = parse(
        """
        define music.track1 = "audio/a.mp3"
        label start:
            jump foo.
        """
    )
    patch_workspace_symbols(monkeypatch, parser)

    # "foo." has no defines → fall back to label completions as before.
    items = labels(complete(lines, parser, 2))
    assert "start" in items
    assert "track1" not in items


def test_completion_inside_screen_transform_and_style_blocks() -> None:
    text = (
        "screen hud():\n"
        "    \n"
        "\n"
        "transform fade_in:\n"
        "    \n"
        "\n"
        "style menu_button:\n"
        "    \n"
    )
    parser = RpyParser(text)
    parser.parse()
    lines = text.splitlines()

    screen_items = labels(complete(lines, parser, 1))
    assert {"text", "button", "xalign"} <= screen_items

    transform_items = labels(complete(lines, parser, 4))
    assert {"linear", "alpha", "xalign"} <= transform_items

    style_items = labels(complete(lines, parser, 7))
    assert {"color", "font", "hover_color"} <= style_items


# ── Replace ranges, quiet contexts, new providers ───────────────────────


def by_label(items):
    return {item.label: item for item in items}


def edit_span(item) -> tuple[int, int]:
    rng = item.text_edit.range
    return rng.start.character, rng.end.character


def test_image_attribute_completion_replaces_only_the_last_word(monkeypatch) -> None:
    lines, parser = parse(
        """
        image eileen happy = "e_happy.png"
        image eileen sad = "e_sad.png"
        image bg room = "room.png"
        label start:
            show eileen ha
            show eil
        """
    )
    patch_workspace_symbols(monkeypatch, parser)
    monkeypatch.setattr(ctx, "_ensure_image_cache", lambda: None)
    monkeypatch.setattr(ctx, "_image_cache", {})

    attrs = by_label(complete(lines, parser, 4))
    assert {"happy", "sad"} <= set(attrs) and "bg room" not in attrs
    assert "at" in attrs  # clause keywords follow a complete tag
    line = lines[4]
    assert edit_span(attrs["happy"]) == (len(line) - 2, len(line))

    full = by_label(complete(lines, parser, 5))
    assert "eileen happy" in full
    line = lines[5]
    assert edit_span(full["eileen happy"]) == (len(line) - 3, len(line))
    assert full["eileen happy"].text_edit.new_text == "eileen happy"


def test_label_completion_replaces_dotted_and_cjk_prefixes(monkeypatch) -> None:
    lines, parser = parse(
        """
        label 日内-彩票站:
            return
        label start:
            jump 日内
        """
    )
    patch_workspace_symbols(monkeypatch, parser)
    items = by_label(complete(lines, parser, 3))
    line = lines[3]
    assert edit_span(items["start"]) == (len(line) - 2, len(line))


def test_no_completion_inside_dialogue_or_comments(monkeypatch) -> None:
    lines, parser = parse(
        """
        define e = Character("Eileen")
        label start:
            e "Hello there 
            # a comment with 
            e "done" 
        """
    )
    patch_workspace_symbols(monkeypatch, parser)
    assert complete(lines, parser, 2) == []
    assert complete(lines, parser, 3) == []
    # after a finished say statement: unknown mid-statement context
    assert complete(lines, parser, 4) == []


def test_space_trigger_on_indentation_offers_nothing(monkeypatch) -> None:
    lines, parser = parse(
        """
        define e = Character("Eileen")
        label start:
        """
    )
    lines.append("    ")
    patch_workspace_symbols(monkeypatch, parser)
    assert _completion_items_for_context(URI, parser, lines, 2, 4, " ") == []
    invoked = labels(_completion_items_for_context(URI, parser, lines, 2, 4))
    assert {"e", "jump", "show"} <= invoked  # characters + statements
    assert "start" not in invoked


def test_label_and_screen_names_complete_inside_action_strings(monkeypatch) -> None:
    lines, parser = parse(
        """
        screen inventory():
            textbutton "Go" action Jump("en
            textbutton "Inv" action Show("
        label ending:
            return
        """
    )
    patch_workspace_symbols(monkeypatch, parser)
    jump_items = by_label(complete(lines, parser, 1))
    assert "ending" in jump_items
    line = lines[1]
    assert edit_span(jump_items["ending"]) == (len(line) - 2, len(line))
    assert "inventory" in labels(complete(lines, parser, 2))


def test_with_and_at_include_workspace_transitions_and_transforms(monkeypatch) -> None:
    lines, parser = parse(
        """
        define slowfade = Dissolve(2.0)
        define bounce = Transform(zoom=1.2)
        define e = Character("Eileen")
        label start:
            show eileen with 
            show eileen at 
        """
    )
    patch_workspace_symbols(monkeypatch, parser)
    with_items = labels(complete(lines, parser, 4))
    assert {"slowfade", "dissolve"} <= with_items and "e" not in with_items
    at_items = labels(complete(lines, parser, 5))
    assert {"bounce", "left"} <= at_items and "slowfade" not in at_items


def test_local_label_and_engine_namespace_completion(monkeypatch) -> None:
    lines, parser = parse(
        """
        label start:
            jump .
        label .sub:
            $ renpy.
            return
        """
    )
    patch_workspace_symbols(monkeypatch, parser)
    assert ".sub" in labels(complete(lines, parser, 1))
    renpy_items = labels(complete(lines, parser, 3))
    assert {"jump", "show_screen", "notify"} <= renpy_items


def test_local_symbols_sort_before_builtins(monkeypatch) -> None:
    lines, parser = parse(
        """
        transform bounce:
            alpha 0.0
        label start:
            show eileen at 
        """
    )
    patch_workspace_symbols(monkeypatch, parser)
    items = by_label(complete(lines, parser, 3))
    assert items["bounce"].sort_text < items["left"].sort_text


# ── Displayable constructor keyword arguments ────────────────────────────


def complete_at(lines, parser, line_no: int, col: int):
    return _completion_items_for_context(
        URI, parser, lines, line_no, col
    )


def test_constructor_kwargs_completion_in_multiline_nest(monkeypatch) -> None:
    src = """
        define game_logo = Fixed(
            Image("images/ui/logo.png"),
            Transform(
                Text("SUMMER", font="wen.ttf", size=16, color="#fff"),
                fit_first=True,
            ),
        )
        """
    lines, parser = parse(src)
    patch_workspace_symbols(monkeypatch, parser)
    l_text = next(i for i, l in enumerate(lines) if 'Text("SUMMER"' in l)
    col = lines[l_text].index('font="wen.ttf"') + len('font="wen.ttf", ker')
    items = by_label(complete_at(lines, parser, l_text, col))

    assert "kerning" in items
    assert items["kerning"].insert_text == "kerning="
    # Keyword arguments already written before the cursor are filtered out.
    assert "font" not in items
    # Fixed()'s kwargs do not leak into the nested Text() call.
    assert "fit_first" not in items


def test_constructor_kwargs_at_open_paren(monkeypatch) -> None:
    lines, parser = parse(
        """
        define logo = Fixed(
            Image("images/ui/logo.png"),
        )
        """
    )
    patch_workspace_symbols(monkeypatch, parser)
    l_fix = next(i for i, l in enumerate(lines) if "Fixed(" in l)
    col = lines[l_fix].index("Fixed(") + len("Fixed(")
    items = labels(complete_at(lines, parser, l_fix, col))

    assert "fit_first" in items and "xalign" in items
    assert "spacing" in items


def test_constructor_kwargs_on_empty_continuation_line(monkeypatch) -> None:
    src = """
        define logo = Fixed(

            fit_first=True,
        )
        """
    lines, parser = parse(src)
    patch_workspace_symbols(monkeypatch, parser)
    items = labels(complete(lines, parser, 1))

    assert "fit_first" in items and "xfit" in items


def test_no_constructor_kwargs_outside_calls(monkeypatch) -> None:
    lines, parser = parse(
        """
        label start:
            "hello"

        label end:
            return
        """
    )
    patch_workspace_symbols(monkeypatch, parser)
    items = labels(complete(lines, parser, 2))

    assert "kerning" not in items
    assert "fit_first" not in items


def test_audio_define_completion_inside_play_file_list(monkeypatch) -> None:
    lines, parser = parse(
        """
        define audio.theme = "audio/theme.ogg"
        define audio.sfx_page = "audio/page.ogg"
        label start:
            play sound_0 [ sfx_
        """
    )
    patch_workspace_symbols(monkeypatch, parser)

    items = complete(lines, parser, 3)

    assert {"theme", "sfx_page"} <= labels(items)


def test_audio_define_completion_mid_list_and_after_open_bracket(monkeypatch) -> None:
    lines_mid, parser_mid = parse(
        """
        define audio.theme = "audio/theme.ogg"
        define audio.sfx_page = "audio/page.ogg"
        label start:
            play sound_0 [ theme, sfx_
        """
    )
    patch_workspace_symbols(monkeypatch, parser_mid)
    assert {"theme", "sfx_page"} <= labels(complete(lines_mid, parser_mid, 3))

    lines_open, parser_open = parse(
        """
        define audio.sfx_page = "audio/page.ogg"
        label start:
            play sound_0 [ 
        """
    )
    patch_workspace_symbols(monkeypatch, parser_open)
    assert "sfx_page" in labels(complete(lines_open, parser_open, 2))


def test_no_audio_completion_after_the_list_closes(monkeypatch) -> None:
    lines, parser = parse(
        """
        define audio.theme = "audio/theme.ogg"
        label start:
            play sound_0 [ theme ] lo
        """
    )
    patch_workspace_symbols(monkeypatch, parser)

    items = complete(lines, parser, 2)

    assert "theme" not in labels(items)
