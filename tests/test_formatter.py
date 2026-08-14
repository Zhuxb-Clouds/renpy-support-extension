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
