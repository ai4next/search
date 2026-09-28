# -*- coding: utf-8 -*-
"""
web复合搜索 / deep-search —— 深度搜索技能

唯一入口：
    scripts/search.py "查询词"        深度搜索（多轮 + 抓正文 + 带出处的证据报告）

核心内核在 ds 包中，零第三方依赖（仅标准库）。
"""

from .search import main

__version__ = "4.0.0"
__all__ = ["main"]
