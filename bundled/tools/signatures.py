"""``textDocument/signatureHelp`` for Ren'Py script statements.

Two kinds of help:

* **Paren calls** — ``call label(a, b)`` / ``call screen scr(a)``: the
  signature is resolved from the actual workspace label/screen
  parameters, with the active parameter derived from comma position.
* **Statement templates** — clause layouts for ``show``, ``play``, …
  with the active clause derived from the last clause keyword left of
  the cursor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from lsprotocol import types

import server_context as ctx

_IDENTIFIER = r"[a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf][\w\u4e00-\u9fff\u3400-\u4dbf]*"

_RE_CALL_PAREN = re.compile(
    rf"^\s*call\s+(screen\s+)?({_IDENTIFIER})\s*\((.*)$",
    re.IGNORECASE,
)


@dataclass
class _Param:
    """One rendered parameter: ``[at transform]`` / ``image`` …

    ``triggers`` are lowercase words that activate this parameter when
    found as " word " (or line-end " word") in the text left of the
    cursor; ``None`` marks the positional parameter (active by default).
    """

    display: str
    description: str
    triggers: Optional[Tuple[str, ...]] = None


@dataclass
class _Template:
    first_words: Tuple[str, ...]
    head: str
    params: List[_Param] = field(default_factory=list)
    doc: str = ""

    @property
    def label(self) -> str:
        return " ".join([self.head] + [p.display for p in self.params])


_TEMPLATES: Dict[str, _Template] = {}


def _register(tpl: _Template) -> None:
    for word in tpl.first_words:
        _TEMPLATES[word] = tpl


_register(
    _Template(
        ("show",),
        "show",
        [
            _Param("image", "The image (tag and attributes) to show"),
            _Param("[at transform]", "A transform or ATL to position the image", ("at",)),
            _Param("[with transition]", "Transition to use", ("with",)),
            _Param("[behind tag]", "Show behind the given tag", ("behind",)),
            _Param("[as tag]", "Override the image tag", ("as",)),
            _Param("[onlayer layer]", "Layer to show on", ("onlayer",)),
            _Param("[zorder z]", "Z-order within the layer", ("zorder",)),
        ],
        "The show statement displays an image on the screen.\n\n"
        "Example: `show eileen happy at left with dissolve`",
    )
)
_register(
    _Template(
        ("scene",),
        "scene",
        [
            _Param("image", "The image to show (empty scene clears the layer)"),
            _Param("[at transform]", "A transform or ATL to position the image", ("at",)),
            _Param("[with transition]", "Transition to use", ("with",)),
            _Param("[as tag]", "Override the image tag", ("as",)),
            _Param("[onlayer layer]", "Layer to show on", ("onlayer",)),
            _Param("[zorder z]", "Z-order within the layer", ("zorder",)),
        ],
        "The scene statement clears the layer and shows an image.\n\n"
        "Example: `scene bg meadow with fade`",
    )
)
_register(
    _Template(
        ("hide",),
        "hide",
        [
            _Param("image", "The image tag to hide"),
            _Param("[with transition]", "Transition to use", ("with",)),
        ],
        "The hide statement removes an image from the screen.",
    )
)
_register(
    _Template(
        ("play",),
        "play",
        [
            _Param('channel "file"', "Channel (music, sound, audio, …) and the file to play"),
            _Param("[fadeout sec]", "Seconds of fade-out before the new track", ("fadeout",)),
            _Param("[fadein sec]", "Seconds of fade-in", ("fadein",)),
            _Param("[loop]", "Loop the track (default for music)", ("loop",)),
            _Param("[if_changed]", "Do nothing if the same track is playing", ("if_changed",)),
            _Param("[noloop]", "Disable looping", ("noloop",)),
        ],
        "The play statement starts playing a sound or music file.\n\n"
        'Example: `play music "sunflower-slow-drag.ogg" fadeout 1.0`',
    )
)
_register(
    _Template(
        ("queue",),
        "queue",
        [
            _Param('channel "file"', "Channel and the file to queue"),
            _Param("[fadeout sec]", "Seconds of fade-out", ("fadeout",)),
            _Param("[fadein sec]", "Seconds of fade-in", ("fadein",)),
            _Param("[loop]", "Loop the track", ("loop",)),
            _Param("[if_changed]", "Queue only if a different track is queued", ("if_changed",)),
            _Param("[noloop]", "Disable looping", ("noloop",)),
        ],
        "The queue statement schedules a file to play when the current "
        "track finishes.",
    )
)
_register(
    _Template(
        ("stop",),
        "stop",
        [
            _Param("channel", "Channel to stop (music, sound, …)"),
            _Param("[fadeout sec]", "Seconds of fade-out", ("fadeout",)),
        ],
        "The stop statement stops the sound playing on a channel.",
    )
)
_register(
    _Template(
        ("voice",),
        "voice",
        [_Param('"file"', "Voice file to play")],
        "The voice statement plays a voice-over file.",
    )
)
_register(
    _Template(
        ("pause",),
        "pause",
        [_Param("[seconds]", "Optional number of seconds to pause for")],
        "The pause statement waits for the player to click or for a "
        "fixed duration.",
    )
)
_register(
    _Template(
        ("window",),
        "window",
        [
            _Param(
                "[auto | show | hide]",
                "auto manages the window; show/hide force it",
                ("auto", "show", "hide"),
            )
        ],
        "The window statement manages the dialogue window.",
    )
)
_register(
    _Template(
        ("camera",),
        "camera",
        [
            _Param("layer", "Layer the camera applies to (default: master)"),
            _Param("[at transform]", "Transform/ATL applied to the layer", ("at",)),
            _Param("[with transition]", "Transition to use", ("with",)),
        ],
        "The camera statement sets the transform of an entire layer.\n\n"
        "Example: `camera at t1`",
    )
)
_register(
    _Template(
        ("with",),
        "with",
        [_Param("transition", "Transition to show")],
        "The with statement shows a transition.",
    )
)
_register(
    _Template(
        ("jump",),
        "jump",
        [_Param("label | expression", "Label to jump to")],
        "The jump statement transfers control to a label.",
    )
)
_register(
    _Template(
        ("call",),
        "call",
        [
            _Param("label | screen", "Label or screen to call"),
            _Param("[(args)]", "Arguments for label/screen parameters", ("(",)),
            _Param("[from name]", "Name for the return point", ("from",)),
        ],
        "The call statement runs a label and returns to the next "
        "statement afterwards.",
    )
)
_register(
    _Template(
        ("return",),
        "return",
        [_Param("[expression]", "Optional value to return")],
        "The return statement ends the current label.",
    )
)
_register(
    _Template(
        ("define",),
        "define",
        [
            _Param("name", "Variable name (dotted names become namespaces)", ("name",)),
            _Param("= expression", "Value — evaluated once at init time", ("=",)),
        ],
        "The define statement sets a variable when the game starts.\n\n"
        'Example: `define e = Character("Eileen")`',
    )
)
_register(
    _Template(
        ("default",),
        "default",
        [
            _Param("name", "Variable name", ("name",)),
            _Param("= expression", "Initial value — assigned once per save", ("=",)),
        ],
        "The default statement gives a variable a value that participates "
        "in saves.",
    )
)
_register(
    _Template(
        ("label",),
        "label",
        [
            _Param("name", "Label name"),
            _Param("[(parameters)]", "Parameters — set by `call label(...)`", ("(",)),
        ],
        "The label statement defines a block of Ren'Py script.",
    )
)
_register(
    _Template(
        ("screen",),
        "screen",
        [
            _Param("name", "Screen name"),
            _Param("[(parameters)]", "Parameters passed by `call screen`", ("(",)),
        ],
        "The screen statement defines a screen.",
    )
)
_register(
    _Template(
        ("transform",),
        "transform",
        [
            _Param("name", "Transform name"),
            _Param("[(parameters)]", "Parameters used by `at name(...)`", ("(",)),
        ],
        "The transform statement defines an ATL transform applied with `at`.",
    )
)
_register(
    _Template(
        ("image",),
        "image",
        [
            _Param("name", "Image name (spaces define tag + attributes)"),
            _Param("= file", "File path or displayable expression", ("=",)),
        ],
        "The image statement defines an image.\n\n"
        'Example: `image eileen happy = "eileen_happy.png"`',
    )
)
_register(
    _Template(
        ("style",),
        "style",
        [
            _Param("name", "Style being defined"),
            _Param("[is parent]", "Parent style to inherit from", ("is",)),
            _Param("[clear]", "Remove all properties", ("clear",)),
            _Param("[take other]", "Take properties from another style", ("take",)),
        ],
        "The style statement defines or changes a displayable style.",
    )
)
_register(
    _Template(
        ("if", "elif"),
        "if",
        [_Param("condition:", "Python expression deciding which block runs")],
        "The if statement runs a block conditionally.",
    )
)
_register(
    _Template(
        ("while",),
        "while",
        [_Param("condition:", "Python expression controlling the loop")],
        "The while statement loops while a condition holds.",
    )
)
_register(
    _Template(
        ("for",),
        "for",
        [_Param("variable in iterable:", "Loop variable and iterable")],
        "The for statement loops over an iterable.",
    )
)
_register(
    _Template(
        ("init",),
        "init",
        [
            _Param("[priority]", "Priority number — lower runs first", ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "-")),
            _Param("[python]", "Introduces a python block", ("python",)),
        ],
        "The init statement runs code at init time.",
    )
)
_register(
    _Template(
        ("translate",),
        "translate",
        [
            _Param("language", "Translation language name"),
            _Param("identifier:", "strings / labels / say-id / label name"),
        ],
        "The translate statement defines translation content.",
    )
)


def _split_top_level(text: str) -> List[str]:
    """Split on commas not nested in (), [], {}, or quotes."""
    parts: List[str] = []
    depth = 0
    quote: Optional[str] = None
    current: List[str] = []
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
            current.append(ch)
            continue
        if ch in "\"'":
            quote = ch
            current.append(ch)
        elif ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    tail = "".join(current)
    if tail.strip() or parts:
        parts.append(tail)
    return parts


def _paren_call_help(
    prefix: str,
) -> Optional[Tuple[types.SignatureInformation, int]]:
    """Signature for ``call label(…`` / ``call screen name(…`` resolved from
    the workspace; None when the line isn't a paren call or the target
    doesn't exist."""
    m = _RE_CALL_PAREN.match(prefix)
    if not m:
        return None
    is_screen = bool(m.group(1))
    name = m.group(2)
    inside = m.group(3)

    params_str: Optional[str] = None
    if is_screen:
        screens = ctx._get_all_workspace_screens().get(name)
        if screens:
            params_str = screens[0][1].parameters or ""
    else:
        labels = ctx._get_all_workspace_labels().get(name)
        if labels:
            params_str = labels[0][1].parameters or ""
    if params_str is None:
        return None
    params_str = params_str.strip()
    param_names = [
        p.strip().split("=")[0].strip() or f"arg{i + 1}"
        for i, p in enumerate(_split_top_level(params_str))
        if p.strip()
    ]
    kind = "screen" if is_screen else "label"
    head = f"call screen {name}" if is_screen else f"call {name}"
    active = min(
        _active_param_by_commas(inside), max(0, len(param_names) - 1)
    )
    return (
        types.SignatureInformation(
            label=head + "(" + ", ".join(param_names) + ")",
            parameters=[
                types.ParameterInformation(label=p) for p in param_names
            ],
            documentation=types.MarkupContent(
                kind=types.MarkupKind.Markdown,
                value=f"Parameters of {kind} `{name}`.",
            ),
        ),
        active,
    )


def _active_param_by_commas(text: str) -> int:
    return max(0, len(_split_top_level(text)) - 1)


def _template_help(
    line_text: str, col: int
) -> Optional[types.SignatureHelp]:
    first_word = re.match(r"[a-zA-Z]+", line_text.lstrip())
    if not first_word:
        return None
    tpl = _TEMPLATES.get(first_word.group(0))
    if tpl is None:
        return None

    prefix = line_text[:col].lower()
    active = 0
    for idx, param in enumerate(tpl.params):
        if param.triggers is None:
            continue
        for t in param.triggers:
            if f" {t} " in prefix or prefix.rstrip().endswith(f" {t}"):
                active = idx
                break
    return types.SignatureHelp(
        signatures=[
            types.SignatureInformation(
                label=tpl.label,
                parameters=[
                    types.ParameterInformation(
                        label=p.display, documentation=p.description
                    )
                    for p in tpl.params
                ],
                documentation=types.MarkupContent(
                    kind=types.MarkupKind.Markdown, value=tpl.doc
                ),
            )
        ],
        active_signature=0,
        active_parameter=active,
    )


def compute_signature_help(
    lines: List[str],
    line_no: int,
    col: int,
) -> Optional[types.SignatureHelp]:
    """Signature help at 0-based *line_no* / character *col*."""
    line_text = lines[line_no] if line_no < len(lines) else ""
    paren = _paren_call_help(line_text[:col])
    if paren is not None:
        signature, active = paren
        return types.SignatureHelp(
            signatures=[signature], active_signature=0, active_parameter=active
        )
    return _template_help(line_text, col)
