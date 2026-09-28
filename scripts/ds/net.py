# -*- coding: utf-8 -*-
"""
ds.net —— 零依赖 HTTP 层

深度搜索要抓几十上百个页面，网络层必须自己扛住三件事：
1. **编码**：中文站点大量使用 GBK/GB18030，猜错就整页乱码。
2. **压缩**：gzip/deflate 要自己解，urllib 不会代劳。
3. **礼貌**：按域限速 + robots.txt，否则很快被封。

只用标准库，不引入 requests/httpx —— 这样技能在任何 Python 3.8+ 环境都能跑。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    # 刻意不要 br：标准库解不了 brotli
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
    "Upgrade-Insecure-Requests": "1",
}

# 明显不是正文的二进制类型，抓回来也白搭
BINARY_TYPES = (
    "image/", "video/", "audio/", "application/pdf", "application/zip",
    "application/octet-stream", "application/x-gzip", "font/",
)


# ---------------------------------------------------------------- 解压 / 解码

def decompress(data: bytes, encoding: str) -> bytes:
    """按 Content-Encoding 解压；失败就原样返回，绝不抛异常。"""
    enc = (encoding or "").lower().strip()
    if not data or enc in ("", "identity"):
        return data
    try:
        if enc == "gzip" or enc == "x-gzip":
            return gzip.decompress(data)
        if enc == "deflate":
            # 有的服务器发裸 deflate，有的发 zlib 包装，两种都试
            try:
                return zlib.decompress(data)
            except zlib.error:
                return zlib.decompress(data, -zlib.MAX_WBITS)
        if enc == "br":
            try:
                import brotli  # type: ignore
                return brotli.decompress(data)
            except Exception:
                return data
    except Exception:
        return data
    return data


_META_CHARSET_RE = re.compile(
    br"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE
)
_META_HTTPEQUIV_RE = re.compile(
    br"""<meta[^>]+http-equiv\s*=\s*["']?content-type["']?[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""",
    re.IGNORECASE,
)


def sniff_charset(head: bytes) -> Optional[str]:
    """从 HTML 头部嗅探 charset 声明。"""
    m = _META_CHARSET_RE.search(head) or _META_HTTPEQUIV_RE.search(head)
    if m:
        try:
            return m.group(1).decode("ascii", "ignore")
        except Exception:
            return None
    return None


# 中文站点常见编码，按命中概率排序
_FALLBACK_CHARSETS = ("utf-8", "gb18030", "big5", "shift_jis", "euc-kr", "latin-1")


def decode_bytes(content: bytes, content_type: str = "") -> str:
    """
    把字节流解成文本。优先级：
    1. HTTP 头里的 charset
    2. HTML meta 声明
    3. 依次尝试常见编码（utf-8 严格模式先试，避免误判）
    """
    if not content:
        return ""

    declared = None
    if content_type:
        m = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_\-]+)", content_type, re.IGNORECASE)
        if m:
            declared = m.group(1)

    head = content[:4096]
    candidates: List[str] = []
    for c in (declared, sniff_charset(head)):
        if c and c.lower() not in ("none", "unknown"):
            candidates.append(c)

    # utf-8 严格解码成功率极高，优先当默认
    candidates.append("utf-8")
    for c in _FALLBACK_CHARSETS:
        if c not in candidates:
            candidates.append(c)

    for enc in candidates:
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # 兜底：带替换字符，至少不丢结构
    return content.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 响应对象

@dataclass
class Response:
    url: str
    status: int
    headers: Dict[str, str]
    content: bytes
    from_cache: bool = False
    error: Optional[str] = None

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 400

    @property
    def text(self) -> str:
        return decode_bytes(self.content, self.content_type)

    def json(self) -> Any:
        return json.loads(self.text)


# ---------------------------------------------------------------- 缓存

class DiskCache:
    """极简磁盘缓存：<root>/<sha1>.bin + <root>/<sha1>.json"""

    def __init__(self, root: Path, ttl: int = 900, enabled: bool = True):
        self.root = Path(root)
        self.ttl = ttl
        self.enabled = enabled
        if self.enabled:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.enabled = False

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    def get(self, url: str) -> Optional[Response]:
        if not self.enabled:
            return None
        k = self._key(url)
        meta_p, body_p = self.root / f"{k}.json", self.root / f"{k}.bin"
        try:
            if not meta_p.exists() or not body_p.exists():
                return None
            meta = json.loads(meta_p.read_text("utf-8"))
            if time.time() - meta.get("ts", 0) > self.ttl:
                return None
            return Response(
                url=url,
                status=meta.get("status", 200),
                headers=meta.get("headers", {}),
                content=body_p.read_bytes(),
                from_cache=True,
            )
        except Exception:
            return None

    def put(self, url: str, resp: Response) -> None:
        if not self.enabled or not resp.ok:
            return
        k = self._key(url)
        try:
            (self.root / f"{k}.bin").write_bytes(resp.content)
            (self.root / f"{k}.json").write_text(
                json.dumps(
                    {"ts": time.time(), "status": resp.status, "headers": resp.headers},
                    ensure_ascii=False,
                ),
                "utf-8",
            )
        except OSError:
            pass


# ---------------------------------------------------------------- 客户端

class HttpClient:
    """
    带重试 / 限速 / 缓存 / robots 的 HTTP 客户端。线程安全。
    """

    def __init__(
        self,
        timeout: float = 12.0,
        retries: int = 2,
        min_interval: float = 0.8,
        cache_dir: Optional[Path] = None,
        cache_ttl: int = 900,
        cache_enabled: bool = True,
        respect_robots: bool = True,
        max_bytes: int = 3_000_000,
        verbose: bool = False,
    ):
        self.timeout = timeout
        self.retries = max(0, retries)
        self.min_interval = min_interval
        self.max_bytes = max_bytes
        self.respect_robots = respect_robots
        self.verbose = verbose

        self.cache = DiskCache(cache_dir, cache_ttl, cache_enabled) if cache_dir else None
        self._lock = threading.Lock()
        self._local = threading.local()
        self._last_hit: Dict[str, float] = {}
        self._robots: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._robots_failed: set = set()

        # 统计，报告里要用
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0, "blocked_by_robots": 0}

    # -------------------------------------------------- 线程级超时覆盖

    def set_thread_timeout(self, t: Optional[float]) -> None:
        """
        给当前线程设置一次性超时覆盖。
        这样"某个引擎特别慢"不会拖垮整轮 —— 引擎层不必各自处理超时。
        """
        self._local.timeout = t

    def _timeout(self) -> float:
        return getattr(self._local, "timeout", None) or self.timeout

    def set_thread_robots(self, flag: Optional[bool]) -> None:
        """线程级 robots 开关（见 engines.search_engine 的用法说明）。"""
        self._local.robots = flag

    def _respect_robots(self) -> bool:
        v = getattr(self._local, "robots", None)
        return self.respect_robots if v is None else v

    # -------------------------------------------------- 内部工具

    def _throttle(self, host: str) -> None:
        """同一域名串行限速，跨域名互不影响。"""
        with self._lock:
            last = self._last_hit.get(host, 0.0)
            wait = self.min_interval - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.time()

    def _robots_ok(self, url: str) -> bool:
        """检查 robots.txt；拿不到就放行（不因为对方站点故障而瘫痪）。"""
        if not self._respect_robots():
            return True
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return True
        origin = f"{parsed.scheme}://{parsed.netloc}"

        with self._lock:
            if origin in self._robots_failed:
                return True
            rp = self._robots.get(origin)
            if rp is None:
                self._robots[origin] = None  # 占位，避免重复抓取

        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(origin + "/robots.txt")
            try:
                req = urllib.request.Request(
                    origin + "/robots.txt",
                    headers={"User-Agent": USER_AGENTS[0], "Accept-Encoding": "gzip, deflate"},
                )
                with urllib.request.urlopen(req, timeout=min(self._timeout(), 6)) as r:
                    raw = decompress(r.read(), r.headers.get("Content-Encoding", ""))
                rp.parse(decode_bytes(raw, "text/plain").splitlines())
            except Exception:
                with self._lock:
                    self._robots_failed.add(origin)
                return True

            with self._lock:
                self._robots[origin] = rp

        try:
            allowed = rp.can_fetch("*", url)
        except Exception:
            allowed = True
        if not allowed:
            self.stats["blocked_by_robots"] += 1
            if self.verbose:
                print(f"    [robots] 拒绝抓取: {url[:90]}")
        return allowed

    def _raw_get(self, url: str, headers: Dict[str, str]) -> Response:
        req = urllib.request.Request(url, headers=headers, method="GET")
        timeout = self._timeout()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                # 限制读入量，防止不小心拉个几百 MB 的文件
                content = r.read(self.max_bytes)
                content = decompress(content, r.headers.get("Content-Encoding", ""))
                hdrs = {k.lower(): v for k, v in r.headers.items()}
                return Response(url=r.geturl(), status=r.status, headers=hdrs, content=content)
        except urllib.error.HTTPError as e:
            # 403/404 的响应体有时也有用（比如反爬提示页），保留下来
            body = b""
            try:
                body = e.read(self.max_bytes)
                body = decompress(body, e.headers.get("Content-Encoding", "") if e.headers else "")
            except Exception:
                pass
            hdrs = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
            return Response(url=url, status=e.code, headers=hdrs, content=body,
                            error=f"HTTP {e.code}")
        except Exception as e:
            return Response(url=url, status=0, headers={}, content=b"", error=str(e)[:160])

    # -------------------------------------------------- 公开接口

    def get(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        allow_binary: bool = False,
        use_cache: bool = True,
    ) -> Response:
        if not url.lower().startswith(("http://", "https://")):
            return Response(url=url, status=0, headers={}, content=b"", error="非 http(s) URL")

        if self.cache and use_cache:
            hit = self.cache.get(url)
            if hit is not None:
                self.stats["cache_hits"] += 1
                return hit

        if not self._robots_ok(url):
            return Response(url=url, status=0, headers={}, content=b"", error="robots.txt 禁止")

        host = urllib.parse.urlparse(url).netloc
        hdrs = dict(DEFAULT_HEADERS)
        hdrs["User-Agent"] = random.choice(USER_AGENTS)
        if headers:
            hdrs.update(headers)

        last: Optional[Response] = None
        for attempt in range(self.retries + 1):
            self._throttle(host)
            self.stats["requests"] += 1
            resp = self._raw_get(url, hdrs)

            if resp.ok:
                if not allow_binary and any(t in resp.content_type.lower() for t in BINARY_TYPES):
                    return Response(url=url, status=resp.status, headers=resp.headers,
                                    content=b"", error=f"二进制内容已跳过: {resp.content_type[:40]}")
                if self.cache and use_cache:
                    self.cache.put(url, resp)
                return resp

            last = resp
            # 429 / 5xx 值得重试；4xx 其它直接放弃
            retryable = resp.status == 429 or resp.status >= 500 or resp.status == 0
            if not retryable or attempt == self.retries:
                break
            backoff = (1.5 ** attempt) + random.uniform(0, 0.6)
            if self.verbose:
                print(f"    [重试 {attempt+1}/{self.retries}] {url[:70]} ({resp.error}) 等待 {backoff:.1f}s")
            time.sleep(backoff)

        self.stats["errors"] += 1
        return last or Response(url=url, status=0, headers={}, content=b"", error="未知错误")

    def get_text(self, url: str, **kw) -> Tuple[str, Response]:
        r = self.get(url, **kw)
        return (r.text if r.ok else ""), r

    def get_json(self, url: str, headers: Optional[Dict[str, str]] = None) -> Any:
        h = {"Accept": "application/json, text/plain, */*"}
        if headers:
            h.update(headers)
        r = self.get(url, headers=h)
        if not r.ok:
            return None
        try:
            return r.json()
        except Exception:
            return None

    def close(self) -> None:
        pass


def host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def domain_of(url: str) -> str:
    """取可注册域名（去掉 www 和常见二级后缀），用于来源去重与权威度判断。"""
    h = host_of(url)
    if not h:
        return ""
    h = h.split(":")[0]
    for pre in ("www.", "m.", "mobile.", "wap."):
        if h.startswith(pre):
            h = h[len(pre):]
    return h


def is_same_site(a: str, b: str) -> bool:
    da, db = domain_of(a), domain_of(b)
    if not da or not db:
        return False
    return da == db or da.endswith("." + db) or db.endswith("." + da)
