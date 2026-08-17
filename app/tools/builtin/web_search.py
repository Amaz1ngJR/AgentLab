"""Web 搜索工具 —— Agent 的互联网信息检索能力。

使用场景:
  模型需要搜索互联网上的最新信息、文档、技术问题解答等。
  返回结构化的搜索结果(标题 + 链接 + 摘要),Agent 可以基于此进一步
  使用浏览器工具访问具体页面,或直接基于摘要回答用户问题。

实现策略:
  1. 优先使用 DuckDuckGo 搜索(免费、无需 API key、注重隐私)
  2. 支持多种搜索后端扩展(Google、Bing 等,需要 API key)
  3. 返回结构化结果:标题、URL、摘要、来源
  4. 结果数量可配置,默认 10 条
  5. 输出大小限制,避免撑爆上下文

安全:
  - 只读 network 请求,内置公网搜索不需要逐次审批
  - 结果经过脱敏处理
  - 超时保护,避免阻塞
  - 输出大小硬截断
"""
from __future__ import annotations

import json
import time
from typing import Optional
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from app.tools.registry import Tool
from app.util.redact import redact

# ── 限制常量 ──────────────────────────────────────────────────────────────────
DEFAULT_MAX_RESULTS = 10          # 默认最多返回多少条结果
MAX_OUTPUT_BYTES = 32_000         # 序列化后 JSON 的硬上限
MAX_SNIPPET_CHARS = 500           # 单条结果摘要截断
DEFAULT_TIMEOUT = 15              # 搜索请求超时(秒)


def _extract_duckduckgo_result_url(href: str) -> str:
    """从 DuckDuckGo 结果链接提取真实 URL，拒绝非 HTTP(S) scheme。

    HTML 端点常把真实地址放在 ``uddg`` query 参数中；直接使用页面展示 URL
    会丢路径或使用省略文本。无法解析时仅接受完整的公网 HTTP(S) 形式，后续
    真正 fetch 时仍由 web_fetch 执行 DNS/SSRF 校验。
    """
    href = (href or "").strip()
    if not href:
        return ""

    absolute = urljoin("https://duckduckgo.com", href)
    parsed = urlparse(absolute)
    if parsed.hostname and parsed.hostname.lower().endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            absolute = unquote(target)
            parsed = urlparse(absolute)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return absolute


def _search_duckduckgo(query: str, max_results: int, timeout: int) -> tuple[list[dict], Optional[str]]:
    """使用 DuckDuckGo 搜索,返回 (结果列表, 错误信息)。

    使用 ddgs 库(需要安装: pip install ddgs)。
    如果库未安装,返回空列表和提示信息。
    """
    # ddgs 是当前包名；保留 duckduckgo_search 兼容旧环境和已有安装。
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return [], (
                "DuckDuckGo library not installed. Install with: "
                "pip install ddgs"
            )

    results = []
    error = None

    try:
        with DDGS() as ddgs:
            # text() 返回生成器,我们需要限制数量
            raw_results = ddgs.text(
                keywords=query,
                max_results=max_results,
            )

            for idx, item in enumerate(raw_results):
                if idx >= max_results:
                    break

                actual_url = _extract_duckduckgo_result_url(item.get("href", ""))
                if not actual_url:
                    continue
                # DuckDuckGo 返回格式: {title, href, body}
                result = {
                    "title": redact(item.get("title", ""))[:200],
                    "url": actual_url,
                    "snippet": redact(item.get("body", ""))[:MAX_SNIPPET_CHARS],
                    "snippet_only": True,
                    "search_provider": "duckduckgo",
                    # 兼容既有调用；后续 Source 模型落地后删除该别名。
                    "source": "duckduckgo",
                }
                results.append(result)

    except Exception as exc:
        error = f"DuckDuckGo search failed: {type(exc).__name__}: {exc}"

    return results, error


def _search_with_requests(query: str, max_results: int, timeout: int) -> tuple[list[dict], Optional[str]]:
    """使用 requests 直接请求搜索引擎 HTML(备用方案)。

    注意:这种方法可能不稳定,搜索引擎会检测爬虫并返回验证码。
    仅作为 fallback,不推荐作为主要方案。
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        return [], "requests or beautifulsoup4 not installed"

    results = []
    error = None

    try:
        # 使用 DuckDuckGo 的 HTML 版本(相对友好)
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # 解析搜索结果
        for idx, result_div in enumerate(soup.select(".result")):
            if idx >= max_results:
                break

            title_elem = result_div.select_one(".result__title")
            # DuckDuckGo 的真实结果链接在标题 <a href>；.result__url 只是展示文本，
            # 可能被截断，不能作为可抓取 URL。
            anchor_elem = result_div.select_one(".result__a") or title_elem
            snippet_elem = result_div.select_one(".result__snippet")

            if not title_elem:
                continue

            title = redact(title_elem.get_text(strip=True))[:200]
            href = anchor_elem.get("href", "") if anchor_elem else ""
            actual_url = _extract_duckduckgo_result_url(href)
            if not actual_url:
                continue
            snippet = redact(snippet_elem.get_text(strip=True))[:MAX_SNIPPET_CHARS] if snippet_elem else ""

            result = {
                "title": title,
                "url": actual_url,
                "snippet": snippet,
                "snippet_only": True,
                "search_provider": "duckduckgo_html",
                # 兼容既有调用；后续 Source 模型落地后删除该别名。
                "source": "duckduckgo_html",
            }
            results.append(result)

    except Exception as exc:
        error = f"HTML search failed: {type(exc).__name__}: {exc}"

    return results, error


def _enforce_output_limit(result: dict) -> dict:
    """序列化后超过 MAX_OUTPUT_BYTES 时,从尾部裁掉结果并标记 truncated。"""
    while result["results"]:
        if len(json.dumps(result, ensure_ascii=False)) <= MAX_OUTPUT_BYTES:
            break
        result["results"].pop()
        result["truncated"] = True
    return result


def _web_search(args: dict) -> str:
    """web_search 工具入口。返回 JSON 字符串;错误返回 'error:'。"""
    query = (args.get("query") or "").strip()
    if not query:
        return "refused: empty query"

    try:
        max_results = int(args.get("max_results", DEFAULT_MAX_RESULTS))
    except (TypeError, ValueError):
        max_results = DEFAULT_MAX_RESULTS
    max_results = max(1, min(max_results, 50))  # 限制 1-50

    try:
        timeout = int(args.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(5, min(timeout, 30))  # 限制 5-30 秒

    engine = args.get("engine", "auto")
    if engine not in ("auto", "duckduckgo", "duckduckgo_html"):
        return f"refused: unknown engine '{engine}' (use auto/duckduckgo/duckduckgo_html)"

    start_time = time.time()
    results: list[dict] = []
    error: Optional[str] = None
    backend_used = ""

    # 尝试不同的搜索后端
    if engine in ("auto", "duckduckgo"):
        results, error = _search_duckduckgo(query, max_results, timeout)
        backend_used = "duckduckgo"

        # 如果 DuckDuckGo 库失败,尝试 HTML fallback
        if not results and engine == "auto":
            results, error2 = _search_with_requests(query, max_results, timeout)
            if results:
                backend_used = "duckduckgo_html"
                error = None
            elif error2:
                error = f"{error}; {error2}" if error else error2

    elif engine == "duckduckgo_html":
        results, error = _search_with_requests(query, max_results, timeout)
        backend_used = "duckduckgo_html"

    elapsed = time.time() - start_time

    # 构造返回结果
    result = {
        "query": query,
        "backend": backend_used,
        "count": len(results),
        "truncated": False,
        "elapsed_seconds": round(elapsed, 2),
        "results": results,
    }

    if error:
        result["error"] = error

    # 如果没有结果但有错误,返回错误信息
    if not results and error:
        return f"error: {error}"

    # 强制输出大小限制
    result = _enforce_output_limit(result)
    result["count"] = len(result["results"])

    return json.dumps(result, ensure_ascii=False, indent=2)


def _web_search_audit_summary(args: dict, result: str) -> tuple[str, str]:
    safe_args = {
        key: args.get(key)
        for key in ("query", "max_results", "engine", "timeout")
        if key in args
    }
    try:
        body = json.loads(result)
        result_summary = json.dumps(
            {
                "engine": body.get("engine", ""),
                "count": body.get("count", 0),
                "truncated": body.get("truncated", False),
                "error": body.get("error", ""),
            },
            ensure_ascii=False,
        )
    except (json.JSONDecodeError, TypeError, AttributeError):
        result_summary = result
    return json.dumps(safe_args, ensure_ascii=False), result_summary


WEB_SEARCH = Tool(
    name="web_search",
    description=(
        "在互联网上搜索信息,返回标题、链接和摘要。"
        "用于获取最新信息、技术文档、新闻、问题解答等。"
        "搜索结果仅包含摘要,如需完整内容应使用浏览器工具访问具体 URL。"
        "默认使用 DuckDuckGo 搜索引擎(注重隐私,无需 API key)。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词或问题",
            },
            "max_results": {
                "type": "integer",
                "default": DEFAULT_MAX_RESULTS,
                "description": f"最多返回结果数,默认 {DEFAULT_MAX_RESULTS},范围 1-50",
            },
            "engine": {
                "type": "string",
                "enum": ["auto", "duckduckgo", "duckduckgo_html"],
                "default": "auto",
                "description": "搜索引擎后端,auto=自动选择,duckduckgo=使用库,duckduckgo_html=HTML解析",
            },
            "timeout": {
                "type": "integer",
                "default": DEFAULT_TIMEOUT,
                "description": f"超时时间(秒),默认 {DEFAULT_TIMEOUT},范围 5-30",
            },
        },
        "required": ["query"],
    },
    executor=_web_search,
    risk="network",
    target_type="internet",
    scope="public_web",
    origin="builtin",
    audit_redactor=_web_search_audit_summary,
    requires_approval=False,  # 只读 network 请求,当前内置公网搜索免逐次审批
)


def default_tools() -> list[Tool]:
    """返回 web_search 模块的默认工具列表。"""
    return [WEB_SEARCH]
