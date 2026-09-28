#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
search.py —— 深度搜索

    规划 → 多引擎检索 → 挑 URL → 抓正文 → 打分 → 找缺口 → 追问 → 证据报告

产出的不是链接列表，而是**每条要点都带 [n] 出处的证据报告**。
每一句实质内容都能回溯到来源 —— 这是它和"看起来对的话"的区别。

零第三方依赖（只用 Python 标准库），无需 API Key。

用法：

    # 默认：2 轮、抓正文、输出 Markdown 报告
    python scripts/search.py "固态电池产业化进展"

    # 更快：只搜一轮且不抓正文（只拿链接和摘要）
    python scripts/search.py "向量数据库" -r 1 --no-fetch -f brief

    # 更深：3 轮、每引擎多取、保留更多证据
    python scripts/search.py "Transformer 架构的局限" -r 3 -n 20 -b 6

    # 落盘全部证据（bundle.json + report.md + sources/*.md）
    python scripts/search.py "量子计算现状" -o ./out/quantum

    # 用外部（agent/LLM）规划好的子查询 —— 对复杂问题效果显著更好
    python scripts/search.py "如何选型向量数据库" \
        -q "向量数据库 对比 HNSW IVF PQ" "向量数据库 成本 运维 实践"

    # 指定/排除引擎
    python scripts/search.py "python asyncio" -e github,stackexchange,bing_intl

    # 工具
    python scripts/search.py --list-engines
    python scripts/search.py --test                 # 离线自检（92 项，不联网）

成本旋钮（从便宜到贵）：
    -r 1 --no-fetch     只检索不抓正文，最快
    -r 1                抓正文但不追问
    -r 2（默认）          抓正文 + 一轮缺口追问
    -r 3 -k 20 -n 20    深挖，最慢

配置文件：技能根目录 config.yaml 的 `deep_search:` 段。命令行参数优先。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ds import engines as E           # noqa: E402
from ds import report as RP           # noqa: E402
from ds.pipeline import DeepConfig, DeepSearch  # noqa: E402

if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = str(SKILL_ROOT / ".cache")


def build_config(args) -> DeepConfig:
    """CLI 参数优先于 config.yaml。"""
    cfg_path = Path(args.config) if args.config else (SKILL_ROOT / "config.yaml")
    overrides = {
        "max_rounds": args.max_rounds,
        "breadth": args.breadth,
        "follow_up": args.follow_up,
        "per_engine": args.per_engine,
        "fetch_top_k": args.fetch_top_k,
        "max_sources": args.max_sources,
        "concurrency": args.concurrency,
        "timeout": args.timeout,
        "round_timeout": args.round_timeout,
        "summary_sentences": args.summary_sentences,
        "intent": args.intent,
        "cache_dir": args.cache_dir or DEFAULT_CACHE,
    }
    if args.no_fetch:
        overrides["fetch_pages"] = False
    if args.no_cache:
        overrides["cache"] = False
    if args.no_robots:
        overrides["respect_robots"] = False
    if args.engines:
        overrides["engines"] = [s.strip() for s in args.engines.split(",") if s.strip()]
    if args.exclude_engines:
        overrides["exclude_engines"] = [
            s.strip() for s in args.exclude_engines.split(",") if s.strip()
        ]
    if args.queries:
        overrides["queries"] = args.queries
    if args.verbose:
        overrides["verbose"] = True
    return DeepConfig.from_file(cfg_path, **overrides)


def cmd_list_engines() -> int:
    print(f"可用引擎（共 {len(E.ENGINES)} 个）\n")
    print(f"{'ID':<16}{'名称':<18}{'类别':<12}{'权重':<7}{'超时':<7}层级")
    print("-" * 70)
    for eid in E.all_engine_ids():
        e = E.ENGINES[eid]
        print(f"{eid:<16}{e.name:<18}{e.category:<12}{e.weight:<7}{e.timeout:<7}"
              f"{'可靠' if e.tier == 1 else '尽力而为'}")
    print("\n意图路由：")
    for name, cfg in E.INTENTS.items():
        print(f"  {name:<11}{cfg['desc']:<28}-> {', '.join(cfg['engines'])}")
    print("\n说明：检索请求不套用 robots.txt（等价于用户搜索）；抓取结果页正文时严格遵守。")
    return 0


def cmd_test() -> int:
    import importlib.util
    tpath = SKILL_ROOT / "test" / "test_deep_search.py"
    if not tpath.exists():
        print(f"未找到自检脚本：{tpath}", file=sys.stderr)
        return 1
    spec = importlib.util.spec_from_file_location("test_deep_search", tpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.run_offline_tests()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="search",
        description="深度搜索：多轮规划 → 多引擎检索 → 抓正文 → 交叉验证 → 带出处的证据报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("query", nargs="?", help="搜索问题（自然语言即可）")

    g = p.add_argument_group("规模（越大越深，也越慢）")
    g.add_argument("-r", "--max-rounds", type=int, help="最大轮次（默认 2）")
    g.add_argument("-b", "--breadth", type=int, help="首轮子查询数（默认 5）")
    g.add_argument("-n", "--max-sources", type=int, help="保留证据条数（默认 14）")
    g.add_argument("-k", "--fetch-top-k", type=int, help="每轮最多抓取页面数（默认 14）")
    g.add_argument("--follow-up", type=int, help="每轮追问条数上限（默认 4）")
    g.add_argument("--per-engine", type=int, help="每引擎每查询取多少条（默认 8）")
    g.add_argument("--summary-sentences", type=int, help="摘要句数上限（默认 12）")

    g2 = p.add_argument_group("引擎与规划")
    g2.add_argument("-i", "--intent", choices=list(E.INTENTS.keys()), help="强制搜索意图")
    g2.add_argument("-e", "--engines", help="追加引擎，逗号分隔")
    g2.add_argument("-x", "--exclude-engines", help="排除引擎，逗号分隔")
    g2.add_argument("-q", "--queries", nargs="+", help="外部规划的子查询（覆盖自动分解）")

    g3 = p.add_argument_group("网络")
    g3.add_argument("--timeout", type=float, help="单请求超时秒数（默认 12）")
    g3.add_argument("--round-timeout", type=float, help="单轮硬超时秒数（默认 35）")
    g3.add_argument("--concurrency", type=int, help="并发度（默认 6）")
    g3.add_argument("--no-fetch", action="store_true", help="只检索不抓正文（最快）")
    g3.add_argument("--no-cache", action="store_true", help="禁用磁盘缓存")
    g3.add_argument("--no-robots", action="store_true", help="抓正文时忽略 robots.txt")
    g3.add_argument("--cache-dir", help="缓存目录（默认 <技能根>/.cache）")

    g4 = p.add_argument_group("输出")
    g4.add_argument("-f", "--format", choices=["markdown", "json", "brief"],
                    default=None, help="输出格式（默认 markdown；json 适合 agent 加工）")
    g4.add_argument("-j", "--json", action="store_true", help="等价于 -f json")
    g4.add_argument("-o", "--out", help="证据目录：bundle.json + report.md + sources/*.md")
    g4.add_argument("-v", "--verbose", action="store_true", help="打印执行过程")
    g4.add_argument("--quiet", action="store_true", help="只输出结果")
    g4.add_argument("--config", help="配置文件路径（默认 <技能根>/config.yaml）")
    g4.add_argument("--show-config", action="store_true", help="打印生效配置后退出")

    g5 = p.add_argument_group("工具")
    g5.add_argument("--list-engines", action="store_true", help="列出全部引擎后退出")
    g5.add_argument("--test", action="store_true", help="跑一遍离线自检后退出")

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_engines:
        return cmd_list_engines()
    if args.test:
        return cmd_test()
    if not args.query:
        build_parser().print_help()
        return 2

    cfg = build_config(args)

    if args.show_config:
        print(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if not args.quiet:
        print("=" * 68)
        print(f"深度搜索：{args.query}")
        print(f"轮次 {cfg.max_rounds} · 广度 {cfg.breadth} · 抓取 {cfg.fetch_top_k}/轮 "
              f"· 证据上限 {cfg.max_sources} · 并发 {cfg.concurrency}"
              f"{'' if cfg.fetch_pages else ' · 不抓正文'}")
        print("=" * 68)

    engine = DeepSearch(cfg)
    try:
        bundle = engine.search(args.query)
    except KeyboardInterrupt:
        print("\n[中断] 用户取消", file=sys.stderr)
        return 130

    fmt = args.format or ("json" if args.json else "markdown")
    if fmt == "json":
        text = RP.render_json(bundle)
    elif fmt == "brief":
        text = RP.render_brief(bundle)
    else:
        text = RP.render_markdown(bundle)

    if args.out:
        out_dir = Path(args.out)
        written = RP.write_evidence_files(bundle, engine._final_sources, out_dir)
        if not args.quiet:
            print(f"\n[已写出] {len(written)} 个文件到 {out_dir}")
            for w in written[:6]:
                print(f"    {w}")
            if len(written) > 6:
                print(f"    … 其余 {len(written) - 6} 个")
    else:
        print(text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
