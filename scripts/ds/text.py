# -*- coding: utf-8 -*-
"""
ds.text —— 文本工具：分词、相似度、指纹、URL 规范化

中文没有空格，而标准库没有分词器。这里用 **CJK 字符二元组（bigram）** 作为
检索单元 —— 不需要词典，召回足够好，且对未登录词（新术语、专有名词）天然友好。
英文按单词切。

这些函数被 planner（找缺口词）和 rank（去重、排序、摘要）共用，
所以单独成模块，避免两个模块互相依赖。
"""

from __future__ import annotations

import hashlib
import math
import re
import urllib.parse
from collections import Counter
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Sequence, Set, Tuple

CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
LATIN_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9_+#.\-]*")
SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|(?<=[.!?])\s+(?=[A-Z])|\n+")

# 中英文停用词（高频功能词，不携带主题信息）
STOPWORDS: Set[str] = set("""
的 了 和 是 在 我 有 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好
自己 这 那 他 她 它 们 这个 那个 什么 怎么 如何 为什么 哪些 哪个 可以 因为 所以
但是 而且 或者 如果 虽然 然后 以及 并且 对于 关于 通过 根据 由于 以便 以便于
进行 已经 正在 将会 能够 需要 应该 可能 必须 提供 包括 例如 比如 等等 一些 这些
我们 你们 他们 它们 其中 之后 之前 同时 另外 此外 因此 然而 不过 只是 就是 还是
把 被 让 使 从 向 对 与 及 或 而 则 于 以 为 之 其 此 该 各 每 某 本 等 者 地 得
the a an and or but if then else of to in on at by for with from as is are was were
be been being this that these those it its they them their we our you your he she his
her i me my mine not no do does did done have has had can could will would shall should
may might must about into over under more most other some such only own same so than
too very just also there here what which who whom when where why how all any both each
""".split())

URL_TRACK_PARAMS = re.compile(
    r"^(utm_|spm|from|share|ref|src|source|fbclid|gclid|msclkid|_ga|"
    r"scm|vd_source|spm_id_from|share_source|share_medium|share_plat|"
    r"share_session_id|share_tag|timestamp|unique_k|buvid|wfr|for|"
    r"from_source|from_spmid|seid|s_from|request_id|biz_|mid|idx|sn|chksm)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- 分词

def tokenize(text: str, min_cjk: int = 2) -> List[str]:
    """
    中英混合分词。
    - 中文：连续汉字串切成字符 bigram（长度不足则保留整串）
    - 英文/数字：按词切，转小写
    返回保留重复的 token 列表（词频有意义）。
    """
    if not text:
        return []
    tokens: List[str] = []

    for run in CJK_RUN.findall(text):
        if len(run) <= min_cjk:
            tokens.append(run)
        else:
            for i in range(len(run) - 1):
                tokens.append(run[i:i + 2])

    for w in LATIN_WORD.findall(text):
        w = w.lower().strip(".-_")
        if len(w) >= 2 and w not in STOPWORDS:
            tokens.append(w)

    return tokens


def token_set(text: str) -> Set[str]:
    return set(tokenize(text))


def content_tokens(text: str) -> List[str]:
    """去掉停用词与纯数字后的 token。"""
    out = []
    for t in tokenize(text):
        if t in STOPWORDS or t.isdigit():
            continue
        out.append(t)
    return out


def split_sentences(text: str) -> List[str]:
    if not text:
        return []
    parts = [re.sub(r"\s+", " ", p).strip() for p in SENT_SPLIT.split(text)]
    return [p for p in parts if len(p) >= 8]


# ---------------------------------------------------------------- 相似度

def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def similarity(a: str, b: str) -> float:
    """综合相似度：字符级 + token 级，取较大者。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    char_sim = SequenceMatcher(None, a, b).ratio()
    tok_sim = jaccard(token_set(a), token_set(b))
    return max(char_sim, tok_sim)


def containment(a: Set[str], b: Set[str]) -> float:
    """a 被 b 覆盖的比例，用于"标题包含"判断。"""
    if not a:
        return 0.0
    return len(a & b) / len(a)


# ---------------------------------------------------------------- SimHash 指纹

def simhash(tokens: Sequence[str], bits: int = 64) -> int:
    """SimHash：内容近似重复检测，比逐对比较快得多。"""
    if not tokens:
        return 0
    counts = Counter(tokens)
    vec = [0] * bits
    for tok, w in counts.items():
        h = int.from_bytes(hashlib.md5(tok.encode("utf-8")).digest()[:8], "big")
        for i in range(bits):
            vec[i] += w if (h >> i) & 1 else -w
    out = 0
    for i in range(bits):
        if vec[i] > 0:
            out |= (1 << i)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def near_duplicate(a: int, b: int, threshold: int = 6) -> bool:
    return hamming(a, b) <= threshold


# ---------------------------------------------------------------- URL 规范化

def normalize_url(url: str) -> str:
    """去掉跟踪参数、锚点、末尾斜杠，得到用于去重的规范形式。"""
    if not url:
        return ""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()

    scheme = "https" if p.scheme in ("http", "https") else p.scheme
    host = (p.hostname or "").lower()
    for pre in ("www.", "m.", "mobile.", "wap."):
        if host.startswith(pre):
            host = host[len(pre):]
    port = ""
    if p.port and p.port not in (80, 443):
        port = f":{p.port}"

    # 过滤跟踪参数
    kept: List[Tuple[str, str]] = []
    for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=False):
        if URL_TRACK_PARAMS.match(k):
            continue
        kept.append((k, v))
    query = urllib.parse.urlencode(sorted(kept))

    path = re.sub(r"/+$", "", p.path) or "/"
    return f"{scheme}://{host}{port}{path}" + (f"?{query}" if query else "")


def url_key(url: str) -> str:
    """用于严格去重的短键。"""
    n = normalize_url(url)
    return hashlib.sha1(n.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 关键词提取

def term_frequencies(texts: Iterable[str]) -> Counter:
    c: Counter = Counter()
    for t in texts:
        c.update(content_tokens(t))
    return c


CJK_NGRAM_SIZES = (2, 3, 4, 5, 6)


def cjk_ngrams(text: str, sizes: Sequence[int] = CJK_NGRAM_SIZES) -> List[str]:
    """中文 n 元组。二元组太碎（"电解质" → "电解"+"解质"），
    长一点的 n-gram 才可能还原出真正的术语。"""
    out: List[str] = []
    for run in CJK_RUN.findall(text):
        for n in sizes:
            if len(run) < n:
                continue
            for i in range(len(run) - n + 1):
                out.append(run[i:i + n])
    return out


def _entropy(counter: Counter) -> float:
    """邻接分布的熵。熵高 = 左右邻居多样 = 边界成立。"""
    total = sum(counter.values())
    if total <= 0:
        return 0.0
    ent = 0.0
    for v in counter.values():
        p = v / total
        if p > 0:
            ent -= p * math.log(p)
    return ent


def discover_terms(texts: Sequence[str], min_freq: int = 2,
                   min_boundary: float = 0.8,
                   sizes: Sequence[int] = CJK_NGRAM_SIZES
                   ) -> List[Tuple[float, str]]:
    """
    无词典中文术语发现，用**左右邻接熵**判定词边界。

    这是关键：靠词频选 n-gram 会选出 "态电池产" 这种跨词碎片 —— 它词频和
    "固态电池" 一样高，因为它俩总是一起出现。但它的左邻永远只有"固"、
    右邻永远只有"业"，邻接熵为 0，说明它不是一个独立词，而是更长词的内部片段。

    真正的词（"固态电池"）左右邻居五花八门（全/条/多…，产/中/技…），熵高。
    """
    freq: Counter = Counter()
    left: Dict[str, Counter] = {}
    right: Dict[str, Counter] = {}

    for t in texts:
        for run in CJK_RUN.findall(t):
            L = len(run)
            for n in sizes:
                if L < n:
                    continue
                for i in range(L - n + 1):
                    g = run[i:i + n]
                    freq[g] += 1
                    if i > 0:
                        left.setdefault(g, Counter())[run[i - 1]] += 1
                    if i + n < L:
                        right.setdefault(g, Counter())[run[i + n]] += 1

    out: List[Tuple[float, str]] = []
    for term, f in freq.items():
        if f < min_freq:
            continue
        hl = _entropy(left.get(term, Counter()))
        hr = _entropy(right.get(term, Counter()))
        boundary = min(hl, hr)
        # 所有长度都要有像样的边界；2 字词门槛略低（短词上下文天然更少）
        need = min_boundary if len(term) >= 3 else min_boundary * 0.75
        if boundary < need:
            continue
        score = f * (1.0 + min(len(term), 6) / 6.0) * (1.0 + boundary)
        out.append((score, term))

    out.sort(key=lambda x: (-x[0], -len(x[1])))
    return out


def salient_terms(texts: Sequence[str], exclude: Sequence[str] = (),
                  top_n: int = 12, min_len: int = 2, min_freq: int = 2) -> List[str]:
    """
    找出"高频、非查询词、且边界成立"的术语，用于生成追问。
    中文走邻接熵发现；英文按词频。最后做最长匹配去碎片。
    """
    if not texts:
        return []
    excluded: Set[str] = set()
    for e in exclude:
        excluded |= token_set(e)

    scored: List[Tuple[float, str]] = []
    for score, term in discover_terms(texts, min_freq=min_freq):
        if term in excluded or term in STOPWORDS or term.isdigit():
            continue
        if len(term) < min_len:
            continue
        scored.append((score, term))

    # 英文术语（长单词）单独补进来
    en: Counter = Counter()
    for t in texts:
        for w in LATIN_WORD.findall(t):
            w = w.lower()
            if len(w) >= 4 and w not in STOPWORDS:
                en[w] += 1
    for w, f in en.most_common(top_n):
        if f >= min_freq and w not in excluded:
            scored.append((f * 2.0, w))

    scored.sort(key=lambda x: (-x[0], -len(x[1])))

    # 先按分数贪心取一批，再做最长匹配去碎片
    picked: List[str] = []
    for _, term in scored:
        if any(term in o for o in picked):
            continue
        picked.append(term)
        if len(picked) >= top_n * 3:
            break

    final: List[str] = []
    for term in sorted(picked, key=len, reverse=True):
        if any(term != o and term in o for o in picked):
            continue
        final.append(term)
    return final[:top_n]


def top_terms_of_text(text: str, top_n: int = 10) -> List[str]:
    c = Counter(content_tokens(text))
    return [t for t, _ in c.most_common(top_n)]
