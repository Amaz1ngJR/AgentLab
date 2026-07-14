"""Web 抓取工具 —— 给定 URL 抓取网页正文并转成 Markdown。

使用场景:
  模型需要读一篇完整的网页文章(知乎/博客/文档/新闻),web_search 只给摘要,
  不够;浏览器 MCP(browser_navigate + browser_snapshot)能拿全文但偏重(要起
  浏览器内核、走 DOM 无障碍树)。web_fetch 是"读一篇文章"的轻量首选:
  一次 HTTP GET 抓 HTML → 抽正文 → 转 Markdown 交给模型。

实现策略:
  1. requests 抓 HTML(带超时、大小上限、通用 UA)。
  2. 正文抽取优先级:trafilatura(最好)→ readability-lxml + BeautifulSoup →
     纯 BeautifulSoup 兜底(去 script/style/nav 后取文本)。都没装则优雅降级。
  3. 转 Markdown:有 markdownify 用它,否则用抽出的纯文本。
  4. 正文大小硬截断,避免一篇长文撑爆上下文。

安全:
  - 只读 network 风险,不需逐次审批(同 web_search)。
  - 只允许 http/https,拒绝 file:// 等本地/内网协议绕过(SSRF 基本防护)。
  - 抓取内容与 URL 经 redact() 脱敏后再返回。
  - 超时 + 响应体大小上限,避免阻塞或拉爆内存。
  - 依赖未装时返回明确的安装提示,不抛异常。
"""
from __future__ import annotations

import json
import time
from typing import Optional
from urllib.parse import urlparse

from app.tools.registry import Tool
from app.util.redact import redact

# ── 限制常量 ──────────────────────────────────────────────────────────────────
DEFAULT_TIMEOUT = 15              # HTTP 请求超时(秒)
MAX_CONTENT_CHARS = 20_000        # 抽出正文的字符上限(超出截断)
MAX_RESPONSE_BYTES = 5_000_000    # HTTP 响应体字节上限(5MB,超出拒绝,防大文件)
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _truncate(text: str, limit: int = MAX_CONTENT_CHARS) -> tuple[str, bool]:
    """把正文截断到 limit 字符,返回 (截断后文本, 是否被截断)。"""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _fetch_html(url: str, timeout: int) -> tuple[str, str, Optional[str]]:
    """HTTP GET 抓 HTML,返回 (html, final_url, 错误信息)。

    带响应体大小上限:用 stream 逐块读,超过 MAX_RESPONSE_BYTES 就中断,避免
    把超大页面/误点的二进制文件整个读进内存。
    """
    try:
        import requests
    except ImportError:
        return "", url, "requests not installed. Install with: pip install requests"

    headers = {"User-Agent": DEFAULT_UA, "Accept": "text/html,application/xhtml+xml"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
        resp.raise_for_status()

        # 非 HTML 内容(pdf/图片/二进制)不适合抽正文,直接拒绝
        ctype = resp.headers.get("Content-Type", "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype and "text" not in ctype:
            return "", resp.url, f"unsupported content-type: {ctype} (web_fetch only reads HTML pages)"

        # 逐块读,超过上限就停(防超大响应体撑爆内存)
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                return "", resp.url, f"response too large (> {MAX_RESPONSE_BYTES} bytes)"
            chunks.append(chunk)

        raw = b"".join(chunks)
        # 优先用 requests 猜的编码,兜底 utf-8
        encoding = resp.encoding or "utf-8"
        try:
            html = raw.decode(encoding, errors="replace")
        except (LookupError, TypeError):
            html = raw.decode("utf-8", errors="replace")
        return html, resp.url, None
    except Exception as exc:
        return "", url, f"fetch failed: {type(exc).__name__}: {exc}"


def _extract_trafilatura(html: str, url: str) -> Optional[tuple[str, str]]:
    """用 trafilatura 抽正文并转 Markdown,返回 (title, markdown);未装/失败返回 None。"""
    try:
        import trafilatura
    except ImportError:
        return None
    try:
        # output_format="markdown" 直接产出 Markdown;with_metadata 拿标题
        md = trafilatura.extract(
            html, output_format="markdown", include_links=True,
            include_tables=True, url=url,
        )
        if not md:
            return None
        title = ""
        try:
            meta = trafilatura.extract_metadata(html)
            if meta and getattr(meta, "title", None):
                title = meta.title
        except Exception:
            pass
        return title, md
    except Exception:
        return None


def _extract_readability(html: str) -> Optional[tuple[str, str]]:
    """用 readability-lxml 抽正文 HTML,再转 Markdown/文本,返回 (title, content)。"""
    try:
        from readability import Document
    except ImportError:
        return None
    try:
        doc = Document(html)
        title = doc.short_title() or ""
        content_html = doc.summary(html_partial=True)
        text = _html_to_markdown(content_html)
        if not text.strip():
            return None
        return title, text
    except Exception:
        return None


def _extract_beautifulsoup(html: str) -> Optional[tuple[str, str]]:
    """纯 BeautifulSoup 兜底:去掉脚本/样式/导航后取正文文本,返回 (title, text)。"""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        # 去掉明显的非正文元素
        for tag in soup(["script", "style", "nav", "header", "footer",
                         "aside", "noscript", "form", "iframe"]):
            tag.decompose()
        # 优先 <article> / <main>,否则 <body>
        main = soup.find("article") or soup.find("main") or soup.body or soup
        text = main.get_text(separator="\n", strip=True)
        # 压掉连续空行
        lines = [ln for ln in (l.strip() for l in text.splitlines()) if ln]
        text = "\n".join(lines)
        if not text.strip():
            return None
        return title, text
    except Exception:
        return None


def _html_to_markdown(html: str) -> str:
    """把一段 HTML 转成 Markdown;有 markdownify 用它,否则退回纯文本。"""
    try:
        from markdownify import markdownify as md
        return md(html, heading_style="ATX")
    except ImportError:
        pass
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)
    except ImportError:
        return html


def _extract_content(html: str, url: str) -> tuple[str, str, str]:
    """按优先级抽正文,返回 (title, markdown, extractor_used)。

    trafilatura > readability > beautifulsoup。全都没装则 extractor_used="none"。
    """
    for name, fn in (("trafilatura", lambda: _extract_trafilatura(html, url)),
                     ("readability", lambda: _extract_readability(html)),
                     ("beautifulsoup", lambda: _extract_beautifulsoup(html))):
        result = fn()
        if result is not None:
            title, content = result
            return title, content, name
    return "", "", "none"


def _web_fetch(args: dict) -> str:
    """web_fetch 工具入口。返回 JSON 字符串;错误返回 'refused:'/'error:'。"""
    url = (args.get("url") or "").strip()
    if not url:
        return "refused: empty url"

    # 只允许 http/https,防 file:// 等协议绕过读本地文件 / 内网(基本 SSRF 防护)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"refused: only http/https urls are allowed (got '{parsed.scheme or 'none'}')"
    if not parsed.netloc:
        return f"refused: invalid url '{url}'"

    try:
        timeout = int(args.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(5, min(timeout, 30))

    try:
        max_chars = int(args.get("max_chars", MAX_CONTENT_CHARS))
    except (TypeError, ValueError):
        max_chars = MAX_CONTENT_CHARS
    max_chars = max(500, min(max_chars, MAX_CONTENT_CHARS))

    start_time = time.time()
    html, final_url, error = _fetch_html(url, timeout)
    if error:
        return f"error: {error}"

    title, content, extractor = _extract_content(html, final_url)
    if extractor == "none":
        return ("error: no content extractor available. Install one with: "
                "pip install trafilatura (recommended), or readability-lxml + markdownify, "
                "or beautifulsoup4")
    if not content.strip():
        return "error: could not extract readable content from this page (empty result)"

    content, truncated = _truncate(content, max_chars)
    elapsed = time.time() - start_time

    result = {
        "url": redact(final_url),
        "title": redact(title)[:300],
        "extractor": extractor,
        "truncated": truncated,
        "chars": len(content),
        "elapsed_seconds": round(elapsed, 2),
        "content": redact(content),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


WEB_FETCH = Tool(
    name="web_fetch",
    description=(
        "抓取一个网页 URL 的正文并转成 Markdown 返回。"
        "用于读完整文章(知乎/博客/技术文档/新闻),补足 web_search 只给摘要的不足。"
        "只读 HTML 页面(pdf/图片/二进制会被拒绝);比浏览器工具更轻量,"
        "适合'读一篇文章'的场景。需要点击/登录/交互时才用 browser_* 工具。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要抓取的网页 URL(必须是 http/https)",
            },
            "max_chars": {
                "type": "integer",
                "default": MAX_CONTENT_CHARS,
                "description": f"正文最多返回多少字符,默认 {MAX_CONTENT_CHARS},超出截断",
            },
            "timeout": {
                "type": "integer",
                "default": DEFAULT_TIMEOUT,
                "description": f"超时时间(秒),默认 {DEFAULT_TIMEOUT},范围 5-30",
            },
        },
        "required": ["url"],
    },
    executor=_web_fetch,
    requires_approval=False,  # 只读 network 风险,不需审批
)


def default_tools() -> list[Tool]:
    """返回 web_fetch 模块的默认工具列表。"""
    return [WEB_FETCH]
