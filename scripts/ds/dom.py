# -*- coding: utf-8 -*-
"""
ds.dom —— 零依赖迷你 DOM + CSS 子集选择器

为什么不用 BeautifulSoup：本技能的运行环境（系统 Python）常常没有 bs4/lxml，
而深度搜索的价值恰恰在于"随手就能跑"。标准库的 html.parser 足够构建一棵可用的树。

支持的选择器子集（覆盖搜索结果页 99% 的用法）：
    div  .class  #id  [attr]  [attr=value]  [attr*=value]  [attr^=value]  [attr$=value]
    "A B" 后代      "A > B" 子代      "A, B" 分组

不支持伪类/伪元素 —— 遇到就静默忽略，绝不因为一个选择器写错而整页失败。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

# HTML 空元素：没有闭合标签，不能压栈
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr", "command", "keygen",
}

# 这些标签遇到新的同类/块级标签时应隐式闭合，否则树会畸形
AUTO_CLOSE = {
    "p", "li", "dt", "dd", "option", "thead", "tbody", "tfoot",
    "tr", "td", "th", "optgroup",
}

# 解析与提取时都应跳过的标签（内容不是正文）
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "canvas", "iframe"}

BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "aside", "header", "footer",
    "li", "td", "th", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6",
    "figcaption", "dd", "dt", "tr", "table", "ul", "ol", "dl", "form",
}


class Node:
    """极简 DOM 节点。children 里同时放 Node 和 str。"""

    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: Optional[Dict[str, str]] = None):
        self.tag = tag
        self.attrs: Dict[str, str] = attrs or {}
        self.children: List[Any] = []
        self.parent: Optional["Node"] = None

    # -------------------------------------------------- 属性访问

    def attr(self, name: str, default: str = "") -> str:
        return self.attrs.get(name.lower(), default)

    @property
    def classes(self) -> List[str]:
        return self.attr("class").split()

    @property
    def id(self) -> str:
        return self.attr("id")

    def has_class(self, cls: str) -> bool:
        return cls in self.classes

    # -------------------------------------------------- 遍历

    def iter_nodes(self) -> Iterator["Node"]:
        """深度优先遍历自身及所有后代元素节点。"""
        stack = [self]
        while stack:
            n = stack.pop()
            yield n
            for c in reversed(n.children):
                if isinstance(c, Node):
                    stack.append(c)

    def iter_text(self) -> Iterator[str]:
        for c in self.children:
            if isinstance(c, str):
                yield c
            elif c.tag not in SKIP_TAGS:
                yield from c.iter_text()

    def get_text(self, sep: str = " ", strip: bool = True) -> str:
        parts: List[str] = []
        for c in self.children:
            if isinstance(c, str):
                parts.append(c)
            elif c.tag not in SKIP_TAGS:
                parts.append(c.get_text(sep, strip))
        txt = sep.join(p for p in parts if p)
        txt = re.sub(r"\s+", " ", txt)
        return txt.strip() if strip else txt

    def find_all(self, tag: str) -> List["Node"]:
        return [n for n in self.iter_nodes() if n is not self and n.tag == tag]

    def find(self, tag: str) -> Optional["Node"]:
        for n in self.iter_nodes():
            if n is not self and n.tag == tag:
                return n
        return None

    def ancestors(self) -> Iterator["Node"]:
        p = self.parent
        while p is not None:
            yield p
            p = p.parent

    def depth(self) -> int:
        return sum(1 for _ in self.ancestors())

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        cls = ".".join(self.classes[:2])
        return f"<{self.tag}{'.' + cls if cls else ''} children={len(self.children)}>"


class _TreeBuilder(HTMLParser):
    """把 HTML 流构造成 Node 树。对畸形 HTML 尽量宽容。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: List[Node] = [self.root]
        self._skip_depth = 0

    # -- 内部

    def _add(self, node: Node) -> None:
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)

    def _close_to(self, tag: str) -> bool:
        """向上找到同名开标签并闭合；找不到返回 False。"""
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return True
        return False

    # -- HTMLParser 回调

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self._skip_depth > 0:
            if tag in SKIP_TAGS:
                self._skip_depth += 1
            return

        if tag in SKIP_TAGS:
            # 仍然建节点（保留位置），但内容不参与文本提取
            node = Node(tag, {k.lower(): (v or "") for k, v in attrs})
            self._add(node)
            if tag not in VOID_TAGS:
                self.stack.append(node)
                self._skip_depth = 1
            return

        # 隐式闭合：<li> 遇到新的 <li>，<p> 遇到块级标签
        if tag in AUTO_CLOSE:
            top = self.stack[-1]
            if top.tag == tag:
                self.stack.pop()
            elif tag in ("td", "th") and top.tag in ("td", "th"):
                self.stack.pop()

        node = Node(tag, {k.lower(): (v or "") for k, v in attrs})
        self._add(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self._close_to(tag.lower())

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth > 0:
            if tag in SKIP_TAGS:
                self._skip_depth -= 1
                self._close_to(tag)
            return
        if tag in VOID_TAGS:
            return
        self._close_to(tag)

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0 or not data:
            return
        # 纯空白文本节点没有价值，丢掉以缩小树
        if not data.strip():
            if data and isinstance(self.stack[-1].children[-1] if self.stack[-1].children else None, str):
                return
            return
        self.stack[-1].children.append(data)


def parse(html: str) -> Node:
    """把 HTML 字符串解析成 DOM 树。永不抛异常。"""
    builder = _TreeBuilder()
    try:
        builder.feed(html)
        builder.close()
    except Exception:
        # 解析中断也要返回已经建好的部分
        pass
    return builder.root


# ---------------------------------------------------------------- 选择器

_SIMPLE_RE = re.compile(
    r"""
    (?P<tag>[\w\-\*]+)?                                  # 标签名
    (?P<rest>(?:\#[^.#\[\]\s>+,]+|\.[^.#\[\]\s>+,]+|\[[^\]]*\])*)
    """,
    re.VERBOSE,
)
_ATTR_RE = re.compile(
    r"""\[\s*(?P<name>[\w\-:]+)\s*(?:(?P<op>[~^$*|]?=)\s*(?P<val>"[^"]*"|'[^']*'|[^\]\s]*))?\s*\]"""
)


class _Compound:
    """一个复合选择器：tag.class#id[attr=val]"""

    __slots__ = ("tag", "classes", "ids", "attrs")

    def __init__(self, text: str):
        self.tag: Optional[str] = None
        self.classes: List[str] = []
        self.ids: List[str] = []
        self.attrs: List[Tuple[str, str, str]] = []

        m = _SIMPLE_RE.fullmatch(text.strip())
        if not m:
            self.tag = text.strip().lower() or None
            return

        tag = m.group("tag")
        if tag and tag != "*":
            self.tag = tag.lower()

        rest = m.group("rest") or ""
        for tok in re.findall(r"\#[^.#\[\]\s>+,]+|\.[^.#\[\]\s>+,]+|\[[^\]]*\]", rest):
            if tok.startswith("."):
                self.classes.append(tok[1:])
            elif tok.startswith("#"):
                self.ids.append(tok[1:])
            else:
                am = _ATTR_RE.fullmatch(tok)
                if am:
                    name = am.group("name").lower()
                    op = am.group("op") or ""
                    val = am.group("val") or ""
                    if val[:1] in ("'", '"') and val[-1:] == val[:1]:
                        val = val[1:-1]
                    self.attrs.append((name, op, val))

    def matches(self, node: Node) -> bool:
        if self.tag and node.tag != self.tag:
            return False
        if self.ids and node.attr("id") not in self.ids:
            return False
        if self.classes:
            ncls = node.classes
            for c in self.classes:
                if c not in ncls:
                    return False
        for name, op, val in self.attrs:
            if name not in node.attrs:
                return False
            if not op:
                continue
            actual = node.attrs.get(name, "")
            if op == "=" and actual != val:
                return False
            if op == "*=" and val not in actual:
                return False
            if op == "^=" and not actual.startswith(val):
                return False
            if op == "$=" and not actual.endswith(val):
                return False
            if op == "~=" and val not in actual.split():
                return False
            if op == "|=" and not (actual == val or actual.startswith(val + "-")):
                return False
        return True


class _Selector:
    """一条复合选择器链，例如 "div.result > a.title"。"""

    def __init__(self, text: str):
        self.steps: List[Tuple[str, _Compound]] = []  # (combinator, compound)
        # 去掉不支持的伪类，避免整体失效
        text = re.sub(r":{1,2}[\w\-]+(\([^)]*\))?", "", text or "")
        text = text.strip()
        if not text:
            self.steps = [(" ", _Compound("*"))]
            return

        tokens = re.split(r"\s*(>)\s*|\s+", text)
        tokens = [t for t in tokens if t not in (None, "")]
        combinator = " "
        for tok in tokens:
            if tok == ">":
                combinator = ">"
                continue
            self.steps.append((combinator, _Compound(tok)))
            combinator = " "

    def select(self, root: Node) -> List[Node]:
        current: List[Node] = [root]
        for combinator, compound in self.steps:
            nxt: List[Node] = []
            seen = set()
            for base in current:
                if combinator == ">":
                    pool = [c for c in base.children if isinstance(c, Node)]
                else:
                    pool = [n for n in base.iter_nodes() if n is not base]
                for n in pool:
                    if id(n) in seen:
                        continue
                    if compound.matches(n):
                        seen.add(id(n))
                        nxt.append(n)
            current = nxt
            if not current:
                break
        return current


_SELECTOR_CACHE: Dict[str, List[_Selector]] = {}


def compile_selector(selector: str) -> List[_Selector]:
    if selector not in _SELECTOR_CACHE:
        parts = [_Selector(p) for p in selector.split(",") if p.strip()]
        _SELECTOR_CACHE[selector] = parts or [_Selector("*")]
    return _SELECTOR_CACHE[selector]


def select(root: Node, selector: str) -> List[Node]:
    """CSS 子集查询，返回文档序去重后的节点列表。"""
    out: List[Node] = []
    seen = set()
    for sel in compile_selector(selector):
        for n in sel.select(root):
            if id(n) not in seen:
                seen.add(id(n))
                out.append(n)
    return out


def select_one(root: Node, selector: str) -> Optional[Node]:
    for sel in compile_selector(selector):
        hits = sel.select(root)
        if hits:
            return hits[0]
    return None


def select_first(root: Node, selectors: Sequence[str]) -> Optional[Node]:
    """按顺序尝试多个选择器，返回第一个命中的节点。"""
    for s in selectors:
        n = select_one(root, s)
        if n is not None:
            return n
    return None


def select_all_first(root: Node, selectors: Sequence[str]) -> List[Node]:
    """按顺序尝试多个选择器，返回第一个有结果的那组。"""
    for s in selectors:
        hits = select(root, s)
        if hits:
            return hits
    return []
