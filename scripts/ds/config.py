# -*- coding: utf-8 -*-
"""
ds.config —— 配置文件读取（YAML，且不强依赖 PyYAML）

优先用 PyYAML（如果环境里有）；没有就用内置的极简解析器。
之所以不直接依赖 PyYAML：本技能的承诺是"零第三方依赖也能跑"，
一个配置文件不该成为安装门槛。

内置解析器支持本技能用到的 YAML 子集：
    key: value
    section:
      nested_key: value
    list_key:
      - item
注释（#）、引号、数字/布尔字面量都支持。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def _coerce(raw: str) -> Any:
    """把 YAML 标量转成 Python 值。"""
    s = raw.strip()
    if not s:
        return ""
    # 去引号
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~"):
        return None
    # 数字
    try:
        if re.fullmatch(r"[-+]?\d+", s):
            return int(s)
        if re.fullmatch(r"[-+]?\d*\.\d+([eE][-+]?\d+)?", s):
            return float(s)
    except ValueError:
        pass
    # 行内列表 [a, b, c]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_coerce(x) for x in inner.split(",")]
    return s


def _strip_comment(line: str) -> str:
    """去掉行尾注释，但不动引号里的 #。"""
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out)


def parse_simple_yaml(text: str) -> Dict[str, Any]:
    """
    极简 YAML 解析器：支持两级嵌套与列表。
    只覆盖配置文件这类结构，不追求 YAML 规范完整性。
    """
    root: Dict[str, Any] = {}
    # 栈：(缩进, 容器)
    stack: List[tuple] = [(-1, root)]
    pending_list: Optional[tuple] = None  # (缩进, list)

    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()

        # 列表项
        if content.startswith("- "):
            item = _coerce(content[2:])
            if pending_list is not None:
                pending_list[1].append(item)
            continue

        if ":" not in content:
            continue

        key, _, val = content.partition(":")
        key = key.strip().strip("'\"")
        val = val.strip()

        # 回退到合适的层级
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        container = stack[-1][1]

        if val == "":
            # 可能是嵌套字典，也可能是列表（由后续 "- " 决定）
            child: Dict[str, Any] = {}
            container[key] = child
            stack.append((indent, child))
            pending_list = (indent, [])
            # 占位：若下一行是列表项，则替换成 list
            child["__pending_list__"] = pending_list
        else:
            container[key] = _coerce(val)
            pending_list = None

    # 把 "只有列表项" 的占位字典替换成真正的列表
    def _fix(node: Any) -> Any:
        if isinstance(node, dict):
            pend = node.pop("__pending_list__", None)
            fixed = {k: _fix(v) for k, v in node.items()}
            if pend is not None and pend[1] and not fixed:
                return list(pend[1])
            return fixed
        if isinstance(node, list):
            return [_fix(x) for x in node]
        return node

    return _fix(root)


def load_config(path: Optional[Path | str]) -> Dict[str, Any]:
    """读取配置文件；不存在或解析失败都返回空字典（不阻断搜索）。"""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        text = p.read_text("utf-8")
    except OSError:
        return {}

    # 优先真 YAML
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    # 退回内置解析器
    try:
        return parse_simple_yaml(text)
    except Exception:
        return {}


def default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "config.yaml"


def get(cfg: Dict[str, Any], dotted: str, default: Any = None) -> Any:
    """按 "a.b.c" 取值。"""
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur
