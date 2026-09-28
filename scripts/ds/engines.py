# -*- coding: utf-8 -*-
"""
ds.engines —— 引擎注册表：API 适配器 + SERP 解析器 + 意图路由

设计原则（来自实测，不是猜的）：

1. **能用 API 就别爬页面。** arXiv / Crossref / OpenAlex / GitHub / StackExchange /
   HackerNews / Wikipedia 都有免鉴权 JSON 接口，结构稳定、不会因为改版而失效。
   它们是深度搜索的骨干。
2. **SERP 必须容错。** 搜索引擎随时改版、随时反爬。所以每个 SERP 引擎都配一条
   通用兜底解析器（按"外链 + 锚文本 + 邻近文本"启发式提取），
   特定解析器只是提高精度，不是唯一依赖。
3. **失败要静默。** 单引擎挂掉绝不影响整轮搜索 —— 这是"聚合"的意义。

实测结论（本机网络）：bing/baidu/360 可解析；DDG/Yahoo 不可达；
Brave 易 429。已据此排布权重与顺序。
"""

from __future__ import annotations

import base64
import html as htmllib
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import dom
from .dom import Node
from .net import HttpClient, domain_of

# ---------------------------------------------------------------- 数据结构


@dataclass
class Hit:
    """一条搜索结果。"""

    title: str
    url: str
    snippet: str = ""
    engine_id: str = ""
    engine_name: str = ""
    category: str = "general"
    weight: float = 0.7
    position: int = 0
    query: str = ""
    extra: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        d = {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "engine": self.engine_name,
            "engine_id": self.engine_id,
            "category": self.category,
            "position": self.position,
            "query": self.query,
        }
        if self.extra:
            d["extra"] = self.extra
        return d


@dataclass
class Engine:
    id: str
    name: str
    category: str
    weight: float
    fn: Callable[[HttpClient, str, int], List[Hit]]
    note: str = ""
    tier: int = 1  # 1=可靠(API/稳定SERP) 2=尽力而为
    timeout: float = 12.0  # 单请求超时；慢引擎必须单独收紧，否则拖垮整轮
    english_only: bool = False  # 只吃英文的引擎（GitHub/arXiv 等）


# ---------------------------------------------------------------- 工具


def _clean(s: str) -> str:
    if not s:
        return ""
    s = htmllib.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    # 搜索引擎的高亮会把中文词拆开（"固态电池 技术的 最新进展"），
    # 中文之间的空格是噪声，去掉；中英之间的空格保留。
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)
    return s.strip()


def _strip_tags_keep_text(html_frag: str) -> str:
    return _clean(html_frag)


_CJK_ANY = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]+")


def strip_cjk(s: str) -> str:
    """
    去掉中日韩字符，只留拉丁部分。
    GitHub / arXiv / StackExchange 这类库只索引英文，混入中文会让
    召回直接归零（"python asyncio 超时处理" → 0 条；"python asyncio" → 正常）。
    """
    return re.sub(r"\s+", " ", _CJK_ANY.sub(" ", s or "")).strip()


def unwrap_redirect(url: str) -> str:
    """解掉 Bing 的 /ck/a 跳转（base64 里藏着真实 URL）。"""
    if not url:
        return url
    if "bing.com/ck/a" in url:
        m = re.search(r"[?&]u=a1([^&]+)", url)
        if m:
            b64 = urllib.parse.unquote(m.group(1))
            pad = "=" * (-len(b64) % 4)
            try:
                return base64.urlsafe_b64decode(b64 + pad).decode("utf-8", "replace")
            except Exception:
                pass
    return url


def is_probably_content_url(url: str) -> bool:
    """过滤掉明显的非内容链接（分享/登录/播放器/资源）。"""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    low = url.lower()
    bad = (
        "javascript:", "mailto:", ".css", ".js", ".png", ".jpg", ".jpeg", ".gif",
        ".svg", ".ico", ".woff", ".mp4", ".mp3", "/login", "/signin", "/signup",
        "/register", "share?", "share=", "/share/", "javascript", "passport.",
        "account.", "/help/", "/privacy", "/terms", "/about", "/contact",
        "beian.", "/feedback", "/setting",
        # 跳转/推广链：抓回来不是内容
        "fwlink", "go.microsoft.com", "bing.com/ck/a", "/link?url=",
        "doubleclick", "googleadservices", "utm_source=ad",
    )
    if any(b in low for b in bad):
        return False
    # 纯首页/栏目页信息量低，但不算错，只是降权（交给排序）
    return True


def _mk(engine: Engine, title: str, url: str, snippet: str, pos: int, query: str,
        extra: Optional[Dict] = None) -> Hit:
    return Hit(
        title=_clean(title)[:300],
        url=unwrap_redirect(url.strip())[:900],
        snippet=_clean(snippet)[:600],
        engine_id=engine.id,
        engine_name=engine.name,
        category=engine.category,
        weight=engine.weight,
        position=pos,
        query=query,
        extra=extra or {},
    )


# ---------------------------------------------------------------- 通用 SERP 解析

# 结果容器常见的 class/id 提示
RESULT_HINT = re.compile(
    r"result|b_algo|b_ans|g-card|res-list|snippet|web-result|serp|algo|"
    r"search-item|list-item|card|item",
    re.IGNORECASE,
)
CHROME_HINT = re.compile(
    r"nav|menu|header|footer|sidebar|breadcrumb|pager|pagination|"
    r"related|recommend|advert|\bads?\b|promo|toolbar|login|share|social|"
    r"tab|filter|sort|logo|banner",
    re.IGNORECASE,
)


# 站点导航/页脚/交互控件的锚文本，抓到就是噪声
JUNK_ANCHOR = re.compile(
    r"^(首页|主页|登录|注册|反馈|意见反馈|举报|更多|查看|下载|关于我们|联系我们|"
    r"用户协议|隐私政策|版权|广告|帮助|客服|设置|订阅|分享|收藏|评论|上一页|下一页|"
    r"home|login|sign\s?in|sign\s?up|register|about|contact|privacy|terms|cookie|"
    r"feedback|report|more|download|subscribe|share|settings|help|advertise|"
    r"newsletter|careers|jobs|blog|pricing|docs?|support|legal|sitemap)$",
    re.IGNORECASE,
)
# 锚文本里出现这些词，基本可以判定是站点自身功能而非搜索结果
JUNK_SUBSTR = re.compile(
    r"(意见反馈|我要举报|了解必应|隐私声明|用户协议|下载客户端|登录|注册|"
    r"newsletter|cookie|all rights reserved|©|京ICP|备案)",
    re.IGNORECASE,
)


def _headline_score(anchor: str, a: Node) -> float:
    """
    判断锚文本像不像"一条结果的标题"。
    导航项通常短、无标点、无信息量；标题通常更长或处在 h2/h3 里。
    """
    s = 0.0
    n = len(anchor)
    if n >= 12:
        s += 4.0
    if n >= 20:
        s += 3.0
    if CJK_RE.search(anchor) and n >= 6:
        s += 3.0
    # 标题里常带分隔符/标点
    if re.search(r"[|｜\-–—:：·,，.。!！?？(（]", anchor):
        s += 1.5
    if any(isinstance(x, Node) and x.tag in ("h1", "h2", "h3", "h4") for x in a.ancestors()):
        s += 6.0
    return s


CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def generic_serp(root: Node, engine: Engine, query: str, limit: int,
                 self_domains: Sequence[str] = ()) -> List[Hit]:
    """
    通用搜索结果提取：不依赖具体站点的 class 名。

    思路：正文型外链 + 像标题的锚文本 + 处于"结果容器"内 + 容器里有真实摘要。
    这四个条件是**与**关系 —— 宁可少抓，也绝不把导航栏当结果（那会污染整份报告）。
    """
    skip_domains = {d.lower() for d in self_domains}
    candidates: List[Tuple[float, Hit]] = []
    seen_urls = set()

    for a in root.find_all("a"):
        href = a.attr("href")
        if not is_probably_content_url(href):
            continue
        url = unwrap_redirect(href)
        if not is_probably_content_url(url):
            continue

        if domain_of(url) in skip_domains:
            continue

        anchor = a.get_text()
        if len(anchor) < 6:
            continue
        # 硬性剔除导航/页脚锚文本
        if JUNK_ANCHOR.match(anchor) or JUNK_SUBSTR.search(anchor):
            continue

        key = url.split("#")[0]
        if key in seen_urls:
            continue

        # 找最合适的祖先容器
        container = a
        best_container = None
        for _ in range(4):
            p = container.parent
            if p is None or p.tag in ("body", "html", "#document"):
                break
            container = p
            sig = " ".join([container.attr("id"), container.attr("class")])
            text_len = len(container.get_text())
            if RESULT_HINT.search(sig) and 60 < text_len < 4000:
                best_container = container
                break
            if best_container is None and 80 < text_len < 3000:
                best_container = container
        container = best_container or a.parent or a

        sig = " ".join([container.attr("id"), container.attr("class")])
        # 容器本身是站点框架，直接跳过
        if CHROME_HINT.search(sig) and not RESULT_HINT.search(sig):
            continue

        # 摘要 = 容器文本去掉标题
        snip = container.get_text()
        if anchor and anchor in snip:
            snip = snip.replace(anchor, " ", 1)
        snip = re.sub(r"\s+", " ", snip).strip()
        snip = re.sub(r"^(阅读全文|阅读原文|详情|查看|更多|Read more|Learn more)[:\s]*", "", snip)

        # 摘要太短 => 这不是一条结果，只是链接列表项
        if len(snip) < 25:
            continue
        # 锚文本占了容器绝大部分文本 => 导航/列表，不是结果
        if len(container.get_text()) > 0:
            ratio = len(anchor) / max(1, len(container.get_text()))
            if ratio > 0.75 and len(snip) < 60:
                continue

        score = _headline_score(anchor, a)
        score += min(len(snip), 300) / 40.0
        if RESULT_HINT.search(sig):
            score += 6.0
        # 域名层级太浅（首页）降权
        if not urllib.parse.urlparse(url).path.strip("/"):
            score -= 3.0
        # 明显不是标题的（纯短词）直接淘汰
        if score < 6.0:
            continue

        seen_urls.add(key)
        candidates.append((score, _mk(engine, anchor, url, snip, 0, query)))

    candidates.sort(key=lambda x: x[0], reverse=True)
    out: List[Hit] = []
    for i, (_, hit) in enumerate(candidates[:limit], 1):
        hit.position = i
        out.append(hit)
    return out


# ---------------------------------------------------------------- Bing

def _parse_bing(client: HttpClient, engine: Engine, query: str, limit: int,
                market: str = "cn") -> List[Hit]:
    q = urllib.parse.quote(query)
    if market == "cn":
        url = f"https://cn.bing.com/search?q={q}&ensearch=0&count=20"
    else:
        url = f"https://www.bing.com/search?q={q}&count=20&setlang=en"
    r = client.get(url)
    if not r.ok:
        return []
    root = dom.parse(r.text)

    hits: List[Hit] = []
    items = dom.select(root, "li.b_algo") or dom.select(root, "#b_results > li.b_algo")
    for i, li in enumerate(items[:limit], 1):
        a = dom.select_one(li, "h2 a") or dom.select_one(li, "a[href^=http]")
        if a is None:
            continue
        href = unwrap_redirect(a.attr("href"))
        if not is_probably_content_url(href):
            continue
        title = a.get_text()
        cap = (
            dom.select_one(li, ".b_caption p")
            or dom.select_one(li, ".b_lineclamp2")
            or dom.select_one(li, "p")
        )
        snip = cap.get_text() if cap is not None else ""
        if not snip:
            snip = li.get_text().replace(title, " ", 1)
        hits.append(_mk(engine, title, href, snip, i, query))

    if not hits:  # 改版兜底
        return generic_serp(root, engine, query, limit, ("bing.com", "microsoft.com", "msn.com"))
    return hits


def bing_cn(client: HttpClient, query: str, limit: int) -> List[Hit]:
    return _parse_bing(client, ENGINES["bing_cn"], query, limit, "cn")


def bing_intl(client: HttpClient, query: str, limit: int) -> List[Hit]:
    return _parse_bing(client, ENGINES["bing_intl"], query, limit, "intl")


# ---------------------------------------------------------------- 百度

def baidu(client: HttpClient, query: str, limit: int) -> List[Hit]:
    q = urllib.parse.quote(query)
    r = client.get(f"https://www.baidu.com/s?wd={q}&rn=20&ie=utf-8")
    if not r.ok:
        return []
    root = dom.parse(r.text)
    engine = ENGINES["baidu"]

    hits: List[Hit] = []
    containers = dom.select_all_first(root, [
        "#content_left .result", "#content_left .c-container",
        ".result.c-container", "#content_left > div[class*=result]",
    ])
    seen = set()
    for c in containers:
        a = dom.select_one(c, "h3 a") or dom.select_one(c, "a[href]")
        if a is None:
            continue
        # 百度把真实 URL 放在容器的 mu / data-url 上，href 只是 /link?url= 跳转。
        # 拿不到真实 URL 的话引证就没法回溯，所以这里优先级最高。
        real = (c.attr("mu") or c.attr("data-url") or a.attr("mu")
                or a.attr("data-landurl") or a.attr("href"))
        url = unwrap_redirect(real)
        if not is_probably_content_url(url) and not url.startswith("http"):
            continue
        # 仍然是百度跳转链的，宁可丢掉也不给出无法回溯的引用
        if "baidu.com/link?" in url:
            continue
        title = a.get_text()
        if not title or len(title) < 4:
            continue
        key = url.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        snip_node = dom.select_first(c, [
            "[class*=content-right]", ".c-abstract", "[class*=abstract]",
            "[class*=c-span-last]", ".c-color-text",
        ])
        snip = snip_node.get_text() if snip_node is not None else ""
        if not snip:
            snip = c.get_text().replace(title, " ", 1)
        hits.append(_mk(engine, title, url, snip, len(hits) + 1, query))
        if len(hits) >= limit:
            break

    if not hits:
        return generic_serp(root, engine, query, limit, ("baidu.com", "bdstatic.com"))
    return hits


# ---------------------------------------------------------------- 360 / 其它 SERP

def _simple_serp(engine_id: str, url_tpl: str, skip: Sequence[str],
                 extra_headers: Optional[Dict[str, str]] = None):
    def _fn(client: HttpClient, query: str, limit: int) -> List[Hit]:
        engine = ENGINES[engine_id]
        url = url_tpl.format(query=urllib.parse.quote(query))
        r = client.get(url, headers=extra_headers)
        if not r.ok:
            return []
        root = dom.parse(r.text)
        hits = generic_serp(root, engine, query, limit, skip)
        return hits
    return _fn


# ---------------------------------------------------------------- Wikipedia API

WIKI_API = "https://{lang}.wikipedia.org/w/api.php"


def _wikipedia(lang: str):
    def _fn(client: HttpClient, query: str, limit: int) -> List[Hit]:
        engine = ENGINES[f"wikipedia_{lang}"]
        params = {
            "action": "query", "list": "search", "srsearch": query,
            "format": "json", "srlimit": str(limit), "srprop": "snippet|wordcount|timestamp",
        }
        data = client.get_json(WIKI_API.format(lang=lang) + "?" + urllib.parse.urlencode(params))
        if not data:
            return []
        hits: List[Hit] = []
        for i, item in enumerate(data.get("query", {}).get("search", [])[:limit], 1):
            title = item.get("title", "")
            page_url = f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
            hits.append(_mk(engine, title, page_url,
                            _strip_tags_keep_text(item.get("snippet", "")), i, query,
                            {"wordcount": item.get("wordcount", 0)}))
        return hits
    return _fn


# ---------------------------------------------------------------- arXiv API

ATOM_NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def arxiv(client: HttpClient, query: str, limit: int) -> List[Hit]:
    engine = ENGINES["arxiv"]
    # arXiv 语法：all: 匹配全部字段；引号保留短语
    q = query.strip()
    if ":" not in q.split(" ")[0]:
        q = f"all:{q}"
    url = ("http://export.arxiv.org/api/query?"
           + urllib.parse.urlencode({"search_query": q, "max_results": str(limit),
                                     "sortBy": "relevance"}))
    r = client.get(url)
    if not r.ok:
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return []

    hits: List[Hit] = []
    for i, entry in enumerate(root.findall("a:entry", ATOM_NS)[:limit], 1):
        title = _clean(entry.findtext("a:title", "", ATOM_NS))
        summary = _clean(entry.findtext("a:summary", "", ATOM_NS))
        link = entry.findtext("a:id", "", ATOM_NS)
        for ln in entry.findall("a:link", ATOM_NS):
            if ln.get("rel") == "alternate":
                link = ln.get("href", link)
                break
        authors = [a.findtext("a:name", "", ATOM_NS) for a in entry.findall("a:author", ATOM_NS)]
        published = entry.findtext("a:published", "", ATOM_NS)[:10]
        hits.append(_mk(engine, title, link, summary, i, query, {
            "authors": authors[:6], "published": published,
            "pdf": next((l.get("href") for l in entry.findall("a:link", ATOM_NS)
                         if l.get("title") == "pdf"), ""),
        }))
    return hits


# ---------------------------------------------------------------- Crossref API

def crossref(client: HttpClient, query: str, limit: int) -> List[Hit]:
    engine = ENGINES["crossref"]
    params = {"query": query, "rows": str(limit),
              "select": "title,DOI,URL,abstract,author,issued,container-title,type"}
    data = client.get_json("https://api.crossref.org/works?" + urllib.parse.urlencode(params),
                           headers={"User-Agent": "deep-search-skill/1.0 (mailto:example@example.com)"})
    if not data:
        return []
    hits: List[Hit] = []
    for i, it in enumerate(data.get("message", {}).get("items", [])[:limit], 1):
        title = (it.get("title") or [""])[0]
        if not title:
            continue
        doi = it.get("DOI", "")
        url = it.get("URL") or (f"https://doi.org/{doi}" if doi else "")
        year = ""
        issued = it.get("issued", {}).get("date-parts") or [[]]
        if issued and issued[0]:
            year = str(issued[0][0])
        authors = []
        for a in (it.get("author") or [])[:5]:
            nm = " ".join(x for x in [a.get("given", ""), a.get("family", "")] if x)
            if nm:
                authors.append(nm)
        venue = (it.get("container-title") or [""])[0]
        abstract = _clean(it.get("abstract", ""))
        hits.append(_mk(engine, title, url, abstract or f"{venue} {year}".strip(), i, query,
                        {"doi": doi, "year": year, "authors": authors, "venue": venue,
                         "type": it.get("type", "")}))
    return hits


# ---------------------------------------------------------------- OpenAlex API

def _openalex_abstract(inv: Optional[Dict[str, List[int]]]) -> str:
    """OpenAlex 的摘要是倒排索引，需要还原成文本。"""
    if not inv:
        return ""
    pos: Dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def openalex(client: HttpClient, query: str, limit: int) -> List[Hit]:
    engine = ENGINES["openalex"]
    params = {"search": query, "per-page": str(limit),
              "select": "id,doi,title,publication_year,authorships,primary_location,abstract_inverted_index,cited_by_count"}
    data = client.get_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params),
                           headers={"User-Agent": "deep-search-skill/1.0 (mailto:example@example.com)"})
    if not data:
        return []
    hits: List[Hit] = []
    for i, it in enumerate(data.get("results", [])[:limit], 1):
        title = it.get("title") or ""
        if not title:
            continue
        url = it.get("doi") or it.get("id") or ""
        loc = it.get("primary_location") or {}
        src = (loc.get("source") or {}).get("display_name", "")
        authors = [a.get("author", {}).get("display_name", "")
                   for a in (it.get("authorships") or [])[:5]]
        abstract = _openalex_abstract(it.get("abstract_inverted_index"))
        hits.append(_mk(engine, title, url, abstract or src, i, query, {
            "year": it.get("publication_year"), "venue": src,
            "authors": [a for a in authors if a],
            "cited_by": it.get("cited_by_count", 0),
        }))
    return hits


# ---------------------------------------------------------------- GitHub API

def github(client: HttpClient, query: str, limit: int) -> List[Hit]:
    engine = ENGINES["github"]
    params = {"q": query, "per_page": str(min(limit, 20)), "sort": "best-match"}
    data = client.get_json("https://api.github.com/search/repositories?" + urllib.parse.urlencode(params),
                           headers={"Accept": "application/vnd.github+json"})
    if not data or "items" not in data:
        return []
    hits: List[Hit] = []
    for i, it in enumerate(data["items"][:limit], 1):
        desc = it.get("description") or ""
        stars = it.get("stargazers_count", 0)
        lang = it.get("language") or ""
        snip = f"{desc} ⭐{stars}" + (f" · {lang}" if lang else "")
        hits.append(_mk(engine, it.get("full_name", ""), it.get("html_url", ""), snip, i, query,
                        {"stars": stars, "language": lang,
                         "updated": (it.get("updated_at") or "")[:10]}))
    return hits


# ---------------------------------------------------------------- StackExchange API

def stackexchange(client: HttpClient, query: str, limit: int) -> List[Hit]:
    engine = ENGINES["stackexchange"]
    params = {"order": "desc", "sort": "relevance", "q": query,
              "site": "stackoverflow", "pagesize": str(min(limit, 20)),
              "filter": "withbody"}
    data = client.get_json("https://api.stackexchange.com/2.3/search/advanced?"
                           + urllib.parse.urlencode(params))
    if not data or "items" not in data:
        return []
    hits: List[Hit] = []
    for i, it in enumerate(data["items"][:limit], 1):
        title = _clean(it.get("title", ""))
        body = _clean(it.get("body", ""))[:400]
        hits.append(_mk(engine, title, it.get("link", ""),
                        body or " ".join(it.get("tags", [])[:6]), i, query,
                        {"score": it.get("score", 0), "answered": it.get("is_answered", False),
                         "tags": it.get("tags", [])[:6]}))
    return hits


# ---------------------------------------------------------------- HackerNews (Algolia)

def hackernews(client: HttpClient, query: str, limit: int) -> List[Hit]:
    engine = ENGINES["hackernews"]
    params = {"query": query, "hitsPerPage": str(min(limit, 20)), "tags": "story"}
    data = client.get_json("https://hn.algolia.com/api/v1/search?" + urllib.parse.urlencode(params))
    if not data or "hits" not in data:
        return []
    hits: List[Hit] = []
    for i, it in enumerate(data["hits"][:limit], 1):
        title = it.get("title") or it.get("story_title") or ""
        if not title:
            continue
        url = it.get("url") or f"https://news.ycombinator.com/item?id={it.get('objectID')}"
        snip = f"{it.get('points', 0)} points · {it.get('num_comments', 0)} comments"
        hits.append(_mk(engine, title, url, snip, i, query,
                        {"points": it.get("points", 0),
                         "comments": it.get("num_comments", 0)}))
    return hits


# ---------------------------------------------------------------- 注册表

ENGINES: Dict[str, Engine] = {}


def _reg(e: Engine) -> Engine:
    ENGINES[e.id] = e
    return e


_reg(Engine("bing_cn", "必应中文", "general", 0.92, bing_cn, tier=1))
_reg(Engine("bing_intl", "必应国际", "general", 0.95, bing_intl, tier=1))
_reg(Engine("baidu", "百度", "general", 0.88, baidu, tier=1))
_reg(Engine("so360", "360搜索", "general", 0.70,
            _simple_serp("so360", "https://m.so.com/s?q={query}", ("so.com", "360.cn")),
            tier=2, timeout=8.0))
_reg(Engine("sogou", "搜狗", "general", 0.68,
            _simple_serp("sogou", "https://www.sogou.com/web?query={query}", ("sogou.com",)),
            tier=2, timeout=8.0))
_reg(Engine("brave", "Brave", "general", 0.80,
            _simple_serp("brave", "https://search.brave.com/search?q={query}", ("brave.com",)),
            tier=2, timeout=8.0))
_reg(Engine("mojeek", "Mojeek", "general", 0.65,
            _simple_serp("mojeek", "https://www.mojeek.com/search?q={query}", ("mojeek.com",)),
            tier=2, timeout=8.0))
_reg(Engine("ecosia", "Ecosia", "general", 0.65,
            _simple_serp("ecosia", "https://www.ecosia.org/search?q={query}", ("ecosia.org",)),
            tier=2, timeout=8.0))

_reg(Engine("wikipedia_zh", "维基百科(中)", "knowledge", 0.90, _wikipedia("zh"),
            tier=1, timeout=9.0))
_reg(Engine("wikipedia_en", "Wikipedia(EN)", "knowledge", 0.90, _wikipedia("en"),
            tier=1, timeout=9.0, english_only=True))
_reg(Engine("arxiv", "arXiv", "academic", 0.95, arxiv, tier=1, english_only=True))
_reg(Engine("crossref", "Crossref", "academic", 0.93, crossref, tier=1, english_only=True))
_reg(Engine("openalex", "OpenAlex", "academic", 0.92, openalex, tier=1, english_only=True))
_reg(Engine("github", "GitHub", "tech", 0.92, github, tier=1, english_only=True))
_reg(Engine("stackexchange", "Stack Overflow", "tech", 0.90, stackexchange,
            tier=1, english_only=True))
_reg(Engine("hackernews", "Hacker News", "tech", 0.78, hackernews,
            tier=1, english_only=True))


# ---------------------------------------------------------------- 意图路由

INTENTS: Dict[str, Dict] = {
    "general": {
        "engines": ["bing_cn", "bing_intl", "baidu", "so360"],
        "desc": "通用中文/国际网页",
    },
    "tech": {
        "engines": ["github", "stackexchange", "hackernews", "bing_intl", "bing_cn"],
        "desc": "代码、库、报错、工程实践",
    },
    "academic": {
        "engines": ["arxiv", "crossref", "openalex", "wikipedia_en", "bing_intl"],
        "desc": "论文、综述、学术证据",
    },
    "knowledge": {
        "engines": ["wikipedia_zh", "wikipedia_en", "bing_cn", "bing_intl"],
        "desc": "概念、定义、百科式事实",
    },
    "finance": {
        "engines": ["bing_cn", "baidu", "bing_intl"],
        "desc": "财经、公司、行情（走 site: 与新闻检索）",
    },
    "social": {
        "engines": ["bing_cn", "baidu", "bing_intl"],
        "desc": "公众号、论坛、社区观点",
    },
    "news": {
        "engines": ["bing_cn", "baidu", "bing_intl"],
        "desc": "新闻与时事",
    },
    "privacy": {
        "engines": ["brave", "mojeek", "ecosia", "bing_intl"],
        "desc": "隐私友好引擎",
    },
    "advanced": {
        "engines": ["bing_intl", "bing_cn", "baidu"],
        "desc": "含 site:/filetype:/intitle: 等高级语法",
    },
}

INTENT_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("academic", ["论文", "文献", "综述", "学术", "期刊", "被引", "doi", "arxiv",
                  "paper", "survey", "research", "study", "journal", "citation"]),
    ("tech", ["github", "代码", "编程", "报错", "编译", "函数", "接口", "框架", "部署",
              "python", "javascript", "java", "rust", "golang", "docker", "kubernetes",
              "error", "bug", "api", "library", "install", "stackoverflow"]),
    ("finance", ["股票", "基金", "财报", "市值", "行情", "估值", "营收", "净利润", "a股",
                 "港股", "美股", "债券", "投资", "stock", "earnings", "valuation", "revenue"]),
    ("news", ["新闻", "最新", "进展", "发布会", "宣布", "2024", "2025", "2026",
              "news", "latest", "announced", "release"]),
    ("social", ["公众号", "微信", "知乎", "微博", "小红书", "论坛", "网友", "reddit", "wechat"]),
    ("knowledge", ["是什么", "什么是", "定义", "含义", "概念", "原理", "介绍",
                   "what is", "definition", "meaning", "overview"]),
]

ADVANCED_SYNTAX = re.compile(
    r"(site:|filetype:|intitle:|inurl:|inanchor:|related:|\"[^\"]{2,}\"|(^|\s)-\S+)",
    re.IGNORECASE,
)


def detect_intent(query: str, hint: Optional[str] = None) -> str:
    if hint and hint in INTENTS:
        return hint
    if ADVANCED_SYNTAX.search(query):
        return "advanced"
    low = query.lower()
    for intent, kws in INTENT_KEYWORDS:
        if any(k in low for k in kws):
            return intent
    return "general"


def route(query: str, intent: Optional[str] = None,
          include: Sequence[str] = (), exclude: Sequence[str] = ()) -> List[str]:
    """决定这轮用哪些引擎。include 强制加入，exclude 强制剔除。"""
    it = detect_intent(query, intent)
    ids = list(INTENTS.get(it, INTENTS["general"])["engines"])
    for e in include:
        if e in ENGINES and e not in ids:
            ids.append(e)
    ids = [e for e in ids if e not in set(exclude) and e in ENGINES]
    return ids or ["bing_cn"]


def engine_names(ids: Sequence[str]) -> List[str]:
    return [ENGINES[i].name for i in ids if i in ENGINES]


def all_engine_ids() -> List[str]:
    return sorted(ENGINES)


def search_engine(client: HttpClient, engine_id: str, query: str,
                  limit: int = 10) -> List[Hit]:
    """调用单个引擎，永不抛异常。"""
    engine = ENGINES.get(engine_id)
    if engine is None:
        return []

    q = query
    if engine.english_only:
        q = strip_cjk(query)
        # 纯中文查询交给英文库毫无意义，直接跳过，省一次请求
        if len(q) < 2:
            return []

    try:
        client.set_thread_timeout(engine.timeout)
        # 检索请求**不**套用 robots.txt：向搜索引擎发一条查询等价于用户在搜索框里
        # 输入（Bing/Baidu 的 robots 会禁止 /search，Wikipedia 会禁止 api.php，
        # 套用则整个技能无法工作）。真正的"爬取"发生在抓取结果页正文时，
        # 那一步仍然严格遵守 robots —— 见 pipeline._fetch_one。
        client.set_thread_robots(False)
        hits = engine.fn(client, q, limit) or []
        return [h for h in hits if h.title and h.url]
    except Exception:
        return []
    finally:
        client.set_thread_timeout(None)
        client.set_thread_robots(None)


def apply_engine_weights(weights: Optional[Dict[str, float]]) -> None:
    """
    用配置覆盖引擎权重（只覆盖已知引擎，未知键静默忽略）。
    这样按环境微调排序偏好不必改代码。
    """
    if not weights:
        return
    for eid, w in weights.items():
        e = ENGINES.get(eid)
        if e is None:
            continue
        try:
            e.weight = float(w)
        except (TypeError, ValueError):
            continue
