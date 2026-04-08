"""Pure-data constants for the Ren'Py language server.

This module contains keyword lists, transition/transform names, hover
documentation, and word-counting helpers.  It has **no** runtime dependencies
on the server or AST parser, so it can be safely imported anywhere.
"""

from __future__ import annotations

import re
from typing import Dict, List

# ─────────────────────── Completion constants ────────────────────────────

RENPY_KEYWORDS: List[str] = [
    "label",
    "jump",
    "call",
    "return",
    "pass",
    "menu",
    "if",
    "elif",
    "else",
    "while",
    "for",
    "in",
    "define",
    "default",
    "image",
    "transform",
    "screen",
    "style",
    "scene",
    "show",
    "hide",
    "with",
    "at",
    "behind",
    "play",
    "stop",
    "queue",
    "voice",
    "init",
    "python",
    "translate",
    "pause",
    "nvl",
    "window",
    "show screen",
    "hide screen",
    "call screen",
]

RENPY_TRANSITIONS: List[str] = [
    "dissolve",
    "fade",
    "pixellate",
    "move",
    "moveinright",
    "moveinleft",
    "moveintop",
    "moveinbottom",
    "moveoutright",
    "moveoutleft",
    "moveouttop",
    "moveoutbottom",
    "ease",
    "easeinright",
    "easeinleft",
    "easeintop",
    "easeinbottom",
    "easeoutright",
    "easeoutleft",
    "easeouttop",
    "easeoutbottom",
    "zoomin",
    "zoomout",
    "vpunch",
    "hpunch",
    "blinds",
    "squares",
    "wipeleft",
    "wiperight",
    "wipeup",
    "wipedown",
    "None",
]

RENPY_TRANSFORMS: List[str] = [
    "left",
    "right",
    "center",
    "truecenter",
    "topleft",
    "topright",
    "top",
    "bottom",
    "offscreenleft",
    "offscreenright",
    "default",
    "reset",
]

# ─────────────────────── Hover documentation ─────────────────────────────

KEYWORD_DOCS: Dict[str, str] = {
    "label": "**label** *name*:\n\nDefines a named point in the script that can be jumped to or called.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/label.html)",
    "jump": "**jump** *label_name*\n\nTransfers control to the named label. Does not return.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/label.html#jump-statement)",
    "call": "**call** *label_name* [**from** *id*]\n\nCalls the named label as a subroutine. Use `return` to come back.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/label.html#call-statement)",
    "return": "**return** [*expression*]\n\nReturns from a `call` statement, optionally with a value.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/label.html#return-statement)",
    "menu": "**menu** [*name*]:\n\nDisplays a menu of choices to the player.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/menus.html)",
    "define": "**define** *name* = *expression*\n\nDefines a name at init time. Commonly used for `Character()` definitions.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/python.html#define-statement)",
    "default": "**default** *name* = *expression*\n\nSets the default value of a variable, created at game start.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/python.html#default-statement)",
    "scene": "**scene** *image* [**at** *transform*] [**with** *transition*]\n\nClears all images and shows a new background.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/displaying_images.html#scene-statement)",
    "show": "**show** *image* [**at** *transform*] [**with** *transition*]\n\nShows an image on the screen.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/displaying_images.html#show-statement)",
    "hide": "**hide** *image* [**with** *transition*]\n\nHides the named image.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/displaying_images.html#hide-statement)",
    "with": "**with** *transition*\n\nApplies a transition effect (e.g., dissolve, fade).\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/displaying_images.html#with-statement)",
    "play": '**play** *channel* "*file*" [**fadein** *sec*]\n\nPlays audio on the specified channel.\n\n[📖 Ren\'Py Docs](https://www.renpy.org/doc/html/audio.html#play-statement)',
    "stop": "**stop** *channel* [**fadeout** *sec*]\n\nStops audio on the specified channel.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/audio.html#stop-statement)",
    "voice": '**voice** "*file*"\n\nPlays a voice file for the next line of dialogue.\n\n[📖 Ren\'Py Docs](https://www.renpy.org/doc/html/voice.html)',
    "screen": "**screen** *name*([*params*]):\n\nDefines a screen for the screen language.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/screens.html)",
    "transform": "**transform** *name*([*params*]):\n\nDefines an ATL transform.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-statement)",
    "image": "**image** *name* = *expression*\n\nDefines an image that can be used with `show` or `scene`.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/displaying_images.html#image-statement)",
    "init": "**init** [*priority*] [**python**]:\n\nRuns code at initialization time, before the game starts.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/python.html#init-python-statement)",
    "python": "**python**:\n\nA block of Python code to execute.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/python.html)",
    "if": "**if** *condition*:\n\nConditional branch. Can be followed by `elif` and `else`.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/conditional.html)",
    "elif": "**elif** *condition*:\n\nAlternative branch in an if/elif/else chain.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/conditional.html)",
    "else": "**else**:\n\nFinal fallback branch in an if/elif/else chain.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/conditional.html)",
    "while": "**while** *condition*:\n\nRepeats the indented block as long as the condition is true.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/conditional.html#while-statement)",
    "for": "**for** *var* **in** *iterable*:\n\nIterates over items in a collection.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/conditional.html#while-statement)",
    "pass": "**pass**\n\nA no-op statement. Used as a placeholder.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/label.html)",
    "translate": "**translate** *language* *identifier*:\n\nProvides a translation for a block of dialogue.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/translation.html)",
    "style": "**style** *name* [**is** *parent*]:\n\nDefines or modifies a style.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style.html)",
    # ── ATL keywords ──
    "contains": "**contains**:\n\nIn ATL, creates a child displayable within a transform. Multiple `contains` blocks display layered children.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#contains-statement)",
    "parallel": "**parallel**:\n\nRuns multiple ATL blocks simultaneously.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#parallel-statement)",
    "block": "**block**:\n\nGroups a set of ATL statements together as one unit.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#block-statement)",
    "choice": "**choice** [*weight*]:\n\nIn ATL, randomly picks one of several branches to execute.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#choice-statement)",
    "linear": "**linear** *duration*\n\nATL warper: interpolates properties linearly over *duration* seconds.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#warpers)",
    "ease": "**ease** *duration*\n\nATL warper: interpolates with ease-in/ease-out (slow start and end).\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#warpers)",
    "easein": "**easein** *duration*\n\nATL warper: slow at the start, fast at the end.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#warpers)",
    "easeout": "**easeout** *duration*\n\nATL warper: fast at the start, slow at the end.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#warpers)",
    "repeat": "**repeat** [*count*]\n\nRepeats the ATL block. Without a count, repeats forever.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#repeat-statement)",
    "pause": "**pause** [*duration*]\n\nPauses execution for the given number of seconds.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#pause-statement)",
    # ── ATL / Style properties ──
    "xpos": "**xpos** *value*\n\nHorizontal position in pixels.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-xpos)",
    "ypos": "**ypos** *value*\n\nVertical position in pixels.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-ypos)",
    "xanchor": "**xanchor** *value*\n\nHorizontal anchor point (0.0 = left, 0.5 = center, 1.0 = right).\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-xanchor)",
    "yanchor": "**yanchor** *value*\n\nVertical anchor point (0.0 = top, 0.5 = center, 1.0 = bottom).\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-yanchor)",
    "xalign": "**xalign** *value*\n\nSets both `xpos` and `xanchor` to the same value. 0.0 = left, 0.5 = center, 1.0 = right.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-xalign)",
    "yalign": "**yalign** *value*\n\nSets both `ypos` and `yanchor` to the same value. 0.0 = top, 0.5 = center, 1.0 = bottom.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-yalign)",
    "align": "**align** (*xalign*, *yalign*)\n\nShorthand for setting `xalign` and `yalign` together.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-align)",
    "xoffset": "**xoffset** *pixels*\n\nHorizontal offset from the computed position, in pixels.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-xoffset)",
    "yoffset": "**yoffset** *pixels*\n\nVertical offset from the computed position, in pixels.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-yoffset)",
    "xsize": "**xsize** *pixels*\n\nSets the width of the displayable.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-xsize)",
    "ysize": "**ysize** *pixels*\n\nSets the height of the displayable.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-ysize)",
    "xysize": "**xysize** (*width*, *height*)\n\nShorthand for setting `xsize` and `ysize` together.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/style_properties.html#style-property-xysize)",
    "rotate": "**rotate** *degrees*\n\nRotates the displayable by the given number of degrees.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-properties)",
    "zoom": "**zoom** *factor*\n\nScales the displayable by the given factor (1.0 = no change).\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-properties)",
    "xzoom": "**xzoom** *factor*\n\nHorizontal scale factor. Negative values flip horizontally.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-properties)",
    "yzoom": "**yzoom** *factor*\n\nVertical scale factor. Negative values flip vertically.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-properties)",
    "alpha": "**alpha** *value*\n\nOpacity of the displayable (0.0 = transparent, 1.0 = opaque).\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-properties)",
    "crop": "**crop** (*x*, *y*, *w*, *h*)\n\nCrops the displayable to the given rectangle.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-properties)",
    "blur": "**blur** *radius*\n\nApplies a Gaussian blur with the given radius.\n\n[📖 Ren'Py Docs](https://www.renpy.org/doc/html/atl.html#transform-properties)",
}

# ─────────────────────── Word counting ───────────────────────────────────

# Regex to match CJK characters (Chinese, Japanese, Korean)
CJK_PATTERN = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]"
)


def count_words(text: str) -> int:
    """Count words in text, handling both CJK and non-CJK languages.

    For CJK characters: each character counts as one word.
    For non-CJK text: words are split by whitespace.
    """
    if not text:
        return 0

    count = 0
    non_cjk_buffer: List[str] = []

    for char in text:
        if CJK_PATTERN.match(char):
            if non_cjk_buffer:
                count += len("".join(non_cjk_buffer).split())
                non_cjk_buffer.clear()
            count += 1
        else:
            non_cjk_buffer.append(char)

    if non_cjk_buffer:
        count += len("".join(non_cjk_buffer).split())

    return count
