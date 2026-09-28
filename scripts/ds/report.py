# -*- coding: utf-8 -*-
"""
ds.report —— 证据包（JSON）与 Markdown 报告

深度搜索的输出有两种消费者：
- **agent/程序**：要结构化证据包（`--json`），自己再加工成最终答复。
- **人**：要一份能直接读、每个论断都带出处的报告（`--format markdown`）。

两者共用同一份数据，只是渲染不同。关键纪律：**报告里每一句实质内容都带 [n] 出处**，
没有出处的句子不许出现 —— 否则报告和"看起来对的话"就没区别了。
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from typing import Dict, List, Sequence

from .rank import Source


def _fmt_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60)}s"


def build_bundle(
    query: str,
    intent: str,
    plan: Sequence,
    sources: Sequence[Source],
    summary: Sequence[Dict],
    divergences: Sequence[Dict],
    coverage: Dict,
    rounds: Sequence[Dict],
    engine_stats: Dict,
    http_stats: Dict,
    elapsed: float,
    facet_coverage: Dict[str, int],
    gaps: Sequence[str],
    warnings: Sequence[str] = (),
) -> Dict:
    """组装完整证据包 —— 这是 deep search 的"事实层"，也是 agent 的输入。"""
    return {
        "query": query,
        "intent": intent,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(elapsed, 2),
        "plan": [sq.to_dict() if hasattr(sq, "to_dict") else sq for sq in plan],
        "rounds": list(rounds),
        "summary": list(summary),
        "divergences": list(divergences),
        "facet_coverage": dict(facet_coverage),
        "gaps": list(gaps),
        "coverage": coverage,
        "sources": [s.to_dict(with_content=False) for s in sources],
        "engine_stats": engine_stats,
        "http_stats": http_stats,
        "warnings": list(warnings),
    }


_CN_NUM = "一二三四五六七八九十"


def render_markdown(bundle: Dict, include_sources: bool = True,
                    include_summary: bool = True) -> str:
    """把证据包渲染成人可读的 Markdown 报告。"""
    q = bundle.get("query", "")
    lines: List[str] = []
    add = lines.append

    # 小节编号动态生成：某些节（如"分歧"）没有内容时会被跳过，
    # 硬编码"一二三四"会导致编号断档（二 → 四）。
    _sec = {"n": 0}

    def heading(title: str) -> None:
        _sec["n"] += 1
        idx = _CN_NUM[_sec["n"] - 1] if _sec["n"] <= len(_CN_NUM) else str(_sec["n"])
        add(f"\n## {idx}、{title}\n")

    add(f"# 深度搜索报告：{q}\n")

    cov = bundle.get("coverage", {}) or {}
    meta_bits = [
        f"用时 {_fmt_time(bundle.get('elapsed_seconds', 0))}",
        f"轮次 {len(bundle.get('rounds', []))}",
        f"来源 {cov.get('sources_with_content', 0)}/{cov.get('sources_total', 0)} 条有正文",
        f"独立域名 {cov.get('distinct_domains', 0)}",
        f"正文合计 {cov.get('total_chars', 0):,} 字",
    ]
    add("> " + " · ".join(meta_bits) + "\n")
    add(f"> 意图：`{bundle.get('intent', '')}`　生成于 {bundle.get('generated_at', '')}\n")

    # ---------------- 摘要
    if include_summary:
        summary = bundle.get("summary", [])
        heading("要点（抽取式，句末编号为来源）")
        if summary:
            for item in summary:
                idx = item.get("source_idx", 0)
                add(f"- {item.get('text', '')} [{idx}]")
        else:
            add("_未获得足够正文，无法生成要点。见下方来源列表。_")
        add("")

    # ---------------- 按面归纳
    heading("按主题面归纳")
    sources = bundle.get("sources", [])
    facet_map: Dict[str, List[Dict]] = defaultdict(list)
    for s in sources:
        for f in s.get("facets", []) or ["general"]:
            facet_map[f].append(s)

    if facet_map:
        for facet, items in sorted(facet_map.items(), key=lambda kv: -len(kv[1])):
            add(f"### {facet}")
            for s in items[:4]:
                snip = (s.get("quotes") or [s.get("snippet", "")])[0] or ""
                snip = re.sub(r"\s+", " ", snip)[:240]
                add(f"- **{s.get('title', '')}** — {snip} [{s.get('idx', 0)}]")
            add("")
    else:
        add("_暂无分面证据。_\n")

    # ---------------- 分歧
    divergences = bundle.get("divergences", [])
    if divergences:
        heading("证据分歧 / 需交叉验证")
        for d in divergences:
            terms = "、".join(d.get("shared_terms", [])[:5])
            add(f"- 围绕 **{terms}** 存在不同表述：")
            add(f"  - {d['a']['text']} [{d['a']['source_idx']}]")
            add(f"  - {d['b']['text']} [{d['b']['source_idx']}]")
        add("")

    # ---------------- 缺口
    gaps = bundle.get("gaps", [])
    facet_cov = bundle.get("facet_coverage", {}) or {}
    if gaps or facet_cov:
        heading("证据缺口（未覆盖 / 覆盖薄弱）")
        if gaps:
            for g in gaps:
                add(f"- {g}")
        weak = [f for f, c in facet_cov.items() if c == 0]
        if weak:
            add(f"- 以下主题面在本轮证据中**没有命中**：{'、'.join(weak)}")
        if not gaps and not weak:
            add("- 未发现明显缺口。")
        add("")

    # ---------------- 来源
    if include_sources:
        heading("来源清单")
        for s in sources:
            idx = s.get("idx", 0)
            title = s.get("title") or s.get("url", "")
            site = s.get("site", "")
            date = s.get("published", "")
            engines = "、".join(s.get("engines", [])[:3])
            flags = []
            if s.get("status") != "ok":
                flags.append(s.get("status", ""))
            tail = " ".join(x for x in [f"`{site}`" if site else "", date,
                                        f"引擎:{engines}" if engines else "",
                                        " ".join(flags)] if x)
            add(f"{idx}. [{title}]({s.get('url', '')}) — {tail}")
        add("")

    # ---------------- 诊断
    es = bundle.get("engine_stats", {}) or {}
    if es:
        heading("执行诊断")
        ok = [k for k, v in es.items() if v.get("hits", 0) > 0]
        bad = [k for k, v in es.items() if v.get("hits", 0) == 0]
        add(f"- 有效引擎 {len(ok)} 个：{'、'.join(ok) if ok else '无'}")
        if bad:
            add(f"- 无结果引擎 {len(bad)} 个：{'、'.join(bad)}")
        hs = bundle.get("http_stats", {}) or {}
        if hs:
            add(f"- HTTP：请求 {hs.get('requests', 0)}，缓存命中 {hs.get('cache_hits', 0)}，"
                f"错误 {hs.get('errors', 0)}，robots 拒绝 {hs.get('blocked_by_robots', 0)}")
        add("")

    warnings = bundle.get("warnings", [])
    if warnings:
        heading("注意事项")
        for w in warnings:
            add(f"- {w}")
        add("")

    add("\n---\n")
    add("_本报告由 deep-search 生成：所有要点均为**抽取式**（原句摘录），"
        "编号对应来源清单。要点不代表结论，请结合出处核验。_")

    return "\n".join(lines)


def render_brief(bundle: Dict) -> str:
    """极简输出：只要要点 + 来源。适合塞进上下文。"""
    lines: List[str] = [f"# {bundle.get('query', '')}"]
    cov = bundle.get("coverage", {}) or {}
    lines.append(f"_{cov.get('sources_with_content', 0)} 条来源 · "
                 f"{cov.get('total_chars', 0):,} 字正文 · "
                 f"{_fmt_time(bundle.get('elapsed_seconds', 0))}_\n")
    for item in bundle.get("summary", []):
        lines.append(f"- {item.get('text', '')} [{item.get('source_idx', 0)}]")
    lines.append("\n## 来源")
    for s in bundle.get("sources", []):
        lines.append(f"{s.get('idx', 0)}. [{s.get('title', '')}]({s.get('url', '')})")
    return "\n".join(lines)


def render_json(bundle: Dict) -> str:
    return json.dumps(bundle, ensure_ascii=False, indent=2)


def write_evidence_files(bundle: Dict, sources: Sequence[Source], out_dir) -> List[str]:
    """
    把原始证据落盘：每个来源一份正文，便于人工回溯与后续复用。
    返回写出的文件路径列表。
    """
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: List[str] = []

    (out / "bundle.json").write_text(render_json(bundle), "utf-8")
    written.append(str(out / "bundle.json"))
    (out / "report.md").write_text(render_markdown(bundle), "utf-8")
    written.append(str(out / "report.md"))

    docs = out / "sources"
    docs.mkdir(exist_ok=True)
    for s in sources:
        if not s.has_content:
            continue
        safe = re.sub(r"[^\w\-.]+", "_", (s.title or s.domain or "source"))[:60]
        p = docs / f"{s.idx:02d}_{safe}.md"
        header = (f"# {s.title}\n\n"
                  f"- URL: {s.url}\n- 站点: {s.site or s.domain}\n"
                  f"- 日期: {s.published}\n- 引擎: {', '.join(sorted(set(s.engines)))}\n"
                  f"- 相关度: {s.relevance:.3f} / 综合分: {s.score:.3f}\n"
                  f"- 字数: {s.char_count}\n\n---\n\n")
        p.write_text(header + s.content, "utf-8")
        written.append(str(p))

    return written
