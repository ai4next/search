#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_deep_search.py —— 离线自检（不联网）

覆盖深度搜索的每个环节，用内联 fixture 而不是真实网络：
    dom      解析与 CSS 子集选择器
    extract  正文抽取与元数据
    text     分词、指纹、URL 规范化、术语发现
    engines  跳转解包、垃圾链接过滤、通用 SERP 提取
    rank     三层去重、BM25 排序、抽取式摘要
    planner  主题抽取、首轮分解、缺口追问
    report   证据包与 Markdown 渲染（要点必须带出处）

运行：
    python3 test/test_deep_search.py
    python3 scripts/search.py --test
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from ds import dom, engines as E, extract as X, rank as R, report as RP  # noqa: E402
from ds import text as T  # noqa: E402
from ds.planner import Planner, extract_subject  # noqa: E402

PASS, FAIL = 0, 0
FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  ✗ {name}  {detail}")


def section(title: str) -> None:
    print(f"\n[{title}]")


# ---------------------------------------------------------------- fixtures

SERP_HTML = """
<html><head><title>测试搜索</title></head><body>
<div id="nav"><a href="https://example.com/login">登录</a>
  <a href="https://example.com/feedback">意见反馈</a></div>
<ol id="b_results">
  <li class="b_algo">
    <h2><a href="https://www.nature.com/articles/nature14539">Deep learning - Nature</a></h2>
    <div class="b_caption"><p>2015年5月27日 · Deep learning allows computational models
    that are composed of multiple processing layers to learn representations.</p></div>
  </li>
  <li class="b_algo">
    <h2><a href="https://www.bing.com/ck/a?!&&p=1&u=a1aHR0cHM6Ly9leGFtcGxlLm9yZy9hcnRpY2xl">Example Article</a></h2>
    <div class="b_caption"><p>An example article snippet with enough text to be useful here.</p></div>
  </li>
</ol>
<div class="footer"><a href="https://example.com/privacy">Privacy Policy</a></div>
</body></html>
"""

ARTICLE_HTML = """
<html lang="zh-CN"><head>
<title>固态电池产业化进展 - 某站</title>
<meta property="og:title" content="固态电池产业化进展">
<meta property="article:published_time" content="2026-05-20T08:00:00+08:00">
<meta name="author" content="张三">
</head><body>
<nav><a href="/">首页</a><a href="/about">关于</a></nav>
<div class="sidebar"><a href="/ad1">广告位招租</a></div>
<article>
  <h1>固态电池产业化进展</h1>
  <p>随着今年多条全固态电池中试线集中投产，固态电池产业化进程有望进一步提速。
     多家企业公布了量产时间表，产业链上下游都在加速布局。</p>
  <p>固态电解质出货量不断提升，2025年全球固态电池电解质出货量为0.41万吨，
     同比增长138.5%。硫化物路线与氧化物路线的分歧依然存在。</p>
  <p>整体而言，固态电池产业化仍有大量技术、工艺、成本难题待突破，
     尤其是界面工程与材料成本问题。</p>
</article>
<footer>版权所有 © 2026</footer>
</body></html>
"""


# ---------------------------------------------------------------- tests

def test_dom() -> None:
    section("dom 解析与选择器")
    root = dom.parse(SERP_HTML)
    check("解析出 li.b_algo", len(dom.select(root, "li.b_algo")) == 2,
          f"got {len(dom.select(root, 'li.b_algo'))}")
    check("后代选择器 h2 a", len(dom.select(root, "h2 a")) == 2)
    check("子代选择器 ol > li", len(dom.select(root, "ol#b_results > li")) == 2)
    check("#id 选择器", dom.select_one(root, "#nav") is not None)
    check("属性选择器 [href^=https]",
          len(dom.select(root, "a[href^='https://www.nature']")) == 1)
    check("分组选择器", len(dom.select(root, ".b_algo, .footer")) == 3)
    check("get_text 取文本", "Deep learning" in dom.select_one(root, "h2 a").get_text())
    check("script 内容不进文本",
          "var" not in dom.parse("<div>a<script>var x=1</script>b</div>").get_text())
    # 畸形 HTML 不应崩
    bad = dom.parse("<div><p>未闭合<span>x</div><li>y")
    check("畸形 HTML 不抛异常", bad is not None and len(dom.select(bad, "p")) >= 1)


def test_text() -> None:
    section("text 分词 / 指纹 / URL")
    toks = T.tokenize("深度学习是机器学习的分支 deep learning")
    check("中文 bigram", "深度" in toks and "学习" in toks, str(toks[:6]))
    check("英文小写词", "learning" in toks)
    check("停用词被过滤", "是" not in T.content_tokens("这是是是"))
    check("URL 去跟踪参数",
          T.normalize_url("https://www.example.com/a/b/?utm_source=x&id=2#f")
          == "https://example.com/a/b?id=2",
          T.normalize_url("https://www.example.com/a/b/?utm_source=x&id=2#f"))
    check("www 前缀归一",
          T.normalize_url("http://www.a.com/x") == T.normalize_url("https://a.com/x"))
    h1 = T.simhash(T.content_tokens("深度学习是机器学习的一个分支" * 6))
    h2 = T.simhash(T.content_tokens("深度学习是机器学习的一个分支" * 6))
    h3 = T.simhash(T.content_tokens("今天天气很好适合出门散步" * 6))
    check("相同内容指纹相同", h1 == h2)
    check("不同内容指纹不同", not T.near_duplicate(h1, h3))
    check("jaccard 自比=1", T.jaccard({"a", "b"}, {"a", "b"}) == 1.0)


def test_term_discovery() -> None:
    section("text 中文术语发现（邻接熵）")
    doc = ("""
    随着今年多条全固态电池中试线集中投产，固态电池产业化进程有望进一步提速。
    固态电解质出货量不断提升，全球固态电池电解质出货量为0.41万吨。
    硫化物固态电池产业化浪潮涌动，电解质材料迭代是核心瓶颈。
    全固态电池产业化尚未攻克瓶颈，硫化物电解质路线与氧化物路线存在分歧。
    固态电池的成本仍高于液态锂电池，电解质材料成本占比超过三成。
    """) * 3
    terms = T.salient_terms([doc], exclude=["固态电池产业化"], top_n=8)
    check("发现「固态电池」", any("固态电池" == t for t in terms), str(terms))
    check("发现「电解质」", any("电解质" == t for t in terms), str(terms))
    check("排除查询词碎片", not any(t == "电池" for t in terms), str(terms))
    # 跨词边界的碎片应被邻接熵剔除
    check("剔除跨词碎片「态电池产」", "态电池产" not in terms, str(terms))
    en = T.salient_terms(["solid state battery electrolyte commercialization"] * 3, top_n=6)
    check("英文术语", "electrolyte" in en, str(en))


def test_extract() -> None:
    section("extract 正文抽取")
    art = X.extract_article(ARTICLE_HTML, "https://example.com/a")
    check("标题", art.title == "固态电池产业化进展", art.title)
    check("日期规范化", art.published == "2026-05-20", art.published)
    check("作者", art.author == "张三", art.author)
    check("语言", art.lang.startswith("zh"), art.lang)
    check("正文够长", art.char_count >= 150, str(art.char_count))
    check("正文含关键内容", "固态电解质出货量" in art.text)
    check("剔除导航", "广告位招租" not in art.text)
    check("剔除页脚", "版权所有" not in art.text)
    check("段落切分", len(art.paragraphs) >= 2, str(len(art.paragraphs)))
    check("extractor 标记", art.extractor in ("semantic", "readability-lite", "body-fallback"),
          art.extractor)
    # 片段提取
    snip = X.make_snippet(art.text, ["电解质", "出货量"], width=120)
    check("make_snippet 命中查询词", "电解质" in snip or "出货量" in snip, snip)
    # 空输入不崩
    check("空 HTML 不崩", X.extract_article("").char_count == 0)


def test_engines() -> None:
    section("engines 解析与过滤")
    # Bing 跳转解包
    real = E.unwrap_redirect(
        "https://www.bing.com/ck/a?!&&p=1&u=a1aHR0cHM6Ly9leGFtcGxlLm9yZy9hcnRpY2xl")
    check("解 Bing ck/a 跳转", real == "https://example.org/article", real)
    check("非跳转 URL 原样返回",
          E.unwrap_redirect("https://a.com/x") == "https://a.com/x")
    # 链接过滤
    check("拒绝 javascript:", not E.is_probably_content_url("javascript:void(0)"))
    check("拒绝图片", not E.is_probably_content_url("https://a.com/x.png"))
    check("拒绝登录页", not E.is_probably_content_url("https://a.com/login"))
    check("拒绝推广跳转", not E.is_probably_content_url("https://go.microsoft.com/fwlink/?linkid=1"))
    check("接受正文链接", E.is_probably_content_url("https://a.com/blog/post-1"))
    # 通用 SERP 提取必须过滤导航
    root = dom.parse(SERP_HTML)
    bing = E.ENGINES["bing_cn"]
    hits = E.generic_serp(root, bing, "deep learning", 10, ("bing.com",))
    titles = [h.title for h in hits]
    check("通用 SERP 提取到结果", len(hits) >= 1, str(titles))
    check("过滤导航锚文本「登录」", "登录" not in titles, str(titles))
    check("过滤「意见反馈」", "意见反馈" not in titles, str(titles))
    check("过滤「Privacy Policy」", "Privacy Policy" not in titles, str(titles))
    # 意图路由
    check("路由 advanced", "advanced" == E.detect_intent("site:gov.cn 数据"))
    check("路由 tech", E.detect_intent("python asyncio 报错") == "tech")
    check("路由 academic", E.detect_intent("transformer 论文") == "academic")
    check("路由返回引擎", len(E.route("python 报错")) >= 3)
    check("引擎注册表非空", len(E.ENGINES) >= 12, str(len(E.ENGINES)))


def test_rank() -> None:
    section("rank 去重 / 排序 / 摘要")
    # URL 去重
    a = R.Source(url="https://a.com/x?utm_source=1", title="T1", content="c" * 600,
                 status="ok", engines=["bing_cn"])
    b = R.Source(url="https://a.com/x", title="T1 dup", content="c" * 600,
                 status="ok", engines=["baidu"])
    kept, merged = R.dedupe([a, b])
    check("URL 去重", len(kept) == 1 and len(merged) == 1, f"{len(kept)}/{len(merged)}")
    check("去重后合并引擎", len(set(kept[0].engines)) == 2, str(kept[0].engines))

    # 标题相似去重（需要正文达到阈值才会启用标题合并）
    c = R.Source(url="https://b.com/1", title="深度学习入门教程完整指南", status="ok",
                 content="深度学习基础内容。" * 60)
    d = R.Source(url="https://c.com/2", title="深度学习入门教程完整指南！", status="ok",
                 content="深度学习基础内容。" * 60)
    kept2, merged2 = R.dedupe([c, d])
    check("标题相似去重", len(kept2) == 1, str([s.title for s in kept2]))

    # 无正文时不应因标题相近而丢链接（快速搜索场景）
    e1 = R.Source(url="https://f.com/1", title="Python Asyncio 完全指南", status="ok",
                  snippet="a")
    e2 = R.Source(url="https://g.com/2", title="Python Asyncio 完全指南", status="ok",
                  snippet="b")
    kept_no_content, _ = R.dedupe([e1, e2])
    check("无正文时保留不同 URL", len(kept_no_content) == 2, str(len(kept_no_content)))

    # 指纹去重（不同 URL 同内容）
    body = "深度学习是机器学习的一个重要分支，通过多层神经网络学习表征。" * 10
    e1 = R.Source(url="https://d.com/1", title="完全不同的标题甲", content=body, status="ok")
    e2 = R.Source(url="https://e.com/2", title="完全不同的标题甲", content=body, status="ok")
    kept3, _ = R.dedupe([e1, e2])
    check("正文指纹去重", len(kept3) == 1, str(len(kept3)))

    # 权威度
    check("gov 权威度高", R.authority_of("gxt.fj.gov.cn") > 0.9)
    check("arxiv 权威度高", R.authority_of("arxiv.org") > 0.9)
    check("内容农场低", R.authority_of("book118.com") < 0.4)
    check("未知域名中性", 0.4 <= R.authority_of("some-random-blog.xyz") <= 0.7)

    # 排序：相关性 + 权威度
    s1 = R.Source(url="https://arxiv.org/abs/1", title="深度学习综述", status="ok",
                  content="深度学习综述 " * 40, published="2026")
    s2 = R.Source(url="https://random-blog.xyz/p", title="深度学习", status="ok",
                  content="深度学习 " * 5, published="2010")
    R.score_sources([s1, s2], "深度学习综述")
    check("权威+新鲜排前", s1.score > s2.score, f"{s1.score:.3f} vs {s2.score:.3f}")

    # 时效性
    check("新文章时效高", R.freshness_of("2026") > R.freshness_of("2015"))
    check("无日期中性", abs(R.freshness_of("") - 0.5) < 1e-6)

    # 抽取式摘要 + 引证
    src = R.Source(
        url="https://a.com/1", title="固态电池", status="ok", idx=1,
        content=("固态电池产业化进程有望进一步提速，多条中试线集中投产，装车试验工作有序推进。\n"
                 "固态电解质出货量同比增长138.5%，达到0.41万吨，硫化物路线受到产业界高度关注。\n"
                 "整体而言，固态电池仍有大量技术、工艺、成本难题待突破，界面工程是核心瓶颈。\n"
                 "多家固态电池产业链企业获得新一轮融资，资本对该技术路线保持较高关注度。\n"
                 "丰田计划2027年量产全固态电池，宁德时代则聚焦凝聚态电池路线，双方选择不同。\n"
                 "今天天气不错适合出门散步，这一段与本研究主题无关，应当排在后面。"),
    )
    check("测试来源达到正文阈值", src.has_content, f"char_count={src.char_count}")
    summ = R.extractive_summary([src], "固态电池 产业化", max_sentences=3)
    check("摘要非空", len(summ) >= 1, str(len(summ)))
    check("摘要带来源编号", all("source_idx" in s for s in summ))
    check("摘要句来自原文", all(s["text"] in src.content for s in summ))
    quotes = R.select_quotes(src, T.content_tokens("固态电池 电解质"), n=2)
    check("引证片段非空", len(quotes) >= 1, str(quotes[:1]))

    # 同一句话在正文里出现两次（网页摘要块常见），摘要里不能重复
    dup_src = R.Source(
        url="https://a.com/dup", title="重复测试", status="ok", idx=9,
        content=("固态电池产业化进程驶入快车道，背后是政策与需求的双重驱动。\n"
                 "固态电解质出货量同比增长138.5%，达到0.41万吨。\n"
                 "固态电池产业化进程驶入快车道，背后是政策与需求的双重驱动。\n"
                 "多家企业获得新一轮融资，资本关注度较高。\n"
                 "全固态电池仍需突破工程化、成本、良率等多重挑战。"),
    )
    dup_summary = R.extractive_summary([dup_src], "固态电池", max_sentences=8)
    texts = [s["text"] for s in dup_summary]
    check("摘要不重复同一句", len(texts) == len(set(texts)), str(texts))

    # 覆盖度
    cov = R.coverage_report([src], "固态电池")
    check("覆盖度统计有正文数", cov["sources_with_content"] == 1, str(cov))

    # 分歧检测：只报"围绕同一特征词的对立表述"，不能把共享话题词当分歧
    shared_topic = [
        {"text": "随着多条全固态电池中试线集中投产，固态电池产业化进程有望进一步提速。",
         "source_idx": 1, "opposition": False},
        {"text": "固态电池产业化进程驶入快车道，背后是政策与需求的双重驱动。",
         "source_idx": 2, "opposition": False},
        {"text": "一边是产业化蓝图，一边是行业内外的密集质疑，这场争议是技术野心与产业现实的碰撞。",
         "source_idx": 5, "opposition": True},
    ]
    check("共享话题词不算分歧", len(R.find_divergences(shared_topic)) == 0,
          str(R.find_divergences(shared_topic)))

    real_split = [
        {"text": "Donut Lab 宣称其全固态电池已具备量产条件，能量密度达到400Wh/kg。",
         "source_idx": 1, "opposition": False},
        {"text": "然而多位专家质疑 Donut Lab 的量产宣言，认为400Wh/kg全固态电池尚不可行。",
         "source_idx": 2, "opposition": True},
    ]
    check("真实分歧能被识别", len(R.find_divergences(real_split)) >= 1)


def test_planner() -> None:
    section("planner 分解与追问")
    check("主题抽取去掉疑问词", extract_subject("什么是固态电池？") == "固态电池",
          extract_subject("什么是固态电池？"))
    check("主题抽取去掉前缀", extract_subject("请问如何学习深度学习") .startswith("学习深度学习")
          or "深度学习" in extract_subject("请问如何学习深度学习"),
          extract_subject("请问如何学习深度学习"))
    check("英文主题", "vector database" in extract_subject("what is a vector database").lower())

    pl = Planner(breadth=5)
    subs = pl.plan("什么是固态电池", "knowledge")
    check("首轮含原始查询", subs and subs[0].facet == "original", str([s.facet for s in subs]))
    check("首轮多条子查询", len(subs) >= 2, str(len(subs)))
    check("子查询去重", len({s.text for s in subs}) == len(subs))

    pl2 = Planner(breadth=5)
    adv = pl2.plan("site:gov.cn 数据安全法", "advanced")
    check("高级语法不分解", len(adv) == 1, str([s.text for s in adv]))

    pl3 = Planner(breadth=4)
    pl3.plan("固态电池产业化", "news")
    asked_before = set(pl3.asked)
    cov = {"definition": 0, "mechanism": 2, "status": 3, "comparison": 0,
           "application": 0, "data": 0, "risk": 0, "howto": 0, "expert": 0, "counter": 0}
    fu = pl3.follow_up("固态电池产业化", 2, cov,
                       ["固态电池产业化中，电解质材料是关键瓶颈，硫化物路线受关注。" * 5],
                       max_n=3)
    check("追问非空", len(fu) >= 1, str([s.text for s in fu]))
    check("追问不与首轮重复", all(s.text not in asked_before for s in fu),
          str([s.text for s in fu]))
    check("追问带轮次", all(s.round == 2 for s in fu))

    fc = Planner.facet_coverage(["本文介绍定义与原理，以及应用场景和风险。"])
    check("面覆盖检测到定义", fc.get("definition", 0) >= 1, str(fc))
    check("面覆盖未覆盖数据", fc.get("data", 0) == 0, str(fc))


def test_report() -> None:
    section("report 渲染")
    src = R.Source(url="https://a.com/1", title="测试来源", status="ok", idx=1,
                   content="固态电池产业化进程提速。" * 10, site="a.com",
                   published="2026", engines=["bing_cn"])
    src.score = 0.8
    src.relevance = 0.9
    summ = [{"text": "固态电池产业化进程提速。", "source_idx": 1,
             "source_title": "测试来源", "source_url": "https://a.com/1",
             "site": "a.com", "score": 1.0, "opposition": False}]
    bundle = RP.build_bundle(
        query="固态电池", intent="news", plan=[], sources=[src], summary=summ,
        divergences=[], coverage=R.coverage_report([src], "固态电池"), rounds=[],
        engine_stats={"bing_cn": {"hits": 3}}, http_stats={"requests": 5},
        elapsed=1.5, facet_coverage={"status": 2}, gaps=["反例与争议"],
        warnings=["测试警告"],
    )
    md = RP.render_markdown(bundle)
    check("报告含标题", "深度搜索报告" in md)
    check("报告含出处编号", "[1]" in md)
    check("报告含来源清单", "来源清单" in md and "https://a.com/1" in md)
    check("报告含缺口", "反例与争议" in md)
    check("报告含诊断", "执行诊断" in md)
    check("报告含警告", "测试警告" in md)
    js = RP.render_json(bundle)
    check("JSON 可解析", '"query"' in js and "固态电池" in js)
    brief = RP.render_brief(bundle)
    check("brief 更短", len(brief) < len(md), f"{len(brief)} vs {len(md)}")
    check("brief 保留出处", "[1]" in brief)


def run_offline_tests() -> int:
    print("=" * 66)
    print("deep-search 离线自检")
    print("=" * 66)
    for fn in (test_dom, test_text, test_term_discovery, test_extract,
               test_engines, test_rank, test_planner, test_report):
        try:
            fn()
        except Exception as e:
            global FAIL
            FAIL += 1
            FAILURES.append(f"{fn.__name__} 抛异常: {e!r}")
            print(f"  ✗ {fn.__name__} 抛异常: {e!r}")
    print("\n" + "=" * 66)
    print(f"通过 {PASS} · 失败 {FAIL}")
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run_offline_tests())
