"""
Ren'Py AST Parser — indentation-aware parser for .rpy files.

Produces a simple AST used by the LSP server for diagnostics,
document symbols, go-to-definition, completion, hover, and formatting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union


# ──────────────────────────── AST Node Types ─────────────────────────────


@dataclass
class Node:
    """Base class for every AST node."""

    lineno: int  # 1-based line number
    end_lineno: int  # 1-based end line (updated after block is parsed)
    indent: int  # column offset (number of leading spaces)

    def __post_init__(self):
        if self.end_lineno == 0:
            self.end_lineno = self.lineno


@dataclass
class Script(Node):
    """Root node representing the entire file."""

    body: List[Node] = field(default_factory=list)


# ── Definitions ──


@dataclass
class Label(Node):
    """``label name(params):`` block."""

    name: str = ""
    parameters: Optional[str] = None
    body: List[Node] = field(default_factory=list)


@dataclass
class Define(Node):
    """``define name = expr``."""

    name: str = ""
    expression: str = ""


@dataclass
class Default(Node):
    """``default name = expr``."""

    name: str = ""
    expression: str = ""


@dataclass
class ImageDef(Node):
    """``image name = expr`` or ``image name:`` block."""

    name: str = ""
    expression: Optional[str] = None
    body: List[Node] = field(default_factory=list)


@dataclass
class TransformDef(Node):
    """``transform name(params):`` block."""

    name: str = ""
    parameters: Optional[str] = None
    body: List[Node] = field(default_factory=list)


@dataclass
class ScreenDef(Node):
    """``screen name(params):`` block."""

    name: str = ""
    parameters: Optional[str] = None
    body: List[Node] = field(default_factory=list)


@dataclass
class StyleDef(Node):
    """``style name [is parent]:``."""

    name: str = ""
    parent: Optional[str] = None
    body: List[Node] = field(default_factory=list)


# ── Control Flow ──


@dataclass
class If(Node):
    """``if condition:`` / ``elif condition:`` / ``else:`` chain."""

    condition: str = ""
    body: List[Node] = field(default_factory=list)
    elif_clauses: List[Elif] = field(default_factory=list)
    else_body: List[Node] = field(default_factory=list)


@dataclass
class Elif(Node):
    """An ``elif`` branch, stored inside :class:`If.elif_clauses`."""

    condition: str = ""
    body: List[Node] = field(default_factory=list)


@dataclass
class While(Node):
    """``while condition:`` loop."""

    condition: str = ""
    body: List[Node] = field(default_factory=list)


@dataclass
class For(Node):
    """``for var in iterable:`` loop."""

    variable: str = ""
    iterable: str = ""
    body: List[Node] = field(default_factory=list)


@dataclass
class Menu(Node):
    """``menu [name]:`` block containing menu items."""

    name: Optional[str] = None
    prompt: Optional[str] = None  # optional "say" string before choices
    body: List[Node] = field(default_factory=list)


@dataclass
class MenuItem(Node):
    """A single choice inside a :class:`Menu`."""

    caption: str = ""
    condition: Optional[str] = None  # ``if expr`` guard
    body: List[Node] = field(default_factory=list)


# ── Flow Statements ──


@dataclass
class Jump(Node):
    """``jump label_name`` or ``jump expression``."""

    target: str = ""
    is_expression: bool = False


@dataclass
class Call(Node):
    """``call label_name [(args)] [from _label]``."""

    target: str = ""
    arguments: Optional[str] = None
    from_label: Optional[str] = None
    is_expression: bool = False


@dataclass
class Return(Node):
    """``return [expr]``."""

    value: Optional[str] = None


@dataclass
class Pass(Node):
    """``pass``."""

    pass


@dataclass
class CallScreen(Node):
    """``call screen screen_name(args)``."""

    screen_name: str = ""
    arguments: Optional[str] = None


@dataclass
class ShowScreen(Node):
    """``show screen screen_name(args)``."""

    screen_name: str = ""
    arguments: Optional[str] = None


# ── Dialogue / Narration ──


@dataclass
class Say(Node):
    """``[who] "what"``  — character dialogue or narration."""

    who: Optional[str] = None
    what: str = ""


@dataclass
class NarratorSay(Node):
    """Bare string on a line (narrator)."""

    what: str = ""


# ── Visual / Audio ──


@dataclass
class Scene(Node):
    """``scene image_name [at transform] [with transition]``."""

    image: str = ""
    at_transform: Optional[str] = None
    with_transition: Optional[str] = None
    has_block: bool = False
    body: List[Node] = field(default_factory=list)


@dataclass
class Show(Node):
    """``show image_name [at transform] [with transition]``."""

    image: str = ""
    at_transform: Optional[str] = None
    with_transition: Optional[str] = None
    has_block: bool = False
    body: List[Node] = field(default_factory=list)


@dataclass
class Hide(Node):
    """``hide image_name [with transition]``."""

    image: str = ""
    with_transition: Optional[str] = None
    has_block: bool = False
    body: List[Node] = field(default_factory=list)


@dataclass
class With(Node):
    """``with transition``."""

    transition: str = ""


@dataclass
class PlayMusic(Node):
    """``play channel "file" [options]``."""

    channel: str = ""
    filename: str = ""
    options: str = ""


@dataclass
class StopMusic(Node):
    """``stop channel [fadeout N]``."""

    channel: str = ""
    options: str = ""


@dataclass
class QueueMusic(Node):
    """``queue channel "file"``."""

    channel: str = ""
    filename: str = ""


@dataclass
class Voice(Node):
    """``voice "filename"``."""

    filename: str = ""


@dataclass
class NvlClear(Node):
    """``nvl clear`` / ``nvl show`` / ``nvl hide``."""

    action: str = ""


@dataclass
class Window(Node):
    """``window show`` / ``window hide`` / ``window auto``."""

    action: str = ""


@dataclass
class Pause(Node):
    """``pause [duration]``."""

    duration: Optional[str] = None


@dataclass
class InitOffset(Node):
    """``init offset = N``."""

    offset: int = 0


# ── Python / Init ──


@dataclass
class PythonBlock(Node):
    """``python [early] [in ns] [hide]:`` — a python code block."""

    is_early: bool = False
    namespace: Optional[str] = None
    is_hide: bool = False
    code: str = ""
    body: List[Node] = field(default_factory=list)


@dataclass
class PythonOneliner(Node):
    """``$ python_expression``."""

    code: str = ""


@dataclass
class Init(Node):
    """``init [priority] [python [early] [in ns]]:`` block."""

    priority: Optional[int] = None
    is_python: bool = False
    is_early: bool = False
    namespace: Optional[str] = None
    body: List[Node] = field(default_factory=list)


# ── Translation ──


@dataclass
class Translate(Node):
    """``translate language label_id:`` block."""

    language: str = ""
    identifier: str = ""
    body: List[Node] = field(default_factory=list)


# ── Misc ──


@dataclass
class Comment(Node):
    """``# text``."""

    text: str = ""


@dataclass
class Unknown(Node):
    """Fallback for lines the parser cannot classify."""

    text: str = ""


# ──────────────────────────── Regex Patterns ─────────────────────────────

# Pre-compiled patterns for performance.
_RE_LABEL = re.compile(
    r"^label\s+([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf][\w.]*)\s*(?:\(([^)]*)\))?\s*:$"
)
_RE_DEFINE = re.compile(
    r"^define\s+([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf][\w.]*)\s*=\s*(.+)$"
)
_RE_DEFAULT = re.compile(
    r"^default\s+([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf][\w.]*)\s*=\s*(.+)$"
)
_RE_IMAGE = re.compile(
    r"^image\s+([\w\u4e00-\u9fff\u3400-\u4dbf\-][\w\u4e00-\u9fff\u3400-\u4dbf\- ]*?)\s*(?:=\s*(.+)|:)\s*$"
)
_RE_TRANSFORM = re.compile(
    r"^transform\s+([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf]\w*)\s*(?:\(([^)]*)\))?\s*:$"
)
_RE_SCREEN = re.compile(
    r"^screen\s+([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf]\w*)\s*(?:\(([^)]*)\))?\s*:$"
)
_RE_STYLE = re.compile(
    r"^style\s+([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf]\w*)(?:\s+is\s+([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf]\w*))?\s*:?$"
)

_RE_IF = re.compile(r"^if\s+(.+):$")
_RE_ELIF = re.compile(r"^elif\s+(.+):$")
_RE_ELSE = re.compile(r"^else\s*:$")
_RE_WHILE = re.compile(r"^while\s+(.+):$")
_RE_FOR = re.compile(r"^for\s+(\w+)\s+in\s+(.+):$")

_RE_MENU = re.compile(r"^menu\s*(?:(\w+)\s*)?:$")
_RE_MENU_ITEM = re.compile(r'^"(.+?)"\s*(?:if\s+(.+))?:$')

_RE_JUMP = re.compile(
    r"^jump\s+(expression\s+)?([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf][\w.]*)$"
)
_RE_CALL = re.compile(
    r"^call\s+(expression\s+)?([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf][\w.]*)\s*(?:\(([^)]*)\))?\s*(?:from\s+([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf]\w*))?$"
)
_RE_CALL_SCREEN = re.compile(
    r"^call\s+screen\s+([\w\u4e00-\u9fff\u3400-\u4dbf]+)\s*(?:\((.*)\))?\s*$"
)
_RE_SHOW_SCREEN = re.compile(
    r"^show\s+screen\s+([\w\u4e00-\u9fff\u3400-\u4dbf]+)\s*(?:\((.*)\))?\s*$"
)

_RE_SCENE = re.compile(r"^scene\s+(.+?)(?:\s+at\s+(.+?))?(?:\s+with\s+(\w+))?\s*:?\s*$")
_RE_SHOW = re.compile(
    r"^show\s+(.+?)(?:\s+at\s+(.+?))?(?:\s+with\s+(\w+))?(?:\s+behind\s+(\w+))?\s*:?\s*$"
)
_RE_HIDE = re.compile(r"^hide\s+(.+?)(?:\s+with\s+(\w+))?\s*:?\s*$")
_RE_WITH = re.compile(r"^with\s+(\w+)$")

_RE_PLAY = re.compile(
    r"^play\s+(\w+)\s+(?:[\"']([^\"']+)[\"']|([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf]\w*))(.*)$"
)
_RE_STOP = re.compile(r"^stop\s+(\w+)\s*(.*)$")
_RE_QUEUE = re.compile(
    r"^queue\s+(\w+)\s+(?:[\"']([^\"']+)[\"']|([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf]\w*))(.*)$"
)
_RE_VOICE = re.compile(r"^voice\s+[\"'](.+?)[\"']$")

_RE_NVL = re.compile(r"^nvl\s+(clear|show|hide)$")

_RE_PYTHON_BLOCK = re.compile(
    r"^python(?:\s+(early))?(?:\s+in\s+([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf]\w*))?(?:\s+(hide))?\s*:$"
)
_RE_PYTHON_ONELINER = re.compile(r"^\$\s+(.+)$")

_RE_INIT = re.compile(
    r"^init\s*(?:(-?\d+)\s+)?"
    r"(?:(python)(?:\s+(early))?(?:\s+in\s+([a-zA-Z_\u4e00-\u9fff\u3400-\u4dbf]\w*))?)?\s*:?$"
)
_RE_INIT_OFFSET = re.compile(r"^init\s+offset\s*=\s*(-?\d+)$")

_RE_TRANSLATE = re.compile(r"^translate\s+(\w+)\s+([\w.]+)\s*:$")

_RE_SAY = re.compile(r'^(\w+)\s+"(.*)"(?:\s+with\s+\w+)?$')
_RE_SAY_NOSPACE = re.compile(r'^(\w+)"(.*)"(?:\s+with\s+\w+)?$')
_RE_NARRATOR = re.compile(r'^"(.*)"(?:\s+with\s+\w+)?$')

# window / pause
_RE_WINDOW = re.compile(r"^window\s+(show|hide|auto)$")
_RE_PAUSE = re.compile(r"^pause(?:\s+(.+))?$")


# ──────────────────────────── Parser ─────────────────────────────────────


class _StackEntry:
    """Book-keeping for each nesting level while parsing."""

    __slots__ = ("indent", "node")

    def __init__(self, indent: int, node: Node):
        self.indent = indent
        self.node = node


class RpyParser:
    """
    Indentation-aware parser for Ren'Py ``.rpy`` files.

    Usage::

        ast = RpyParser(source_text).parse()
    """

    def __init__(self, text: str):
        # Strip BOM if present
        if text.startswith("\ufeff"):
            text = text[1:]
        self.lines = text.splitlines()
        self.pos = 0
        self.root = Script(lineno=0, end_lineno=0, indent=0, body=[])
        self._errors: List[Tuple[int, str]] = []  # (lineno, message)

    # ── public API ──

    def parse(self) -> Script:
        """Parse the source and return the root :class:`Script` node."""
        stack: List[_StackEntry] = [_StackEntry(0, self.root)]
        # Track bracket nesting for multi-line continuation
        _bracket_depth = 0
        _continuation_lines: List[str] = []
        _continuation_start = 0
        _continuation_indent = 0

        while self.pos < len(self.lines):
            raw = self.lines[self.pos]
            self.pos += 1

            line = raw.rstrip()
            if not line.strip():
                # If inside a bracket continuation, keep blank lines
                if _bracket_depth > 0:
                    _continuation_lines.append("")
                continue

            indent = len(line) - len(line.lstrip(" "))
            content = line.strip()
            lineno = self.pos  # 1-based

            # ── Handle multi-line bracket continuation ──
            if _bracket_depth > 0:
                _continuation_lines.append(content)
                _bracket_depth += (
                    content.count("(") + content.count("[") + content.count("{")
                )
                _bracket_depth -= (
                    content.count(")") + content.count("]") + content.count("}")
                )
                if _bracket_depth <= 0:
                    # Rejoin and parse as one logical line
                    _bracket_depth = 0
                    content = " ".join(ln for ln in _continuation_lines if ln)
                    lineno = _continuation_start
                    indent = _continuation_indent
                    _continuation_lines = []
                    # Fall through to normal parsing below
                else:
                    continue

            # ── Detect triple-quoted strings and skip to closing ──
            if '"""' in content:
                count = content.count('"""')
                if count == 1:
                    # Opening triple-quote without close — skip lines until close
                    while self.pos < len(self.lines):
                        nxt = self.lines[self.pos]
                        self.pos += 1
                        if '"""' in nxt:
                            break
                    continue

            # ── Check for unclosed brackets (multi-line continuation) ──
            depth = (
                content.count("(")
                + content.count("[")
                + content.count("{")
                - content.count(")")
                - content.count("]")
                - content.count("}")
            )
            if depth > 0:
                _bracket_depth = depth
                _continuation_lines = [content]
                _continuation_start = lineno
                _continuation_indent = indent
                continue

            # ── Pop stack to correct parent level ──
            while len(stack) > 1 and indent <= stack[-1].indent:
                finished = stack.pop()
                finished.node.end_lineno = lineno - 1

            parent = stack[-1].node

            # ── Inside a Python block: skip Ren'Py parsing ──
            if isinstance(parent, PythonBlock) or (
                isinstance(parent, Init) and parent.is_python
            ):
                node = PythonOneliner(
                    lineno=lineno, end_lineno=lineno, indent=indent, code=content
                )
                self._append_to_parent(parent, node)
                continue

            # ── Inside screen / transform / style / image / ATL blocks: skip body parsing ──
            if isinstance(
                parent, (ScreenDef, TransformDef, StyleDef, ImageDef, Scene, Show, Hide)
            ):
                # But still parse python: blocks and $ one-liners inside these
                m = _RE_PYTHON_BLOCK.match(content)
                if m:
                    node = PythonBlock(
                        lineno=lineno,
                        end_lineno=0,
                        indent=indent,
                        is_early=m.group(1) is not None,
                        namespace=m.group(2),
                        is_hide=m.group(3) is not None,
                    )
                    self._append_to_parent(parent, node)
                    stack.append(_StackEntry(indent + 1, node))
                    continue
                m = _RE_PYTHON_ONELINER.match(content)
                if m:
                    node = PythonOneliner(
                        lineno=lineno,
                        end_lineno=lineno,
                        indent=indent,
                        code=m.group(1).strip(),
                    )
                    self._append_to_parent(parent, node)
                    continue
                node = Unknown(
                    lineno=lineno, end_lineno=lineno, indent=indent, text=content
                )
                self._append_to_parent(parent, node)
                # If it ends with ':', it can also be a sub-block container
                if content.endswith(":"):
                    stack.append(_StackEntry(indent + 1, parent))
                continue

            node = self._parse_line(content, lineno, indent)

            # ── Handle elif / else: attach to preceding If ──
            if isinstance(node, tuple):
                tag = node[0]
                if tag == "ELIF":
                    _, cond, ln, ind = node
                    self._attach_elif(stack, parent, cond, ln, ind)
                elif tag == "ELSE":
                    _, ln, ind = node
                    self._attach_else(stack, parent, ln, ind)
                continue

            # ── Insert node into parent's body ──
            self._append_to_parent(parent, node)

            # ── If the node can contain children, push onto stack ──
            if self._is_block_node(node):
                stack.append(_StackEntry(indent + 1, node))

        # Close remaining stack
        total = len(self.lines)
        while len(stack) > 1:
            finished = stack.pop()
            finished.node.end_lineno = total

        self.root.end_lineno = total
        return self.root

    @property
    def errors(self) -> List[Tuple[int, str]]:
        """Parse errors collected during ``parse()``."""
        return self._errors

    # ── convenience query methods ──

    def get_all_labels(self, root: Optional[Script] = None) -> List[Label]:
        """Return every :class:`Label` in the tree."""
        return self._collect(root or self.root, Label)

    def get_all_defines(self, root: Optional[Script] = None) -> List[Define]:
        return self._collect(root or self.root, Define)

    def get_all_defaults(self, root: Optional[Script] = None) -> List[Default]:
        return self._collect(root or self.root, Default)

    def get_all_characters(self, root: Optional[Script] = None) -> List[Define]:
        """Return :class:`Define` nodes whose expression looks like ``Character(...)``."""
        return [d for d in self.get_all_defines(root) if "Character(" in d.expression]

    def get_all_screens(self, root: Optional[Script] = None) -> List[ScreenDef]:
        return self._collect(root or self.root, ScreenDef)

    def get_all_images(self, root: Optional[Script] = None) -> List[ImageDef]:
        return self._collect(root or self.root, ImageDef)

    def get_all_jumps(self, root: Optional[Script] = None) -> List[Jump]:
        return self._collect(root or self.root, Jump)

    def get_all_calls(self, root: Optional[Script] = None) -> List[Call]:
        return self._collect(root or self.root, Call)

    # ── private helpers ──

    def _collect(self, node: Node, cls: type) -> list:
        """Recursively collect nodes of a given type."""
        result = []
        if isinstance(node, cls):
            result.append(node)
        for child in self._children_of(node):
            result.extend(self._collect(child, cls))
        return result

    @staticmethod
    def _children_of(node: Node) -> List[Node]:
        """Return direct children of *node*."""
        kids: List[Node] = []
        if hasattr(node, "body") and isinstance(node.body, list):
            kids.extend(node.body)
        if isinstance(node, If):
            for ec in node.elif_clauses:
                kids.append(ec)
                kids.extend(ec.body)
            kids.extend(node.else_body)
        if isinstance(node, Menu):
            kids.extend(node.body)
        return kids

    def _parse_line(self, content: str, lineno: int, indent: int):
        """Classify a single stripped line and return a Node (or a tuple sentinel for elif/else)."""

        # ── Comment ──
        if content.startswith("#"):
            return Comment(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                text=content[1:].strip(),
            )

        # ── Label ──
        m = _RE_LABEL.match(content)
        if m:
            return Label(
                lineno=lineno,
                end_lineno=0,
                indent=indent,
                name=m.group(1),
                parameters=m.group(2),
            )

        # ── Define ──
        m = _RE_DEFINE.match(content)
        if m:
            return Define(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                name=m.group(1),
                expression=m.group(2).strip(),
            )

        # ── Default ──
        m = _RE_DEFAULT.match(content)
        if m:
            return Default(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                name=m.group(1),
                expression=m.group(2).strip(),
            )

        # ── Image ──
        m = _RE_IMAGE.match(content)
        if m:
            return ImageDef(
                lineno=lineno,
                end_lineno=0,
                indent=indent,
                name=m.group(1).strip(),
                expression=m.group(2),
            )

        # ── Transform ──
        m = _RE_TRANSFORM.match(content)
        if m:
            return TransformDef(
                lineno=lineno,
                end_lineno=0,
                indent=indent,
                name=m.group(1),
                parameters=m.group(2),
            )

        # ── Screen ──
        m = _RE_SCREEN.match(content)
        if m:
            return ScreenDef(
                lineno=lineno,
                end_lineno=0,
                indent=indent,
                name=m.group(1),
                parameters=m.group(2),
            )

        # ── Style ──
        m = _RE_STYLE.match(content)
        if m:
            return StyleDef(
                lineno=lineno,
                end_lineno=0,
                indent=indent,
                name=m.group(1),
                parent=m.group(2),
            )

        # ── Init offset ──
        m = _RE_INIT_OFFSET.match(content)
        if m:
            return InitOffset(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                offset=int(m.group(1)),
            )

        # ── Init ──
        m = _RE_INIT.match(content)
        if m:
            prio_str = m.group(1)
            return Init(
                lineno=lineno,
                end_lineno=0,
                indent=indent,
                priority=int(prio_str) if prio_str else None,
                is_python=m.group(2) is not None,
                is_early=m.group(3) is not None,
                namespace=m.group(4),
            )

        # ── Python block ──
        m = _RE_PYTHON_BLOCK.match(content)
        if m:
            return PythonBlock(
                lineno=lineno,
                end_lineno=0,
                indent=indent,
                is_early=m.group(1) is not None,
                namespace=m.group(2),
                is_hide=m.group(3) is not None,
            )

        # ── Python one-liner ($ ...) ──
        m = _RE_PYTHON_ONELINER.match(content)
        if m:
            return PythonOneliner(
                lineno=lineno, end_lineno=lineno, indent=indent, code=m.group(1).strip()
            )

        # ── Translate ──
        m = _RE_TRANSLATE.match(content)
        if m:
            return Translate(
                lineno=lineno,
                end_lineno=0,
                indent=indent,
                language=m.group(1),
                identifier=m.group(2),
            )

        # ── Control: if / elif / else ──
        m = _RE_IF.match(content)
        if m:
            return If(
                lineno=lineno, end_lineno=0, indent=indent, condition=m.group(1).strip()
            )

        m = _RE_ELIF.match(content)
        if m:
            return ("ELIF", m.group(1).strip(), lineno, indent)

        m = _RE_ELSE.match(content)
        if m:
            return ("ELSE", lineno, indent)

        # ── While / For ──
        m = _RE_WHILE.match(content)
        if m:
            return While(
                lineno=lineno, end_lineno=0, indent=indent, condition=m.group(1).strip()
            )

        m = _RE_FOR.match(content)
        if m:
            return For(
                lineno=lineno,
                end_lineno=0,
                indent=indent,
                variable=m.group(1),
                iterable=m.group(2).strip(),
            )

        # ── Menu ──
        m = _RE_MENU.match(content)
        if m:
            return Menu(lineno=lineno, end_lineno=0, indent=indent, name=m.group(1))

        # ── Menu item (must come after menu) ──
        m = _RE_MENU_ITEM.match(content)
        if m:
            return MenuItem(
                lineno=lineno,
                end_lineno=0,
                indent=indent,
                caption=m.group(1),
                condition=m.group(2),
            )

        # ── Flow: jump / call / return / pass ──
        m = _RE_JUMP.match(content)
        if m:
            return Jump(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                target=m.group(2),
                is_expression=m.group(1) is not None,
            )

        m = _RE_CALL_SCREEN.match(content)
        if m:
            return CallScreen(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                screen_name=m.group(1),
                arguments=m.group(2),
            )

        m = _RE_CALL.match(content)
        if m:
            return Call(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                target=m.group(2),
                arguments=m.group(3),
                from_label=m.group(4),
                is_expression=m.group(1) is not None,
            )

        if content == "return" or content.startswith("return "):
            val = content[6:].strip() or None
            return Return(lineno=lineno, end_lineno=lineno, indent=indent, value=val)

        if content == "pass":
            return Pass(lineno=lineno, end_lineno=lineno, indent=indent)

        # ── Visual: scene / show / hide / with ──
        m = _RE_SCENE.match(content)
        if m:
            return Scene(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                image=m.group(1).strip(),
                at_transform=m.group(2),
                with_transition=m.group(3),
                has_block=content.rstrip().endswith(":"),
            )

        m = _RE_SHOW_SCREEN.match(content)
        if m:
            return ShowScreen(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                screen_name=m.group(1),
                arguments=m.group(2),
            )

        m = _RE_SHOW.match(content)
        if m:
            return Show(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                image=m.group(1).strip(),
                at_transform=m.group(2),
                with_transition=m.group(3),
                has_block=content.rstrip().endswith(":"),
            )

        m = _RE_HIDE.match(content)
        if m:
            return Hide(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                image=m.group(1).strip(),
                with_transition=m.group(2),
                has_block=content.rstrip().endswith(":"),
            )

        m = _RE_WITH.match(content)
        if m:
            return With(
                lineno=lineno, end_lineno=lineno, indent=indent, transition=m.group(1)
            )

        # ── Audio: play / stop / queue / voice ──
        m = _RE_PLAY.match(content)
        if m:
            return PlayMusic(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                channel=m.group(1),
                filename=m.group(2) or m.group(3),
                options=m.group(4).strip(),
            )

        m = _RE_STOP.match(content)
        if m:
            return StopMusic(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                channel=m.group(1),
                options=m.group(2).strip(),
            )

        m = _RE_QUEUE.match(content)
        if m:
            return QueueMusic(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                channel=m.group(1),
                filename=m.group(2) or m.group(3),
            )

        m = _RE_VOICE.match(content)
        if m:
            return Voice(
                lineno=lineno, end_lineno=lineno, indent=indent, filename=m.group(1)
            )

        # ── NVL clear/show/hide ──
        m = _RE_NVL.match(content)
        if m:
            return NvlClear(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                action=m.group(1),
            )

        # ── Window show/hide/auto ──
        m = _RE_WINDOW.match(content)
        if m:
            return Window(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                action=m.group(1),
            )

        # ── Pause ──
        m = _RE_PAUSE.match(content)
        if m:
            return Pause(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                duration=m.group(1),
            )

        # ── Say dialogue ──
        m = _RE_SAY.match(content)
        if m:
            return Say(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                who=m.group(1),
                what=m.group(2),
            )

        # ── Say without space (e.g. n"text") ──
        m = _RE_SAY_NOSPACE.match(content)
        if m:
            return Say(
                lineno=lineno,
                end_lineno=lineno,
                indent=indent,
                who=m.group(1),
                what=m.group(2),
            )

        m = _RE_NARRATOR.match(content)
        if m:
            return NarratorSay(
                lineno=lineno, end_lineno=lineno, indent=indent, what=m.group(1)
            )

        # ── Unknown ──
        self._errors.append((lineno, f"Unrecognized statement: {content}"))
        return Unknown(lineno=lineno, end_lineno=lineno, indent=indent, text=content)

    # ── elif / else attachment ──

    def _attach_elif(self, stack, parent, condition, lineno, indent):
        """Attach an ``elif`` branch to the most recent :class:`If` on the stack."""
        target_if = self._find_if_on_stack(stack, parent, indent)
        if target_if is None:
            self._errors.append((lineno, "elif without matching if"))
            return
        elif_node = Elif(
            lineno=lineno, end_lineno=0, indent=indent, condition=condition
        )
        target_if.elif_clauses.append(elif_node)
        # Push elif so its children go into elif_node.body
        stack.append(_StackEntry(indent + 1, elif_node))

    def _attach_else(self, stack, parent, lineno, indent):
        """Attach an ``else`` block to the most recent :class:`If` on the stack."""
        target_if = self._find_if_on_stack(stack, parent, indent)
        if target_if is None:
            self._errors.append((lineno, "else without matching if"))
            return
        # Create a pseudo-node to collect else-body children.
        else_container = If(
            lineno=lineno, end_lineno=0, indent=indent, condition="__else__"
        )
        target_if.else_body = else_container.body
        stack.append(_StackEntry(indent + 1, else_container))

    @staticmethod
    def _find_if_on_stack(stack, parent, indent) -> Optional[If]:
        """Walk the stack backwards to find the If node matching this elif/else indent."""
        # First check direct parent body
        if isinstance(
            parent,
            (
                Script,
                Label,
                Menu,
                MenuItem,
                Init,
                PythonBlock,
                ScreenDef,
                TransformDef,
                While,
                For,
                Translate,
                If,
                Elif,
            ),
        ):
            body = getattr(parent, "body", [])
            for n in reversed(body):
                if isinstance(n, If) and n.indent == indent:
                    return n
        # Fallback: scan stack
        for entry in reversed(stack):
            if isinstance(entry.node, If) and entry.node.indent == indent:
                return entry.node
        return None

    @staticmethod
    def _append_to_parent(parent: Node, child: Node):
        """Append *child* to the appropriate list on *parent*."""
        if hasattr(parent, "body") and isinstance(parent.body, list):
            parent.body.append(child)

    @staticmethod
    def _is_block_node(node: Node) -> bool:
        """Return True if *node* can contain children (has a body list)."""
        # Scene/Show/Hide are block nodes only when they have a trailing colon (ATL block)
        if isinstance(node, (Scene, Show, Hide)):
            return node.has_block
        return isinstance(
            node,
            (
                Label,
                If,
                Elif,
                While,
                For,
                Menu,
                MenuItem,
                Init,
                PythonBlock,
                ScreenDef,
                TransformDef,
                StyleDef,
                ImageDef,
                Translate,
            ),
        )


# ──────────────────────────── Self-test ──────────────────────────────────

if __name__ == "__main__":
    sample = """\
# 这是一个完整测试

define e = Character("艾琳", color="#c8ffc8")
define narrator = Character(None, kind=nvl)
default player_name = "玩家"
default player_level = 1
default has_key = False

image bg meadow = "bg/meadow.png"
image eileen happy = "eileen/happy.png"

transform my_dissolve:
    alpha 0.0
    linear 1.0 alpha 1.0

init python:
    import os
    config.screen_width = 1920

init -1 python:
    renpy.music.register_channel("ambient", mixer="sfx")

screen inventory_screen():
    tag menu
    frame:
        vbox:
            text "背包"

label start:
    scene bg meadow with dissolve
    show eileen happy at center with fade

    "这是旁白。"
    e "你好！欢迎来到 Ren'Py 的世界！"
    e "你的名字是 [player_name]。"

    $ player_level += 1

    if player_level > 10:
        e "你已经很强了。"
    elif player_level > 5:
        e "再努力一下！"
    else:
        e "继续加油吧。"

    menu:
        "选择你的命运"
        "走左边的路":
            jump left_path
        "走右边的路":
            jump right_path
        "留在原地" if has_key:
            e "你决定留在原地。"

    return

label left_path:
    play music "bgm/adventure.ogg" fadein 1.0
    voice "voice/eileen_001.ogg"
    e "这边风景不错！"
    hide eileen with dissolve
    call right_path from _left_call
    return

label right_path:
    stop music fadeout 2.0
    play sound "sfx/footstep.ogg"
    show eileen sad at left
    e "这边好像有些危险……"
    jump start

translate english start_abc123:
    e "Hello! Welcome to Ren'Py!"
"""
    import pprint

    parser = RpyParser(sample)
    ast = parser.parse()

    print("=" * 60)
    print("AST:")
    print("=" * 60)
    pprint.pp(ast)

    print("\n" + "=" * 60)
    print("Labels:")
    for lb in parser.get_all_labels():
        print(f"  {lb.name} (line {lb.lineno}-{lb.end_lineno})")

    print("\nDefines:")
    for d in parser.get_all_defines():
        print(f"  {d.name} = {d.expression} (line {d.lineno})")

    print("\nDefaults:")
    for d in parser.get_all_defaults():
        print(f"  {d.name} = {d.expression} (line {d.lineno})")

    print("\nCharacters:")
    for c in parser.get_all_characters():
        print(f"  {c.name} = {c.expression} (line {c.lineno})")

    print("\nScreens:")
    for s in parser.get_all_screens():
        print(f"  {s.name} (line {s.lineno}-{s.end_lineno})")

    print("\nJumps:")
    for j in parser.get_all_jumps():
        print(f"  jump {j.target} (line {j.lineno})")

    print("\nCalls:")
    for c in parser.get_all_calls():
        print(f"  call {c.target} (line {c.lineno})")

    if parser.errors:
        print("\n⚠️ Parse errors:")
        for ln, msg in parser.errors:
            print(f"  Line {ln}: {msg}")
    else:
        print("\n✅ No parse errors.")
