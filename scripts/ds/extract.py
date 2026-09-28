# -*- coding: utf-8 -*-
"""
ds.extract —— 正文抽取（readability 精简版，零依赖）

搜索结果只给摘要，摘要答不了"这份材料到底怎么说"。
深度搜索必须把页面正文拿回来，否则"深"只是多跑几个引擎而已。

算法（Readability 的思路，去掉复杂度）：
1. 剔除明显的非正文容器（导航/页脚/评论/广告/侧栏）
2. 给每个块级候选打分：文本长度 + 标点密度 − 链接密度，再按 class/id 语义加权
3. 分数向父节点部分传播（正文常被包在无意义的 div 里）
4. 取最高分节点，并入得分接近的同级兄弟
5. 兜底：整页正文文本（仍然去样板）

输出既有人类可读的 text，也有按段切好的 paragraphs —— 后者是后续做
抽取式摘要和引证定位的基础。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import dom
from .dom import Node

_THIS_YEAR = datetime.now().year

# ---------------------------------------------------------------- 语义权重

POSITIVE_HINT = re.compile(
    r"article|content|post|entry|main|body|text|story|detail|markdown|"
    r"rich_media|js_content|post-content|article-content|entry-content|"
    r"正文|内容|文章",
    re.IGNORECASE,
)
NEGATIVE_HINT = re.compile(
    r"comment|sidebar|footer|header|nav|menu|breadcrumb|advert|\bads?\b|"
    r"share|social|related|recommend|promo|banner|popup|modal|subscribe|"
    r"copyright|pagination|tag-list|widget|aside|meta-|toolbar|"
    r"评论|推荐|广告|导航|页脚|侧栏|相关阅读",
    re.IGNORECASE,
)
UNLIKELY_TAGS = {"nav", "footer", "aside", "form", "button", "select", "textarea"}

# 中文标点也计入"像正文"的证据
SENTENCE_PUNCT = re.compile(r"[。！？；…]|[.!?;](?:\s|$)")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

MIN_PARAGRAPH_CHARS = 25


@dataclass
class Article:
    """一篇文章的抽取结果。"""

    url: str = ""
    title: str = ""
    text: str = ""
    paragraphs: List[str] = field(default_factory=list)
    author: str = ""
    site: str = ""
    published: str = ""
    lang: str = ""
    word_count: int = 0
    char_count: int = 0
    extractor: str = "readability-lite"

    @property
    def ok(self) -> bool:
        return len(self.text) >= 200

    def to_dict(self) -> Dict:
        return {
            "url": self.url,
            "title": self.title,
            "author": self.author,
            "site": self.site,
            "published": self.published,
            "lang": self.lang,
            "char_count": self.char_count,
            "word_count": self.word_count,
            "paragraphs": len(self.paragraphs),
        }


# ---------------------------------------------------------------- 元数据

def _meta_map(root: Node) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in dom.select(root, "meta"):
        key = (m.attr("property") or m.attr("name") or m.attr("itemprop")).strip().lower()
        if key and key not in out:
            out[key] = m.attr("content").strip()
    return out


def _first_text(root: Node, selectors: List[str]) -> str:
    for sel in selectors:
        n = dom.select_one(root, sel)
        if n is not None:
            t = n.get_text()
            if t:
                return t
    return ""


def extract_metadata(root: Node, html: str = "") -> Dict[str, str]:
    """抽取标题/作者/站点/时间/语言。"""
    meta = _meta_map(root)

    title = (
        meta.get("og:title")
        or meta.get("twitter:title")
        or _first_text(root, ["h1", "title"])
    )
    # <title> 常带 "标题 - 站点" 后缀，剥掉
    if not title and html:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()

    site = meta.get("og:site_name") or ""
    if not site:
        n = dom.select_one(root, "meta[name=application-name]")
        if n is not None:
            site = n.attr("content")

    author = (
        meta.get("author")
        or meta.get("article:author")
        or meta.get("og:article:author")
        or _first_text(root, [".author", ".byline", "[rel=author]", ".article-author"])
    )

    # 日期：只信高置信来源（meta / <time datetime>）。
    # 宁可为空也不要错的 —— 页脚和侧栏里的日期会把时效性判断带偏。
    published = ""
    for key in ("article:published_time", "og:published_time", "datepublished",
                "date", "pubdate", "publishdate", "og:release_date", "sailthru.date"):
        v = meta.get(key, "")
        if v:
            published = v
            break
    if not published:
        n = dom.select_one(root, "time[datetime]")
        if n is not None:
            published = n.attr("datetime")

    m = re.search(r"((?:19|20)\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", published or "")
    if m:
        published = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    else:
        m2 = re.search(r"((?:19|20)\d{2})", published or "")
        published = m2.group(1) if m2 else ""

    # 合理性校验：明显不可能是该文章发布时间的，直接丢掉
    if published[:4].isdigit():
        y = int(published[:4])
        if y < 2005 or y > _THIS_YEAR + 1:
            published = ""

    lang = ""
    n = dom.select_one(root, "html")
    if n is not None:
        lang = n.attr("lang") or n.attr("xml:lang")
    if not lang:
        lang = meta.get("og:locale", "")
    if not lang:
        sample = (title or "") + (html[:2000] if html else "")
        lang = "zh" if CJK_RE.search(sample) else "en"

    return {
        "title": _clean(title)[:300],
        "site": _clean(site)[:80],
        "author": _clean(author)[:80],
        "published": _clean(published)[:40],
        "lang": lang[:12],
    }


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


# ---------------------------------------------------------------- 文本转换

def node_to_text(node: Node) -> str:
    """把子树转成带段落结构的纯文本。"""
    chunks: List[str] = []

    def walk(n: Node) -> None:
        if n.tag in dom.SKIP_TAGS:
            return
        if n.tag == "br":
            chunks.append("\n")
            return
        if n.tag == "li":
            chunks.append("\n- ")
        elif n.tag in dom.BLOCK_TAGS:
            if chunks and not chunks[-1].endswith("\n\n"):
                chunks.append("\n\n")
        for c in n.children:
            if isinstance(c, str):
                chunks.append(c)
            else:
                walk(c)
        if n.tag in dom.BLOCK_TAGS and chunks and not chunks[-1].endswith("\n\n"):
            chunks.append("\n\n")

    walk(node)
    text = "".join(chunks)
    text = re.sub(r"[ \t\u00a0\u3000]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_paragraphs(text: str) -> List[str]:
    """按空行切段，过滤过短/无意义的行。"""
    out: List[str] = []
    for raw in re.split(r"\n\s*\n", text):
        p = _clean(raw)
        if len(p) < MIN_PARAGRAPH_CHARS:
            continue
        # 纯符号/纯数字的行不算段落
        if not re.search(r"[\w\u4e00-\u9fff]", p):
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------- 打分

def _link_density(node: Node) -> float:
    total = len(node.get_text())
    if total == 0:
        return 1.0
    link_len = 0
    for a in node.find_all("a"):
        link_len += len(a.get_text())
    return min(1.0, link_len / total)


def _class_weight(node: Node) -> float:
    sig = " ".join([node.attr("id"), node.attr("class")]).strip()
    if not sig:
        return 0.0
    w = 0.0
    if POSITIVE_HINT.search(sig):
        w += 30.0
    if NEGATIVE_HINT.search(sig):
        w -= 40.0
    return w


def _text_score(text: str) -> float:
    """文本本身像不像正文。"""
    n = len(text)
    if n == 0:
        return 0.0
    # 长度分：收益递减
    score = min(n, 1500) / 100.0
    # 标点密度：正文句子多
    punct = len(SENTENCE_PUNCT.findall(text))
    score += min(punct, 60) * 0.9
    # 中文按字计，英文按词计
    if CJK_RE.search(text):
        score += min(n / 120.0, 12.0)
    else:
        score += min(len(text.split()) / 25.0, 12.0)
    return score


def _candidate_containers(root: Node) -> List[Node]:
    out: List[Node] = []
    for n in root.iter_nodes():
        if n.tag in UNLIKELY_TAGS or n.tag in dom.SKIP_TAGS:
            continue
        if n.tag in ("p", "article", "section", "main", "div", "td", "blockquote", "pre"):
            out.append(n)
    return out


def strip_boilerplate(root: Node) -> None:
    """原地删除明显的样板容器。"""
    doomed: List[Node] = []
    for n in root.iter_nodes():
        if n is root:
            continue
        if n.tag in ("nav", "footer", "aside", "form"):
            doomed.append(n)
            continue
        sig = " ".join([n.attr("id"), n.attr("class")])
        if sig and NEGATIVE_HINT.search(sig):
            # 只有在该容器确实"不像正文"时才删，避免误杀正文容器
            if _link_density(n) > 0.35 or len(n.get_text()) < 400:
                doomed.append(n)
    for n in doomed:
        p = n.parent
        if p is not None and n in p.children:
            p.children.remove(n)


def extract_main(root: Node) -> Tuple[Optional[Node], float]:
    """返回 (最佳正文节点, 得分)。"""
    scores: Dict[int, float] = {}
    nodes: Dict[int, Node] = {}

    for node in _candidate_containers(root):
        text = node.get_text()
        n = len(text)
        if n < 40:
            continue
        density = _link_density(node)
        if density > 0.6 and n < 800:
            continue  # 链接堆，是导航不是正文

        score = _text_score(text) * (1.0 - density) + _class_weight(node)

        # 段落多的容器更像正文
        p_count = sum(1 for c in node.children if isinstance(c, Node) and c.tag == "p")
        score += min(p_count, 20) * 1.2

        scores[id(node)] = score
        nodes[id(node)] = node

        # 分数向父级部分传播：正文常被包在无语义的 div 里
        parent = node.parent
        if parent is not None and parent.tag not in ("html", "body", "#document"):
            scores[id(parent)] = scores.get(id(parent), 0.0) + score * 0.35
            nodes.setdefault(id(parent), parent)

    if not scores:
        return None, 0.0

    best_id = max(scores, key=lambda k: scores[k])
    return nodes[best_id], scores[best_id]


def _merge_siblings(best: Node, scores: Dict[int, float]) -> List[Node]:
    """并入得分与最佳节点同量级的兄弟节点，找回被切碎的正文。"""
    parent = best.parent
    if parent is None:
        return [best]
    best_score = scores.get(id(best), 0.0)
    if best_score <= 0:
        return [best]

    out = [best]
    for sib in parent.children:
        if not isinstance(sib, Node) or sib is best:
            continue
        if sib.tag in dom.SKIP_TAGS or sib.tag in ("nav", "footer", "aside"):
            continue
        s = scores.get(id(sib), 0.0)
        if s >= best_score * 0.3:
            # 段落/链接密度要合理
            if _link_density(sib) < 0.5:
                out.append(sib)
    # 保持文档顺序
    order = {id(c): i for i, c in enumerate(parent.children)}
    out.sort(key=lambda n: order.get(id(n), 0))
    return out


# ---------------------------------------------------------------- 主入口

def extract_article(html: str, url: str = "") -> Article:
    """从 HTML 抽取正文。永不抛异常。"""
    if not html or len(html) < 100:
        return Article(url=url, extractor="empty")

    root = dom.parse(html)
    meta = extract_metadata(root, html)

    try:
        strip_boilerplate(root)
    except Exception:
        pass

    art = Article(
        url=url,
        title=meta["title"],
        author=meta["author"],
        site=meta["site"],
        published=meta["published"],
        lang=meta["lang"],
    )

    # 第一优先：语义化标签
    main_node = dom.select_one(root, "article") or dom.select_one(root, "main")
    text = ""
    if main_node is not None and len(main_node.get_text()) >= 200:
        text = node_to_text(main_node)
        art.extractor = "semantic"

    if len(text) < 200:
        best, score = extract_main(root)
        if best is not None and score > 0:
            merged = _merge_siblings(best, {id(best): score})
            text = "\n\n".join(node_to_text(n) for n in merged)
            art.extractor = "readability-lite"

    if len(text) < 200:
        body = dom.select_one(root, "body") or root
        text = node_to_text(body)
        art.extractor = "body-fallback"

    # 去掉重复的标题行
    if art.title:
        text = re.sub(r"^\s*" + re.escape(art.title) + r"\s*", "", text, count=1)

    art.text = text.strip()
    art.paragraphs = split_paragraphs(art.text)
    art.char_count = len(art.text)
    art.word_count = len(art.text.split()) if not CJK_RE.search(art.text) else art.char_count
    return art


# ---------------------------------------------------------------- 摘要片段

def make_snippet(text: str, query_terms: List[str], width: int = 260) -> str:
    """在正文中找与查询最相关的一段，作为引证片段。"""
    if not text:
        return ""
    if not query_terms:
        return _clean(text[:width])

    low = text.lower()
    best_pos, best_hits = 0, 0
    for term in query_terms[:12]:
        t = term.lower().strip()
        if len(t) < 2:
            continue
        pos = 0
        while True:
            i = low.find(t, pos)
            if i < 0:
                break
            # 以命中点为中心开窗，数窗内命中了多少个查询词
            start = max(0, i - width // 3)
            window = low[start:start + width]
            hits = sum(1 for x in query_terms if x.lower() in window)
            if hits > best_hits:
                best_hits, best_pos = hits, start
            pos = i + len(t)
        if best_hits >= 3:
            break

    seg = text[best_pos:best_pos + width]
    if best_pos > 0:
        seg = "…" + seg
    if best_pos + width < len(text):
        seg = seg + "…"
    return _clean(seg)
