# -*- coding: utf-8 -*-
"""
ds.rank —— 去重、相关性排序、抽取式摘要

深度搜索的产出不是"一堆链接"，而是**可核验的证据集**。所以这里做三件事：

1. **去重**：URL 规范化 + SimHash 正文指纹 + 标题相似度。
   同一篇内容被 5 个引擎返回，只应算 1 条证据（但记录它被 5 个引擎印证）。
2. **排序**：相关性（BM25）+ 来源权威度 + 时效性 + 内容厚度。
   只按相关性排会把内容农场顶上来，权威度和时效是必要的矫正项。
3. **摘要**：跨来源做抽取式摘要（MMR 去冗余），每句都带出处编号。
   —— 摘要是**有引用的**，这是它和"生成一段看起来对的话"的区别。
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from . import text as T

# ---------------------------------------------------------------- 数据模型


@dataclass
class Source:
    """一条经过抓取与正文抽取的证据。"""

    url: str
    title: str = ""
    snippet: str = ""
    content: str = ""
    paragraphs: List[str] = field(default_factory=list)
    site: str = ""
    published: str = ""
    author: str = ""
    engines: List[str] = field(default_factory=list)
    queries: List[str] = field(default_factory=list)
    facets: List[str] = field(default_factory=list)
    relevance: float = 0.0
    authority: float = 0.0
    freshness: float = 0.0
    depth: float = 0.0
    score: float = 0.0
    fingerprint: int = 0
    status: str = "pending"   # ok / fetch_failed / empty / skipped
    error: str = ""
    quotes: List[str] = field(default_factory=list)
    idx: int = 0              # 引用编号，排序后分配

    @property
    def char_count(self) -> int:
        return len(self.content or "")

    @property
    def domain(self) -> str:
        try:
            h = (urlparse(self.url).hostname or "").lower()
        except Exception:
            return ""
        for pre in ("www.", "m.", "mobile."):
            if h.startswith(pre):
                h = h[len(pre):]
        return h

    @property
    def has_content(self) -> bool:
        return self.status == "ok" and self.char_count >= 200

    def best_text(self) -> str:
        return self.content or self.snippet or ""

    def to_dict(self, with_content: bool = False) -> Dict:
        d = {
            "idx": self.idx,
            "title": self.title,
            "url": self.url,
            "site": self.site or self.domain,
            "published": self.published,
            "author": self.author,
            "engines": sorted(set(self.engines)),
            "queries": self.queries[:4],
            "facets": sorted(set(self.facets)),
            "status": self.status,
            "char_count": self.char_count,
            "score": round(self.score, 3),
            "relevance": round(self.relevance, 3),
            "authority": round(self.authority, 3),
            "freshness": round(self.freshness, 3),
            "snippet": self.snippet[:300],
            "quotes": self.quotes[:5],
        }
        if with_content:
            d["content"] = self.content
            d["paragraphs"] = self.paragraphs[:60]
        return d


# ---------------------------------------------------------------- 权威度

# 域名后缀 → 权威度（0-1）。不是"真理排名"，只是把内容农场压下去。
AUTHORITY_SUFFIX: List[Tuple[str, float]] = [
    (".gov", 0.95), (".gov.cn", 0.95), (".edu", 0.93), (".edu.cn", 0.93),
    (".ac.uk", 0.93), (".org", 0.72), (".int", 0.9), (".mil", 0.9),
]

AUTHORITY_DOMAIN: Dict[str, float] = {
    # 学术
    "arxiv.org": 0.97, "nature.com": 0.97, "science.org": 0.97, "sciencedirect.com": 0.93,
    "springer.com": 0.92, "link.springer.com": 0.92, "ieee.org": 0.93, "acm.org": 0.93,
    "pubmed.ncbi.nlm.nih.gov": 0.95, "nih.gov": 0.95, "doi.org": 0.9,
    "openalex.org": 0.9, "crossref.org": 0.9, "semanticscholar.org": 0.9,
    "cell.com": 0.94, "pnas.org": 0.94, "jstor.org": 0.9,
    # 百科/知识
    "wikipedia.org": 0.88, "zh.wikipedia.org": 0.88, "en.wikipedia.org": 0.88,
    "britannica.com": 0.88,
    # 技术
    "github.com": 0.88, "stackoverflow.com": 0.88, "stackexchange.com": 0.85,
    "python.org": 0.92, "docs.python.org": 0.93, "developer.mozilla.org": 0.93,
    "kernel.org": 0.92, "rust-lang.org": 0.92, "go.dev": 0.92, "nodejs.org": 0.9,
    "react.dev": 0.9, "vuejs.org": 0.9, "kubernetes.io": 0.9, "docker.com": 0.85,
    "readthedocs.io": 0.8, "npmjs.com": 0.8, "pypi.org": 0.82,
    "news.ycombinator.com": 0.75, "arxiv.com": 0.9,
    # 媒体（中）
    "reuters.com": 0.85, "apnews.com": 0.85, "bbc.com": 0.85, "bbc.co.uk": 0.85,
    "nytimes.com": 0.82, "wsj.com": 0.82, "ft.com": 0.84, "bloomberg.com": 0.84,
    "economist.com": 0.84, "theguardian.com": 0.8, "wired.com": 0.78,
    "xinhuanet.com": 0.82, "people.com.cn": 0.82, "caixin.com": 0.85,
    "yicai.com": 0.8, "cls.cn": 0.8, "stcn.com": 0.78, "21jingji.com": 0.75,
    "thepaper.cn": 0.78, "jiemian.com": 0.78, "36kr.com": 0.72,
    "mp.weixin.qq.com": 0.7, "zhihu.com": 0.7, "juejin.cn": 0.72, "csdn.net": 0.6,
    "cnblogs.com": 0.7, "segmentfault.com": 0.72, "infoq.cn": 0.75,
    "sspai.com": 0.75, "medium.com": 0.7, "substack.com": 0.7, "dev.to": 0.72,
    "reddit.com": 0.6, "twitter.com": 0.55, "x.com": 0.55, "facebook.com": 0.5,
    "baike.baidu.com": 0.6, "baidu.com": 0.5, "so.com": 0.5,
}

# 内容农场/聚合站，显著降权
LOW_QUALITY = re.compile(
    r"(content|article)\d*\.(?:com|net)|"
    r"(zhannei|so\.|sobooks|zhihu\.com/search)|"
    r"(csdn\.net/download)|(docin|doc88|book118|renrendoc)",
    re.IGNORECASE,
)


def authority_of(domain: str) -> float:
    if not domain:
        return 0.4
    d = domain.lower()
    if d in AUTHORITY_DOMAIN:
        return AUTHORITY_DOMAIN[d]
    # 尝试父域匹配
    parts = d.split(".")
    for i in range(1, len(parts) - 1):
        parent = ".".join(parts[i:])
        if parent in AUTHORITY_DOMAIN:
            return AUTHORITY_DOMAIN[parent] * 0.95
    for suffix, val in AUTHORITY_SUFFIX:
        if d.endswith(suffix):
            return val
    if LOW_QUALITY.search(d):
        return 0.25
    # 未知域名给中性偏低分
    return 0.55


# ---------------------------------------------------------------- 时效性

_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _parse_year(s: str) -> Optional[int]:
    if not s:
        return None
    m = _YEAR_RE.search(s)
    if not m:
        return None
    try:
        y = int(m.group(0))
        return y if 1990 <= y <= datetime.now().year + 1 else None
    except ValueError:
        return None


def freshness_of(published: str, content: str = "", now_year: Optional[int] = None) -> float:
    """
    时效性 0-1。有明确日期的按日期算；没有的从正文里找最晚年份。
    找不到年份给中性 0.5 —— 不惩罚也不奖励。
    """
    now = now_year or datetime.now().year
    year = _parse_year(published)
    if year is None and content:
        years = [int(y) for y in _YEAR_RE.findall(content[:4000])]
        years = [y for y in years if 1990 <= y <= now + 1]
        if years:
            year = max(years)
    if year is None:
        return 0.5
    age = max(0, now - year)
    # 1 年内 = 1.0，每老一年衰减
    return max(0.15, 1.0 - age * 0.14)


# ---------------------------------------------------------------- 相关性 (BM25)

def _bm25(query_terms: Sequence[str], docs: Sequence[Sequence[str]],
          k1: float = 1.5, b: float = 0.75) -> List[float]:
    """标准 BM25，语料 = 本次检索到的所有来源。"""
    n = len(docs)
    if n == 0:
        return []
    avgdl = sum(len(d) for d in docs) / n or 1.0

    df: Counter = Counter()
    for d in docs:
        for t in set(d):
            df[t] += 1

    scores = [0.0] * n
    for i, d in enumerate(docs):
        dl = len(d) or 1
        tf = Counter(d)
        for t in query_terms:
            if t not in tf:
                continue
            f = tf[t]
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            scores[i] += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
    return scores


def _normalize(vals: Sequence[float]) -> List[float]:
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return [0.5] * len(vals)
    return [(v - lo) / (hi - lo) for v in vals]


def score_sources(sources: List[Source], query: str,
                  weights: Optional[Dict[str, float]] = None) -> List[Source]:
    """
    给每个来源打综合分并原地排序（分数写入 source.score）。
    综合分 = 相关性 + 权威度 + 时效性 + 厚度 + 多引擎印证
    """
    w = {"relevance": 1.0, "authority": 0.45, "freshness": 0.20,
         "depth": 0.25, "corroboration": 0.20}
    if weights:
        w.update(weights)

    q_terms = T.content_tokens(query)
    if not q_terms:
        q_terms = T.tokenize(query)[:20]

    # 相关性：标题命中权重更高，所以标题单独算一遍再合并
    body_docs = [T.tokenize((s.title + " ") * 3 + s.best_text()) for s in sources]
    rel = _bm25(q_terms, body_docs)

    # 完全没命中的降到很低
    for i, s in enumerate(sources):
        ts = T.token_set(s.title + " " + s.best_text()[:4000])
        hits = sum(1 for t in set(q_terms) if t in ts)
        if hits == 0:
            rel[i] *= 0.15

    rel_n = _normalize(rel)
    auth = [authority_of(s.domain) for s in sources]
    fresh = [freshness_of(s.published, s.content) for s in sources]
    depth = _normalize([min(s.char_count, 8000) for s in sources])
    corrob = _normalize([len(set(s.engines)) for s in sources])

    for i, s in enumerate(sources):
        s.relevance = rel_n[i] if i < len(rel_n) else 0.0
        s.authority = auth[i]
        s.freshness = fresh[i]
        s.depth = depth[i] if i < len(depth) else 0.0
        s.score = (
            w["relevance"] * s.relevance
            + w["authority"] * s.authority
            + w["freshness"] * s.freshness
            + w["depth"] * s.depth
            + w["corroboration"] * (corrob[i] if i < len(corrob) else 0.0)
        )

    sources.sort(key=lambda s: s.score, reverse=True)
    return sources


# ---------------------------------------------------------------- 去重

def _title_key(title: str) -> str:
    return re.sub(r"[^\u4e00-\u9fff a-zA-Z0-9]", "", (title or "").lower())


def dedupe(sources: List[Source],
           title_threshold: float = 0.86,
           fingerprint_distance: int = 6) -> Tuple[List[Source], List[Tuple[str, str]]]:
    """
    三层去重，返回 (保留的来源, [(被合并的 url, 保留的 url)])。

    合并时保留内容更长的那条，但把 engines/queries/facets 合并过去 ——
    多个引擎都指向同一篇，是"可信"的信号，不该丢。
    """
    kept: List[Source] = []
    merged: List[Tuple[str, str]] = []
    url_seen: Dict[str, Source] = {}
    fp_index: List[Tuple[int, Source]] = []
    title_index: List[Tuple[str, Source]] = []

    # 内容厚的优先成为"主条目"
    ordered = sorted(sources, key=lambda s: (s.char_count, len(s.engines)), reverse=True)

    for s in ordered:
        key = T.normalize_url(s.url) or s.url
        dup_of: Optional[Source] = None

        if key in url_seen:
            dup_of = url_seen[key]
        else:
            # 正文指纹近似（内容够长才计算；相同内容无论长短都该合并）
            if s.char_count >= 200:
                fp = T.simhash(T.content_tokens(s.content[:4000]))
                s.fingerprint = fp
                for prev_fp, prev in fp_index:
                    if T.near_duplicate(fp, prev_fp, fingerprint_distance):
                        # 指纹相近还要标题也别差太远，避免误杀同主题不同文
                        if T.similarity(_title_key(s.title), _title_key(prev.title)) >= 0.5:
                            dup_of = prev
                            break
            # 标题高度相似 —— 但只在**确实抓到正文**时才敢合并。
            # 否则（快速搜索/抓取失败时）会把"标题相近其实是不同页面"的
            # 链接直接丢掉，用户就少了一个入口。
            if dup_of is None and s.char_count >= 400:
                tk = _title_key(s.title)
                if len(tk) >= 8:
                    for prev_tk, prev in title_index:
                        if T.similarity(tk, prev_tk) >= title_threshold:
                            dup_of = prev
                            break

        if dup_of is not None:
            dup_of.engines = list(set(dup_of.engines) | set(s.engines))
            dup_of.queries = list(dict.fromkeys(dup_of.queries + s.queries))
            dup_of.facets = list(set(dup_of.facets) | set(s.facets))
            if not dup_of.snippet and s.snippet:
                dup_of.snippet = s.snippet
            merged.append((s.url, dup_of.url))
            continue

        url_seen[key] = s
        if s.fingerprint:
            fp_index.append((s.fingerprint, s))
        tk = _title_key(s.title)
        if len(tk) >= 8:
            title_index.append((tk, s))
        kept.append(s)

    return kept, merged


# ---------------------------------------------------------------- 引证片段

_OPPOSITION = re.compile(
    r"(但是|然而|不过|相反|并非|并不是|恰恰相反|实际上不|质疑|反驳|错误|失败|"
    r"however|but\s|contrary|not\s+true|failed|criticism|flawed|misleading)",
    re.IGNORECASE,
)


def select_quotes(source: Source, query_terms: Sequence[str], n: int = 3) -> List[str]:
    """从正文里挑与查询最相关的句子作为引证。"""
    text = source.best_text()
    if not text:
        return []
    sentences = T.split_sentences(text)
    if not sentences:
        return [T.re.sub(r"\s+", " ", text[:220])]

    qset = set(query_terms)
    scored: List[Tuple[float, str]] = []
    for i, sent in enumerate(sentences[:200]):
        if len(sent) < 20:
            continue
        toks = set(T.content_tokens(sent))
        overlap = len(toks & qset)
        score = overlap * 2.0
        # 带数字/年份的句子信息量大
        if re.search(r"\d", sent):
            score += 0.8
        # 太长的句子不适合引用
        if len(sent) > 300:
            score -= 1.0
        # 靠前的句子通常是核心论述
        score += max(0.0, 1.2 - i * 0.02)
        if score <= 0:
            continue
        scored.append((score, sent))

    scored.sort(key=lambda x: x[0], reverse=True)
    out: List[str] = []
    for _, sent in scored:
        if any(T.similarity(sent, o) > 0.8 for o in out):
            continue
        out.append(sent)
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------- 抽取式摘要

def extractive_summary(sources: Sequence[Source], query: str,
                       max_sentences: int = 12) -> List[Dict]:
    """
    跨来源抽取式摘要：选出最有信息量且互不重复的句子，每句带来源编号。

    用 MMR（最大边际相关）平衡"相关"与"新颖"—— 否则会选出一堆同义句。
    """
    q_terms = set(T.content_tokens(query))
    cands: List[Tuple[float, str, Source]] = []

    for s in sources:
        if not s.has_content:
            continue
        sentences = T.split_sentences(s.content)[:160]
        for i, sent in enumerate(sentences):
            if len(sent) < 25 or len(sent) > 400:
                continue
            toks = set(T.content_tokens(sent))
            if not toks:
                continue
            overlap = len(toks & q_terms)
            base = overlap * 1.6
            if re.search(r"\d", sent):
                base += 0.7
            base += max(0.0, 1.0 - i * 0.015)
            # 来源本身的分量也计入
            base += s.score * 0.8
            if base <= 0:
                continue
            cands.append((base, sent, s))

    if not cands:
        return []

    cands.sort(key=lambda x: x[0], reverse=True)
    # 只从最靠前的候选里挑，避免长尾噪声
    pool = cands[: max(60, max_sentences * 8)]

    selected: List[Dict] = []
    chosen_tokens: List[Set[str]] = []

    while len(selected) < max_sentences and pool:
        best_idx, best_val = -1, -1e9
        for i, (base, sent, src) in enumerate(pool):
            toks = set(T.content_tokens(sent))
            redundancy = 0.0
            for ct in chosen_tokens:
                redundancy = max(redundancy, T.jaccard(toks, ct))
            mmr = base - 2.2 * redundancy
            if mmr > best_val:
                best_val, best_idx = mmr, i
        if best_idx < 0 or best_val <= 0:
            break
        base, sent, src = pool.pop(best_idx)
        toks = set(T.content_tokens(sent))
        # 硬去重：与已选句子高度重合的直接丢弃。
        # 不能只靠 MMR 惩罚 —— 当某个重复句的 base 很高时，扣掉惩罚仍然能胜出，
        # 结果就是同一句话（常来自网页里的摘要块）在报告里出现两次。
        if any(T.jaccard(toks, ct) >= 0.85 for ct in chosen_tokens):
            continue
        chosen_tokens.append(toks)
        selected.append({
            "text": sent,
            "source_idx": src.idx,
            "source_title": src.title,
            "source_url": src.url,
            "site": src.site or src.domain,
            "score": round(base, 3),
            "opposition": bool(_OPPOSITION.search(sent)),
        })

    return selected


# ---------------------------------------------------------------- 分歧检测

def find_divergences(summary: Sequence[Dict], min_shared: int = 3,
                     max_out: int = 3) -> List[Dict]:
    """
    在摘要句里找"可能的分歧"：围绕**同一组有辨识度的主题词**，出现了对立表述。

    关键是"有辨识度"：像 "固态电池产业化" 这种话题词几乎每句都有，
    它们构成的是"共同话题"而不是"分歧点"。所以先算语料级文档频率，
    把近乎每句都出现的词剔除，只用剩下的**特征词**判断是否在谈同一件事。
    否则任意两句都会被判成分歧 —— 那比没有这一节更糟。
    """
    if len(summary) < 2:
        return []

    token_sets = [set(T.content_tokens(s["text"])) for s in summary]
    df: Counter = Counter()
    for ts in token_sets:
        df.update(ts)
    n = len(token_sets)
    # 出现得太普遍的词 = 话题背景，不承载分歧
    common = {t for t, c in df.items() if c >= max(3, n * 0.5)}

    out: List[Dict] = []
    for i, a in enumerate(summary):
        for j in range(i + 1, len(summary)):
            b = summary[j]
            if a["source_idx"] == b["source_idx"]:
                continue
            # 只比较"一方有对立标记、另一方没有"的句子对
            if a["opposition"] == b["opposition"]:
                continue
            shared = (token_sets[i] & token_sets[j]) - common
            if len(shared) < min_shared:
                continue
            out.append({
                "shared_terms": sorted(shared)[:6],
                "a": {"text": a["text"][:240], "source_idx": a["source_idx"]},
                "b": {"text": b["text"][:240], "source_idx": b["source_idx"]},
            })
            if len(out) >= max_out:
                return out
    return out


# ---------------------------------------------------------------- 覆盖度统计

def coverage_report(sources: Sequence[Source], query: str) -> Dict:
    """统计证据的结构：多少条、多少独立域名、多少条有正文、面覆盖情况。"""
    usable = [s for s in sources if s.has_content]
    domains = {s.domain for s in usable if s.domain}
    years = [y for y in (_parse_year(s.published) for s in usable) if y]
    return {
        "sources_total": len(sources),
        "sources_with_content": len(usable),
        "distinct_domains": len(domains),
        "avg_chars": int(sum(s.char_count for s in usable) / len(usable)) if usable else 0,
        "total_chars": sum(s.char_count for s in usable),
        "year_min": min(years) if years else None,
        "year_max": max(years) if years else None,
        "top_domains": Counter(s.domain for s in usable).most_common(8),
    }
