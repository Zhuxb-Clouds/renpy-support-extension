from __future__ import annotations

import lsp_server


def test_normalize_binary_star_spacing_in_matrixcolor_expression() -> None:
    assert (
        lsp_server._normalize_binary_star_spacing(
            "matrixcolor TintMatrix('#EEFDFD') *  BrightnessMatrix(0.1)"
        )
        == "matrixcolor TintMatrix('#EEFDFD') * BrightnessMatrix(0.1)"
    )
    assert (
        lsp_server._normalize_binary_star_spacing(
            "matrixcolor TintMatrix('#EEFDFD')*BrightnessMatrix(0.1)"
        )
        == "matrixcolor TintMatrix('#EEFDFD') * BrightnessMatrix(0.1)"
    )


def test_normalize_binary_star_spacing_avoids_strings_comments_and_star_args() -> None:
    assert lsp_server._normalize_binary_star_spacing('e "a *  b"') == 'e "a *  b"'
    assert (
        lsp_server._normalize_binary_star_spacing("matrixcolor A *  B  # keep *  here")
        == "matrixcolor A * B  # keep *  here"
    )
    assert (
        lsp_server._normalize_binary_star_spacing("$ callback(*args, **kwargs)")
        == "$ callback(*args, **kwargs)"
    )


def test_normalize_expression_spacing_for_common_renpy_and_python_syntax() -> None:
    assert (
        lsp_server._normalize_expression_spacing(
            'define e=Character("Eileen",color="#fff")'
        )
        == 'define e = Character("Eileen", color="#fff")'
    )
    assert (
        lsp_server._normalize_expression_spacing("default score=old_score+10")
        == "default score = old_score + 10"
    )
    assert (
        lsp_server._normalize_expression_spacing("$ result=a+b*  c-5")
        == "$ result = a + b * c - 5"
    )
    assert (
        lsp_server._normalize_expression_spacing("if score>=10 and lives<3:")
        == "if score >= 10 and lives < 3:"
    )
    assert (
        lsp_server._normalize_expression_spacing("blur blur_amount-5")
        == "blur blur_amount - 5"
    )


def test_normalize_expression_spacing_keeps_keyword_arguments_tight() -> None:
    assert (
        lsp_server._normalize_expression_spacing(
            'Transform(matrixcolor=BrightnessMatrix(-0.1),zoom=dot_zoom*(y/screen_h))'
        )
        == 'Transform(matrixcolor=BrightnessMatrix(-0.1), zoom=dot_zoom * (y / screen_h))'
    )
    assert (
        lsp_server._normalize_expression_spacing("matrixcolor=BrightnessMatrix(1.0),")
        == "matrixcolor=BrightnessMatrix(1.0),"
    )


def test_normalize_expression_spacing_keeps_hyphenated_statement_names() -> None:
    assert (
        lsp_server._normalize_expression_spacing("image 便利店-空调-关闭:")
        == "image 便利店-空调-关闭:"
    )
    assert (
        lsp_server._normalize_expression_spacing("label 便利店-空调:")
        == "label 便利店-空调:"
    )
    assert (
        lsp_server._normalize_expression_spacing("screen 便利店-空调:")
        == "screen 便利店-空调:"
    )
    assert (
        lsp_server._normalize_expression_spacing("transform 便利店-空调:")
        == "transform 便利店-空调:"
    )
    assert (
        lsp_server._normalize_expression_spacing("show 便利店-空调-关闭")
        == "show 便利店-空调-关闭"
    )
    assert (
        lsp_server._normalize_expression_spacing("jump 便利店-空调")
        == "jump 便利店-空调"
    )
    assert (
        lsp_server._normalize_expression_spacing("init -1 python:")
        == "init -1 python:"
    )


def test_normalize_expression_spacing_collapses_dashes_in_image_names() -> None:
    # Ren'Py forbids image-name components that begin with '-'; a dash used as
    # an in-component separator must not be surrounded by spaces.
    assert (
        lsp_server._normalize_expression_spacing("image 便利店 - 内部:")
        == "image 便利店-内部:"
    )
    assert (
        lsp_server._normalize_expression_spacing("image 主角家 - 卧室:")
        == "image 主角家-卧室:"
    )
    assert (
        lsp_server._normalize_expression_spacing("image 便利店 - 外部 - 树:")
        == "image 便利店-外部-树:"
    )
    assert (
        lsp_server._normalize_expression_spacing('image 便利店 - 内部 = "x.png"')
        == 'image 便利店-内部 = "x.png"'
    )
    assert (
        lsp_server._normalize_expression_spacing("show 便利店 - 内部")
        == "show 便利店-内部"
    )
    assert (
        lsp_server._normalize_expression_spacing("scene 便利店 - 内部 with dissolve")
        == "scene 便利店-内部 with dissolve"
    )
    assert (
        lsp_server._normalize_expression_spacing("hide 便利店 - 内部")
        == "hide 便利店-内部"
    )
    # Already-valid multi-component names (no dash) are untouched.
    assert (
        lsp_server._normalize_expression_spacing("image bg market:")
        == "image bg market:"
    )
    # Non-image statements keep their spaced dashes (e.g. ``init -1``).
    assert (
        lsp_server._normalize_expression_spacing("init -1 python:")
        == "init -1 python:"
    )


def test_normalize_expression_spacing_keeps_unary_minus_after_assignment() -> None:
    # Regression: a unary minus right after a spaced assignment operator must
    # not be mistaken for a binary operator and split with spaces, which would
    # turn ``init offset = -2`` into the invalid ``init offset =  - 2``.
    assert (
        lsp_server._normalize_expression_spacing("init offset = -2")
        == "init offset = -2"
    )
    assert (
        lsp_server._normalize_expression_spacing("init offset =  -2")
        == "init offset = -2"
    )
    assert (
        lsp_server._normalize_expression_spacing("init offset =-2")
        == "init offset = -2"
    )
    assert (
        lsp_server._normalize_expression_spacing("define foo = -2")
        == "define foo = -2"
    )
    assert (
        lsp_server._normalize_expression_spacing("default foo = -2")
        == "default foo = -2"
    )
    # Binary minus elsewhere in the expression is still normalized.
    assert (
        lsp_server._normalize_expression_spacing("init offset = -2 - 3")
        == "init offset = -2 - 3"
    )
    assert (
        lsp_server._normalize_expression_spacing("define foo = -2 + 3")
        == "define foo = -2 + 3"
    )


def test_normalize_expression_spacing_still_normalizes_after_statement_name() -> None:
    assert (
        lsp_server._normalize_expression_spacing('image foo = "x-1.png"')
        == 'image foo = "x-1.png"'
    )
    assert (
        lsp_server._normalize_expression_spacing("image foo= bg-1")
        == "image foo = bg - 1"
    )
    assert (
        lsp_server._normalize_expression_spacing("define e=Character('E',color='#fff')")
        == "define e = Character('E', color='#fff')"
    )
    assert (
        lsp_server._normalize_expression_spacing("show expression (a-1) as x")
        == "show expression (a - 1) as x"
    )


# ─────────────────────── format_document ──────────────────────────────


class _FakeDoc:
    def __init__(self, source: str) -> None:
        self.source = source


class _FakeWorkspace:
    def __init__(self, source: str) -> None:
        self._doc = _FakeDoc(source)

    def get_text_document(self, uri: str) -> _FakeDoc:
        return self._doc


class _FakeLS:
    def __init__(self, source: str) -> None:
        self.workspace = _FakeWorkspace(source)


def _format(
    source: str,
    *,
    blank_lines: str = "collapse",
    indent_size: int = 4,
    tab_size: int = 4,
) -> str:
    """Run format_document with the given style settings and return the result."""
    from lsprotocol import types

    saved_formatting = dict(lsp_server._settings["formatting"])
    saved_style = dict(lsp_server._vscode_style)
    try:
        lsp_server._settings["formatting"]["enabled"] = True
        lsp_server._settings["formatting"]["indentSize"] = indent_size
        lsp_server._settings["formatting"]["blankLines"] = blank_lines
        lsp_server._vscode_style["indentSize"] = indent_size
        lsp_server._vscode_style["blankLines"] = blank_lines
        params = types.DocumentFormattingParams(
            text_document=types.TextDocumentIdentifier(uri="file:///test.rpy"),
            options=types.FormattingOptions(tab_size=tab_size, insert_spaces=True),
        )
        edits = lsp_server.format_document(_FakeLS(source), params)
        return edits[0].new_text if edits else source
    finally:
        lsp_server._settings["formatting"].update(saved_formatting)
        lsp_server._vscode_style.update(saved_style)


def test_format_collapse_blank_lines() -> None:
    source = 'label a:\n    "x"\n\n\n\n    "y"\n'
    assert _format(source, blank_lines="collapse") == 'label a:\n    "x"\n\n    "y"\n'


def test_format_preserve_leaves_blank_lines_untouched() -> None:
    source = 'label a:\n    "x"\n\n\n    "y"\n'
    assert _format(source, blank_lines="preserve") == source


def test_format_strip_removes_all_blank_lines() -> None:
    source = 'label a:\n    "x"\n\n    "y"\n\nlabel b:\n    "z"\n'
    assert (
        _format(source, blank_lines="strip")
        == 'label a:\n    "x"\n    "y"\nlabel b:\n    "z"\n'
    )


def test_format_between_say_inserts_blank_lines() -> None:
    source = 'label start:\n    "甲"\n    girl    "乙"\n    scene 黑夜\n    "丙"\n'
    assert (
        _format(source, blank_lines="betweenSay")
        == 'label start:\n    "甲"\n\n    girl "乙"\n\n    scene 黑夜\n\n    "丙"\n'
    )


def test_format_between_say_normalizes_existing_blanks() -> None:
    source = 'label start:\n    "甲"\n\n\n    "乙"\n'
    assert (
        _format(source, blank_lines="betweenSay")
        == 'label start:\n    "甲"\n\n    "乙"\n'
    )


def test_format_between_say_extend_is_not_separated() -> None:
    source = 'label start:\n    "甲"\n    extend "乙"\n    "丙"\n'
    assert (
        _format(source, blank_lines="betweenSay")
        == 'label start:\n    "甲"\n    extend "乙"\n\n    "丙"\n'
    )
    # An existing blank between a say line and its extend is removed.
    source_with_blank = 'label start:\n    "甲"\n\n    extend "乙"\n'
    assert (
        _format(source_with_blank, blank_lines="betweenSay")
        == 'label start:\n    "甲"\n    extend "乙"\n'
    )


def test_format_between_say_keeps_menu_compact() -> None:
    source = (
        'label start:\n'
        '    "对话"\n'
        '    menu:\n'
        '        "选项A":\n'
        '            jump a\n'
        '        "选项B":  # comment\n'
        '            jump b\n'
    )
    assert (
        _format(source, blank_lines="betweenSay")
        == 'label start:\n'
        '    "对话"\n'
        '\n'
        '    menu:\n'
        '        "选项A":\n'
        '            jump a\n'
        '        "选项B":  # comment\n'
        '            jump b\n'
    )


def test_format_between_say_ignores_non_script_blocks() -> None:
    source = (
        'init python:\n'
        '    """docstring"""\n'
        '    x = 1\n'
        '\n'
        'screen foo():\n'
        '    text "你好"\n'
        '    text "世界"\n'
    )
    assert _format(source, blank_lines="betweenSay") == source


def test_format_between_say_requires_label_scope() -> None:
    # Bare top-level say lines (outside any label) are not spaced apart.
    source = '"甲"\n"乙"\n'
    assert _format(source, blank_lines="betweenSay") == source


def test_format_indent_size_setting_overrides_lsp_tab_size() -> None:
    source = 'label a:\n    if x:\n        "hi"\n'
    # VS Code reports tabSize=4, but the indentSize setting wins with 2.
    assert (
        _format(source, indent_size=2, tab_size=4)
        == 'label a:\n  if x:\n    "hi"\n'
    )


def test_format_indent_size_zero_falls_back_to_lsp_tab_size() -> None:
    source = 'label a:\n    "x"\n'
    assert (
        _format(source, indent_size=0, tab_size=2)
        == 'label a:\n  "x"\n'
    )


def test_format_collapses_spaced_dashes_in_image_names() -> None:
    # Regression: ``image 主角家 - 卧室:`` is invalid Ren'Py ("image name
    # components may not begin with a '-'"); the formatter collapses the
    # spaces around the dash so the result is ``image 主角家-卧室:``.
    source = (
        'image 主角家 - 卧室:\n'
        '    "#000"\n'
        '    xysize (3840, 2160)\n'
        '\n'
        'image 便利店 - 内部:\n'
        '    "#000"\n'
    )
    assert (
        _format(source)
        == 'image 主角家-卧室:\n'
        '    "#000"\n'
        '    xysize (3840, 2160)\n'
        '\n'
        'image 便利店-内部:\n'
        '    "#000"\n'
    )
