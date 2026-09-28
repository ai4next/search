# -*- coding: utf-8 -*-
"""
ds.planner —— 查询规划：把一个问题拆成一组可搜索的子查询，并在证据不足时追问

深度搜索与"多搜几次"的区别就在这里：

- **多搜几次**：同一个问题换几个引擎，召回的是同一批结果。
- **深度搜索**：先问"这个问题由哪几个面构成"，每个面单独取证；
  再问"哪些面还没证据、哪些说法还没被验证"，据此发起下一轮。

两个机制：
1. `plan()` —— 首轮分解。按问题类型（是什么/如何/为什么/对比/最新）选择面，
   用中英双语的模板生成子查询。
2. `follow_up()` —— 后续轮次。基于已覆盖的面、未覆盖的面、以及正文里
   新出现的显著实体，生成追问，并对已问过的查询去重。

不依赖任何 LLM —— 但支持外部（agent）直接注入子查询，见 `explicit` 参数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

from . import text as T

# ---------------------------------------------------------------- 数据结构


@dataclass
class SubQuery:
    text: str
    facet: str = "general"
    reason: str = ""
    round: int = 1
    weight: float = 1.0

    def to_dict(self) -> Dict:
        return {"text": self.text, "facet": self.facet, "round": self.round,
                "reason": self.reason, "weight": round(self.weight, 3)}


# ---------------------------------------------------------------- 面（facet）定义

@dataclass
class Facet:
    id: str
    label: str
    zh: List[str]      # 中文模板，{s} = 主题
    en: List[str]      # 英文模板
    keywords: List[str] = field(default_factory=list)  # 判断该面是否已被证据覆盖


FACETS: Dict[str, Facet] = {
    f.id: f for f in [
        Facet("definition", "定义与概述",
              ["{s} 是什么", "什么是 {s}", "{s} 定义 概念", "{s} 简介"],
              ["what is {s}", "{s} definition overview"],
              ["定义", "是指", "是一种", "概念", "definition", "refers to", "is a"]),
        Facet("mechanism", "原理与机制",
              ["{s} 原理 工作机制", "{s} 如何工作", "{s} 实现原理"],
              ["how does {s} work", "{s} mechanism explained"],
              ["原理", "机制", "流程", "架构", "mechanism", "architecture", "pipeline"]),
        Facet("status", "现状与最新进展",
              ["{s} 最新进展 2025", "{s} 现状 发展", "{s} 最新研究"],
              ["{s} latest developments 2025", "{s} state of the art"],
              ["最新", "进展", "2024", "2025", "2026", "recent", "latest", "state of the art"]),
        Facet("comparison", "对比与选型",
              ["{s} 对比 优缺点", "{s} 区别 差异", "{s} 替代方案"],
              ["{s} vs alternatives", "{s} comparison pros cons"],
              ["对比", "相比", "区别", "优势", "劣势", "versus", "compared", "pros and cons"]),
        Facet("application", "应用与实践",
              ["{s} 应用 案例 实践", "{s} 落地 场景"],
              ["{s} use cases applications", "{s} in practice case study"],
              ["应用", "案例", "场景", "实践", "use case", "application", "case study"]),
        Facet("data", "数据与量化",
              ["{s} 数据 统计 报告", "{s} 市场规模 数字"],
              ["{s} statistics data report", "{s} market size numbers"],
              ["数据", "统计", "占比", "增长", "市场规模", "statistics", "percent", "survey"]),
        Facet("risk", "风险与局限",
              ["{s} 风险 局限 挑战", "{s} 缺点 问题"],
              ["{s} limitations risks challenges", "{s} criticism problems"],
              ["风险", "局限", "挑战", "问题", "不足", "limitation", "risk", "challenge", "drawback"]),
        Facet("howto", "方法与教程",
              ["{s} 教程 步骤 方法", "如何 {s}", "{s} 实践指南"],
              ["{s} tutorial how to", "{s} guide step by step"],
              ["教程", "步骤", "方法", "指南", "tutorial", "step by step", "how to", "guide"]),
        Facet("expert", "专家与权威观点",
              ["{s} 专家 观点 分析", "{s} 权威 解读"],
              ["{s} expert analysis opinion", "{s} research findings"],
              ["专家", "分析", "研究", "报告", "expert", "analyst", "according to"]),
        Facet("counter", "反例与争议",
              ["{s} 争议 反驳 失败案例", "{s} 为什么不行 质疑"],
              ["{s} controversy debate criticism", "{s} failure counterargument"],
              ["争议", "质疑", "反驳", "失败", "controvers", "debate", "however", "failure"]),
    ]
}

# 问题类型 → 优先面（按顺序）
QUESTION_PLANS: List[tuple] = [
    (re.compile(r"(什么是|是什么|何为|定义|what\s+is|definition)", re.I),
     ["definition", "mechanism", "application", "comparison"]),
    (re.compile(r"(如何|怎么|怎样|步骤|教程|how\s+to|tutorial|guide)", re.I),
     ["howto", "mechanism", "application", "risk"]),
    (re.compile(r"(为什么|为何|原因|why|reason)", re.I),
     ["mechanism", "risk", "expert", "data"]),
    (re.compile(r"(对比|区别|差异|优劣|vs\.?|versus|compare|difference)", re.I),
     ["comparison", "definition", "application", "risk"]),
    (re.compile(r"(最新|进展|现状|趋势|latest|recent|trend|2025|2026)", re.I),
     ["status", "data", "expert", "application"]),
    (re.compile(r"(风险|局限|缺点|问题|挑战|risk|limitation|drawback|problem)", re.I),
     ["risk", "counter", "comparison", "expert"]),
    (re.compile(r"(方案|选型|推荐|哪个好|best|recommend|which)", re.I),
     ["comparison", "application", "risk", "howto"]),
]

# 意图 → 补充面
INTENT_FACETS = {
    "academic": ["definition", "mechanism", "status", "expert", "data"],
    "tech": ["howto", "mechanism", "comparison", "risk", "application"],
    "finance": ["data", "status", "risk", "expert", "comparison"],
    "news": ["status", "expert", "data", "counter"],
    "social": ["expert", "application", "risk", "counter"],
    "knowledge": ["definition", "mechanism", "application", "comparison"],
    "general": ["definition", "mechanism", "status", "application", "risk"],
}

DEFAULT_FACETS = ["definition", "mechanism", "status", "application", "risk"]

# 主题抽取：去掉疑问/客套成分
_Q_PREFIX = re.compile(
    r"^(请问|请帮我|请|帮我|帮忙|我想知道|我想了解|我想|我要|麻烦|想问一下|想问|想知道|"
    r"了解下|了解一下|介绍一下|介绍|讲讲|说说|什么是|何为|啥是|如何|怎么|怎样|怎么样|"
    r"为什么|为何|哪些|哪个|"
    r"how to|what is|what are|tell me about|explain|please)\s*",
    re.I,
)
_Q_SUFFIX = re.compile(
    r"(是什么|有哪些|怎么样|怎么办|如何|为什么|为何|吗|呢|吧|啊|"
    r"的优缺点|的优势和劣势|的优缺点是什么)\s*$"
)
_FACET_TAIL = re.compile(
    r"(的)?(优缺点|优势与劣势|优势和劣势|优劣势|优势|劣势|原理|定义|现状|进展|"
    r"方法|教程|案例|应用|风险|问题|对比|区别|差异|分析|介绍|综述|最新进展|"
    r"局限|局限性|缺点|挑战|争议|价值|意义|影响|趋势|前景|原因|方案|选型)\s*$"
)
_PUNCT_TAIL = re.compile(r"[\s?？。.!！,，、;；:：]+$")


def extract_subject(query: str) -> str:
    """从自然语言问题里提取可搜索的主题短语。"""
    s = (query or "").strip()
    s = _Q_PREFIX.sub("", s)
    s = _Q_SUFFIX.sub("", s)
    s = _FACET_TAIL.sub("", s)
    s = _PUNCT_TAIL.sub("", s)
    s = _Q_PREFIX.sub("", s).strip()
    return s or (query or "").strip()


def _looks_chinese(s: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", s or ""))


# ---------------------------------------------------------------- 规划器

class Planner:
    """
    查询规划器。

    explicit 不为空时直接采用外部给的子查询（agent 可以用 LLM 规划后注入），
    但**仍然**会做主题抽取与去重，保证质量下限。
    """

    def __init__(self, breadth: int = 5, max_follow_up: int = 4,
                 explicit: Optional[Sequence[str]] = None):
        self.breadth = max(1, breadth)
        self.max_follow_up = max(1, max_follow_up)
        self.explicit = [q.strip() for q in (explicit or []) if q and q.strip()]
        self.asked: List[str] = []
        self.facet_asked: Dict[str, int] = {}
        self._asked_sets: List[Set[str]] = []
        self.intent: str = "general"

    # -------------------------------------------------- 内部

    def _already_asked(self, q: str) -> bool:
        ts = T.token_set(q)
        for prev in self._asked_sets:
            if T.jaccard(ts, prev) >= 0.75:
                return True
        return False

    def _record(self, q: str) -> None:
        self.asked.append(q)
        self._asked_sets.append(T.token_set(q))

    def _templates(self, facet_id: str, subject: str, use_en: bool) -> List[str]:
        facet = FACETS.get(facet_id)
        if facet is None:
            return []
        pool = facet.en if use_en else facet.zh
        return [t.format(s=subject) for t in pool]

    # -------------------------------------------------- 首轮规划

    def plan(self, query: str, intent: str = "general",
             include_original: bool = True) -> List[SubQuery]:
        query = (query or "").strip()
        if not query:
            return []

        self.intent = intent
        out: List[SubQuery] = []

        # 外部注入的子查询优先（agent 规划能力更强）
        if self.explicit:
            if include_original:
                out.append(SubQuery(query, "original", "原始问题", 1, 1.0))
                self._record(query)
            for q in self.explicit:
                if self._already_asked(q):
                    continue
                out.append(SubQuery(q, "explicit", "外部规划", 1, 0.95))
                self._record(q)
            return out[: self.breadth + 1]

        if include_original:
            out.append(SubQuery(query, "original", "原始问题", 1, 1.0))
            self._record(query)

        # 高级语法是用户显式约束，不再分解，避免破坏 site:/filetype:
        if re.search(r"(site:|filetype:|intitle:|inurl:)", query, re.I):
            return out

        subject = extract_subject(query)
        use_en = not _looks_chinese(subject)

        # 选面：问题类型优先，意图补充
        facet_ids: List[str] = []
        for pat, fids in QUESTION_PLANS:
            if pat.search(query):
                facet_ids.extend(fids)
                break
        for fid in INTENT_FACETS.get(intent, DEFAULT_FACETS):
            if fid not in facet_ids:
                facet_ids.append(fid)
        if not facet_ids:
            facet_ids = list(DEFAULT_FACETS)

        # 问题里**已经问到**的面往后放 —— 否则会出现
        # "RAG 的局限" → "RAG 的局限 风险 局限 挑战" 这种自我重复。
        # 分解的价值在于补齐没问到的角度，而不是复述问题。
        q_low = query.lower()

        def _implied(fid: str) -> bool:
            f = FACETS.get(fid)
            return bool(f) and any(k.lower() in q_low for k in f.keywords)

        facet_ids.sort(key=_implied)

        budget = max(0, self.breadth - len(out))
        # 轮转取模板：先给每个面一条，再回头取第二条。
        # 广度应该花在"覆盖更多角度"上，而不是"同一角度换三种说法"。
        max_tpl = max((len(self._templates(f, subject, use_en)) for f in facet_ids),
                      default=0)
        for tpl_idx in range(max_tpl):
            if budget <= 0:
                break
            for fid in facet_ids:
                if budget <= 0:
                    break
                tpls = self._templates(fid, subject, use_en)
                if tpl_idx >= len(tpls):
                    continue
                cand = tpls[tpl_idx]
                if cand.strip() == query or self._already_asked(cand):
                    continue
                out.append(SubQuery(cand, fid, f"{FACETS[fid].label}", 1, 0.85))
                self._record(cand)
                self.facet_asked[fid] = self.facet_asked.get(fid, 0) + 1
                budget -= 1

        return out

    # -------------------------------------------------- 追问规划

    def follow_up(
        self,
        query: str,
        round_no: int,
        facet_coverage: Dict[str, int],
        evidence_texts: Sequence[str],
        max_n: Optional[int] = None,
        contradictions: Sequence[str] = (),
    ) -> List[SubQuery]:
        """
        依据"缺口"生成下一轮查询。三类缺口：
        1. **面无证据** —— 该面一条证据都没有 → 换一个模板重问
        2. **有实体的面** —— 正文里高频出现但没被直接搜过的实体 → 实体 × 面
        3. **有争议的点** —— 出现矛盾信号 → 针对性验证
        """
        n = max_n or self.max_follow_up
        subject = extract_subject(query)
        use_en = not _looks_chinese(subject)
        out: List[SubQuery] = []

        # 1) 空面补搜
        empty_facets = [f for f in FACETS if facet_coverage.get(f, 0) == 0]
        # 优先补"该意图下重要但还空着"的面
        priority = INTENT_FACETS.get(self.intent, DEFAULT_FACETS)
        empty_facets.sort(key=lambda f: priority.index(f) if f in priority else 99)

        for fid in empty_facets:
            if len(out) >= n:
                break
            for cand in self._templates(fid, subject, use_en):
                if self._already_asked(cand):
                    continue
                out.append(SubQuery(cand, fid, f"缺口补搜：{FACETS[fid].label}", round_no, 0.8))
                self._record(cand)
                break

        # 2) 显著实体 × 主题 —— "主题 + 实体"是最有效的定向深挖式查询
        if len(out) < n and evidence_texts:
            terms = T.salient_terms(evidence_texts, exclude=[query], top_n=6)
            for term in terms:
                if len(out) >= n:
                    break
                if len(term) < 2 or term in subject:
                    continue
                cand = f"{subject} {term}"
                if len(cand) < 4 or self._already_asked(cand):
                    continue
                out.append(SubQuery(cand, "entity", f"深挖实体「{term}」", round_no, 0.7))
                self._record(cand)

        # 3) 争议验证
        if len(out) < n and contradictions:
            for c in contradictions[:2]:
                if len(out) >= n:
                    break
                cand = f"{c} 争议 不同观点"
                if self._already_asked(cand):
                    continue
                out.append(SubQuery(cand, "counter", f"验证分歧：{c[:24]}", round_no, 0.75))
                self._record(cand)

        return out[:n]

    # -------------------------------------------------- 覆盖度

    @staticmethod
    def facet_coverage(texts: Sequence[str]) -> Dict[str, int]:
        """
        用关键词命中数估计每个面被证据覆盖的程度。
        粗糙但有效：它只需要区分"完全没提到"和"有内容"。
        """
        joined = "\n".join(texts).lower()
        cov: Dict[str, int] = {}
        for fid, facet in FACETS.items():
            hits = sum(1 for kw in facet.keywords if kw.lower() in joined)
            cov[fid] = hits
        return cov

    @staticmethod
    def uncovered_facets(coverage: Dict[str, int], threshold: int = 1) -> List[str]:
        return [f for f, c in coverage.items() if c < threshold]
