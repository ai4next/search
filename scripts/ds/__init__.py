# -*- coding: utf-8 -*-
"""
ds —— deep search 核心包

零第三方依赖的深度搜索内核：
    net       HTTP（编码/压缩/限速/缓存/robots）
    dom       迷你 DOM + CSS 子集选择器
    extract   正文抽取（readability 精简版）
    engines   引擎注册表（API 适配器 + SERP 解析 + 意图路由）
    planner   查询规划（子查询分解 + 缺口追问）
    rank      去重、相关性排序、抽取式摘要
    report    证据包与 Markdown 报告
    pipeline  多轮编排
"""

from . import dom, engines, extract, net  # noqa: F401

__version__ = "3.0.0"
__all__ = ["net", "dom", "extract", "engines"]
