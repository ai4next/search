# -*- coding: utf-8 -*-
"""
ds.pipeline —— 多轮深度搜索编排

一轮深度搜索 = 规划 → 检索 → 挑 URL → 抓正文 → 打分 → 找缺口 → 再规划

关键设计取舍：

1. **先挑后抓。** 检索回来的链接可能上百条，但只有一部分值得抓正文。
   抓取是整条流水线里最慢、最容易被反爬的一步，所以先用
   "引擎权重 + 排名 + 多引擎印证 + 域名权威度 + 标题匹配" 挑出最值得的 N 条。
2. **多引擎印证是强信号。** 三个独立引擎都指向同一 URL，比单个引擎排第一更可信。
3. **缺口驱动而非固定轮数。** 每轮结束后计算"哪些主题面还没证据"，
   据此生成下一轮查询；没有缺口就提前停，不空跑。
4. **任何单点失败都不致命。** 引擎、抓取、解析全部 try/except 兜住，
   整轮搜索永远返回一份（可能不完整但诚实的）证据包。
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import engines as E
from . import extract as X
from . import rank as R
from . import report as RP
from . import text as T
from .net import HttpClient, domain_of
from .planner import FACETS, Planner, SubQuery


@dataclass
class DeepConfig:
    """一次深度搜索的全部可调参数。"""

    max_rounds: int = 2
    breadth: int = 5               # 首轮子查询数
    follow_up: int = 4             # 每轮追问数上限
    per_engine: int = 8            # 每个引擎每查询取多少条
    fetch_top_k: int = 14          # 每轮最多抓多少个页面
    max_sources: int = 14          # 最终保留多少条证据
    concurrency: int = 6           # 并发度
    timeout: float = 12.0
    round_timeout: float = 35.0   # 单轮检索的硬上限：慢引擎不许拖垮整轮
    retries: int = 1
    min_interval: float = 0.6
    cache: bool = True
    cache_ttl: int = 900
    respect_robots: bool = True
    fetch_pages: bool = True       # False = 只做聚合检索（快）
    intent: Optional[str] = None
    engines: Sequence[str] = ()
    exclude_engines: Sequence[str] = ()
    queries: Sequence[str] = ()    # 外部注入的规划
    min_content_chars: int = 200
    summary_sentences: int = 12
    verbose: bool = False
    cache_dir: Optional[str] = None
    raw: Dict = field(default_factory=dict, repr=False)  # 原始配置（去重/排序权重等）

    def to_dict(self) -> Dict:
        d = {k: v for k, v in self.__dict__.items() if k != "raw"}
        d["engines"] = list(self.engines)
        d["exclude_engines"] = list(self.exclude_engines)
        d["queries"] = list(self.queries)
        return d

    @classmethod
    def from_file(cls, path=None, **overrides) -> "DeepConfig":
        """
        从 config.yaml 的 `deep_search:` 段读取默认值，再用 overrides 覆盖。
        配置文件缺失/损坏时静默使用内置默认值 —— 搜索不该因为配置问题而失败。
        """
        import dataclasses

        from . import config as C

        raw = C.load_config(path if path is not None else C.default_config_path())
        section = raw.get("deep_search") if isinstance(raw, dict) else None
        if not isinstance(section, dict):
            section = {}

        known = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in section.items() if k in known and v is not None}
        kwargs.update({k: v for k, v in overrides.items()
                       if k in known and v is not None})
        obj = cls(**kwargs)
        obj.raw = raw if isinstance(raw, dict) else {}
        return obj


class DeepSearch:
    """深度搜索执行器。"""

    def __init__(self, config: Optional[DeepConfig] = None):
        self.cfg = config or DeepConfig()
        self.client = HttpClient(
            timeout=self.cfg.timeout,
            retries=self.cfg.retries,
            min_interval=self.cfg.min_interval,
            cache_dir=self.cfg.cache_dir,
            cache_ttl=self.cfg.cache_ttl,
            cache_enabled=self.cfg.cache,
            respect_robots=self.cfg.respect_robots,
            verbose=self.cfg.verbose,
        )
        self.engine_stats: Dict[str, Dict] = {}
        self.rounds_log: List[Dict] = []
        self.warnings: List[str] = []
        self._seen_urls: set = set()
        self._final_sources: List[R.Source] = []

        # 配置里的引擎权重覆盖
        E.apply_engine_weights((self.cfg.raw or {}).get("engine_weights"))

    # -------------------------------------------------- 日志

    def _log(self, msg: str) -> None:
        if self.cfg.verbose:
            print(msg, flush=True)

    def _stat(self, engine_id: str, **kw) -> None:
        s = self.engine_stats.setdefault(engine_id, {"hits": 0, "calls": 0, "errors": 0})
        for k, v in kw.items():
            if k == "hits":
                s["hits"] += v
            elif k == "errors":
                s["errors"] += v
            else:
                s[k] = v

    # -------------------------------------------------- 检索

    def _search_one(self, engine_id: str, sq: SubQuery) -> Tuple[str, SubQuery, List[E.Hit]]:
        t0 = time.time()
        hits = E.search_engine(self.client, engine_id, sq.text, self.cfg.per_engine)
        self._stat(engine_id, calls=1, hits=len(hits),
                   ms=int((time.time() - t0) * 1000),
                   errors=0 if hits else 1)
        return engine_id, sq, hits

    def run_round(self, subqueries: Sequence[SubQuery], round_no: int) -> Tuple[List[E.Hit], List[str]]:
        """并行执行一轮检索，返回 (全部命中, 实际使用的引擎)。"""
        engine_ids = E.route(
            subqueries[0].text if subqueries else "",
            self.cfg.intent,
            include=self.cfg.engines,
            exclude=self.cfg.exclude_engines,
        )
        self._log(f"\n[第 {round_no} 轮] 引擎 {len(engine_ids)} 个 × 子查询 {len(subqueries)} 条")

        tasks: List[Tuple[str, SubQuery]] = []
        for sq in subqueries:
            for eid in engine_ids:
                tasks.append((eid, sq))

        all_hits: List[E.Hit] = []
        if not tasks:
            return all_hits, engine_ids

        workers = max(1, min(self.cfg.concurrency, len(tasks)))
        deadline = time.time() + self.cfg.round_timeout
        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            futs = [ex.submit(self._search_one, eid, sq) for eid, sq in tasks]
            try:
                for f in as_completed(futs, timeout=max(1.0, deadline - time.time())):
                    try:
                        _, sq, hits = f.result()
                        for h in hits:
                            h.extra.setdefault("facet", sq.facet)
                            h.extra.setdefault("subquery", sq.text)
                        all_hits.extend(hits)
                    except Exception as e:
                        self._log(f"    [检索异常] {e}")
            except FuturesTimeout:
                # 到点就收工，拿已经回来的结果继续 —— 深度搜索不该被单个慢引擎卡死
                pending = sum(1 for f in futs if not f.done())
                self._log(f"    [轮次超时 {self.cfg.round_timeout:.0f}s] 放弃 {pending} 个未完成请求")
                self.warnings.append(
                    f"第 {round_no} 轮有 {pending} 个检索请求超时未返回，结果可能不完整。"
                )
        finally:
            # wait=False：不阻塞主流程等慢线程，它们会随各自请求超时自行结束
            ex.shutdown(wait=False, cancel_futures=True)

        self._log(f"    命中 {len(all_hits)} 条（去重前）")
        return all_hits, engine_ids

    # -------------------------------------------------- 挑 URL

    def rank_hits(self, hits: Sequence[E.Hit], query: str,
                  facet_weight: Optional[Dict[str, float]] = None) -> List[E.Hit]:
        """
        给候选链接打分，决定抓取优先级。

        信号：
        - 引擎权重 / 该引擎内的排名
        - **多引擎印证**（同一 URL 被多个引擎返回）
        - 域名权威度
        - 标题/摘要与查询的词面匹配
        """
        by_url: Dict[str, E.Hit] = {}
        url_engines: Dict[str, set] = defaultdict(set)
        url_queries: Dict[str, set] = defaultdict(set)
        url_facets: Dict[str, set] = defaultdict(set)
        url_best_pos: Dict[str, int] = {}

        for h in hits:
            key = T.normalize_url(h.url) or h.url
            if not key:
                continue
            url_engines[key].add(h.engine_id)
            url_queries[key].add(h.query)
            url_facets[key].add(h.extra.get("facet", "general"))
            prev = url_best_pos.get(key, 999)
            url_best_pos[key] = min(prev, h.position or 999)
            # 保留摘要最长的那条作为代表
            if key not in by_url or len(h.snippet) > len(by_url[key].snippet):
                by_url[key] = h

        q_terms = set(T.content_tokens(query))
        scored: List[Tuple[float, E.Hit]] = []
        for key, h in by_url.items():
            if key in self._seen_urls:
                continue
            score = 0.0
            # 引擎权重（取最强的那个）
            engs = url_engines[key]
            score += 3.0 * max((E.ENGINES[e].weight for e in engs if e in E.ENGINES), default=0.5)
            # 多引擎印证
            score += 2.2 * (len(engs) - 1)
            # 排名靠前
            pos = url_best_pos.get(key, 10)
            score += max(0.0, 2.5 - pos * 0.18)
            # 权威度
            score += 2.0 * R.authority_of(domain_of(h.url))
            # 词面匹配
            title_tokens = T.token_set(h.title)
            snip_tokens = T.token_set(h.snippet)
            if q_terms:
                score += 3.0 * len(title_tokens & q_terms) / max(1, len(q_terms))
                score += 1.0 * len(snip_tokens & q_terms) / max(1, len(q_terms))
            # 被多个子查询命中 = 主题核心
            score += 0.8 * (len(url_queries[key]) - 1)
            # 用户给的域名偏好（facet 权重）暂不参与，保持简单
            scored.append((score, h))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [h for _, h in scored]

    # -------------------------------------------------- 抓取 + 抽取

    def _fetch_one(self, hit: E.Hit) -> R.Source:
        src = R.Source(
            url=hit.url,
            title=hit.title,
            snippet=hit.snippet,
            engines=[hit.engine_id],
            queries=[hit.query],
            facets=[hit.extra.get("facet", "general")],
            site=domain_of(hit.url),
        )
        try:
            resp = self.client.get(hit.url)
            if not resp.ok:
                src.status = "fetch_failed"
                src.error = resp.error or f"HTTP {resp.status}"
                return src
            art = X.extract_article(resp.text, hit.url)
            if art.char_count < self.cfg.min_content_chars:
                src.status = "empty"
                src.error = f"正文过短({art.char_count}字)"
                # 仍然保留标题等元信息，但内容为空
                src.title = src.title or art.title
                return src
            src.content = art.text
            src.paragraphs = art.paragraphs
            src.title = art.title or hit.title
            src.published = art.published
            src.author = art.author
            src.site = art.site or domain_of(hit.url)
            src.status = "ok"
        except Exception as e:
            src.status = "fetch_failed"
            src.error = str(e)[:160]
        return src

    def fetch_and_extract(self, hits: Sequence[E.Hit]) -> List[R.Source]:
        picked = list(hits)[: self.cfg.fetch_top_k]
        if not picked:
            return []
        self._log(f"    抓取正文 {len(picked)} 个页面…")
        out: List[R.Source] = []
        workers = max(1, min(self.cfg.concurrency, len(picked)))
        deadline = time.time() + self.cfg.round_timeout
        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            futs = [ex.submit(self._fetch_one, h) for h in picked]
            try:
                for f in as_completed(futs, timeout=max(1.0, deadline - time.time())):
                    try:
                        out.append(f.result())
                    except Exception as e:
                        self._log(f"    [抓取异常] {e}")
            except FuturesTimeout:
                pending = sum(1 for f in futs if not f.done())
                self._log(f"    [抓取超时] 放弃 {pending} 个未完成页面")
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        ok = sum(1 for s in out if s.has_content)
        self._log(f"    成功取得正文 {ok}/{len(out)} 条")
        return out

    # -------------------------------------------------- 主流程

    def search(self, query: str) -> Dict:
        t_start = time.time()
        cfg = self.cfg

        intent = E.detect_intent(query, cfg.intent)
        planner = Planner(breadth=cfg.breadth, max_follow_up=cfg.follow_up,
                          explicit=cfg.queries)

        all_hits: List[E.Hit] = []
        all_sources: List[R.Source] = []
        used_engines: List[str] = []
        planned: List[SubQuery] = []   # 全轮次的子查询，用于证据包
        round_no = 0

        subqueries = planner.plan(query, intent)
        planned.extend(subqueries)
        self._log(f"[规划] 意图={intent}，首轮子查询 {len(subqueries)} 条")
        for sq in subqueries:
            self._log(f"    · [{sq.facet}] {sq.text}")

        while round_no < cfg.max_rounds and subqueries:
            round_no += 1
            r_start = time.time()
            hits, engine_ids = self.run_round(subqueries, round_no)
            used_engines = engine_ids
            all_hits.extend(hits)

            ranked = self.rank_hits(hits, query)
            if cfg.fetch_pages:
                sources = self.fetch_and_extract(ranked)
                # 标记已处理，避免下一轮重复抓取
                for h in ranked[: cfg.fetch_top_k]:
                    self._seen_urls.add(T.normalize_url(h.url) or h.url)
            else:
                sources = [
                    R.Source(url=h.url, title=h.title, snippet=h.snippet,
                             engines=[h.engine_id], queries=[h.query],
                             facets=[h.extra.get("facet", "general")],
                             site=domain_of(h.url),
                             status="ok" if h.snippet else "empty")
                    for h in ranked[: cfg.fetch_top_k]
                ]
            all_sources.extend(sources)

            # 覆盖度与缺口
            texts = [s.best_text() for s in all_sources if s.best_text()]
            facet_cov = Planner.facet_coverage(texts)
            gaps = [f for f, c in facet_cov.items() if c == 0]

            self.rounds_log.append({
                "round": round_no,
                "subqueries": [sq.to_dict() for sq in subqueries],
                "engines": engine_ids,
                "hits": len(hits),
                "fetched": len(sources),
                "with_content": sum(1 for s in sources if s.has_content),
                "empty_facets": gaps,
                "seconds": round(round(time.time() - r_start, 2), 2),
            })
            self._log(f"[第 {round_no} 轮完成] 命中 {len(hits)}，正文 {sum(1 for s in sources if s.has_content)}，"
                      f"空面 {len(gaps)}")

            # 决定是否继续
            if round_no >= cfg.max_rounds:
                break
            if not cfg.fetch_pages:
                break
            if not gaps and round_no >= 1:
                # 面都覆盖了，且已有足够正文 → 提前停
                if sum(1 for s in all_sources if s.has_content) >= cfg.max_sources:
                    self._log("[提前停止] 主题面已覆盖且证据充足")
                    break

            evidence_texts = [s.content for s in all_sources if s.has_content][:12]
            next_qs = planner.follow_up(
                query, round_no + 1, facet_cov, evidence_texts,
                max_n=cfg.follow_up,
            )
            if not next_qs:
                self._log("[停止] 无新的追问")
                break
            subqueries = next_qs
            planned.extend(next_qs)
            self._log(f"[第 {round_no + 1} 轮规划] 追问 {len(subqueries)} 条")
            for sq in subqueries:
                self._log(f"    · [{sq.facet}] {sq.text}")

        # ---------------- 收尾：去重 → 排序 → 摘要
        dd = (self.cfg.raw or {}).get("deduplication") or {}
        try:
            title_th = float(dd.get("title_similarity_threshold", 0.86))
        except (TypeError, ValueError):
            title_th = 0.86
        try:
            fp_dist = int(dd.get("fingerprint_hamming_distance", 6))
        except (TypeError, ValueError):
            fp_dist = 6

        sources, merged = R.dedupe(all_sources, title_threshold=title_th,
                                   fingerprint_distance=fp_dist)
        if merged:
            self._log(f"[去重] 合并 {len(merged)} 条重复来源")

        rk = (self.cfg.raw or {}).get("ranking") or {}
        rank_weights = {k: float(v) for k, v in rk.items()
                        if isinstance(v, (int, float)) and not isinstance(v, bool)}
        R.score_sources(sources, query, weights=rank_weights or None)
        sources = [s for s in sources if s.status == "ok" or s.snippet][: cfg.max_sources]
        self._final_sources = sources

        # 分配引用编号（按最终顺序）
        for i, s in enumerate(sources, 1):
            s.idx = i

        # 引证片段
        q_terms = T.content_tokens(query)
        for s in sources:
            s.quotes = R.select_quotes(s, q_terms, n=3)

        summary = R.extractive_summary(sources, query, max_sentences=cfg.summary_sentences)
        divergences = R.find_divergences(summary)
        coverage = R.coverage_report(sources, query)

        texts = [s.content for s in sources if s.has_content]
        facet_cov = Planner.facet_coverage(texts) if texts else {}
        gaps = [f for f, c in facet_cov.items() if c == 0]

        # 诚实声明局限
        if not texts:
            self.warnings.append(
                "本轮没有取得任何正文（可能全部被反爬拦截或网络不可达），"
                "要点为空。下方来源列表仅为检索命中，未经正文核验。"
            )
        else:
            thin = [s for s in sources if s.has_content and s.char_count < 800]
            if len(thin) > len(texts) * 0.5:
                self.warnings.append("超过半数来源正文较短，证据厚度有限。")
        if gaps:
            self.warnings.append(
                f"以下主题面缺少证据，结论不宜覆盖：{'、'.join(FACETS[g].label for g in gaps if g in FACETS)}"
            )
        if self.client.stats.get("blocked_by_robots"):
            self.warnings.append(
                f"{self.client.stats['blocked_by_robots']} 个页面因 robots.txt 未抓取。"
            )

        elapsed = time.time() - t_start
        self._log(f"\n[完成] 用时 {elapsed:.1f}s，证据 {len(sources)} 条，"
                  f"正文 {len(texts)} 条，摘要 {len(summary)} 句")

        return RP.build_bundle(
            query=query,
            intent=intent,
            plan=planned,
            sources=sources,
            summary=summary,
            divergences=divergences,
            coverage=coverage,
            rounds=self.rounds_log,
            engine_stats=self.engine_stats,
            http_stats=dict(self.client.stats),
            elapsed=elapsed,
            facet_coverage=facet_cov,
            gaps=[FACETS[g].label for g in gaps if g in FACETS],
            warnings=self.warnings,
        )


def deep_search(query: str, config: Optional[DeepConfig] = None) -> Dict:
    """便捷入口。"""
    return DeepSearch(config or DeepConfig()).search(query)
