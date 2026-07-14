"""web_fetch 工具的单元测试。"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.tools.builtin.web_fetch import (
    MAX_CONTENT_CHARS,
    WEB_FETCH,
    _extract_beautifulsoup,
    _extract_content,
    _truncate,
    _web_fetch,
    default_tools,
)

# 检查可选依赖
try:
    import bs4  # noqa: F401
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    import requests  # noqa: F401
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


SAMPLE_HTML = """
<html>
  <head><title>测试文章标题</title></head>
  <body>
    <nav>导航栏应该被去掉</nav>
    <article>
      <h1>WebRTC 学习笔记</h1>
      <p>WebRTC 是一种实时通信技术。</p>
      <p>它包含 ICE、STUN、TURN 等协议。</p>
    </article>
    <footer>页脚应该被去掉</footer>
    <script>console.log('脚本应该被去掉')</script>
  </body>
</html>
"""


class TestWebFetchTool:
    """工具注册与 schema。"""

    def test_tool_registration(self):
        assert WEB_FETCH.name == "web_fetch"
        assert WEB_FETCH.requires_approval is False
        assert "正文" in WEB_FETCH.description

    def test_tool_schema(self):
        schema = WEB_FETCH.to_schema()
        assert schema["name"] == "web_fetch"
        assert "url" in schema["input_schema"]["properties"]
        assert "url" in schema["input_schema"]["required"]

    def test_default_tools_export(self):
        tools = default_tools()
        assert len(tools) == 1
        assert tools[0].name == "web_fetch"


class TestUrlValidation:
    """URL 校验 —— SSRF 基本防护。"""

    def test_empty_url_refused(self):
        assert "refused" in _web_fetch({"url": ""})
        assert "refused" in _web_fetch({"url": "   "})

    def test_file_scheme_refused(self):
        result = _web_fetch({"url": "file:///etc/passwd"})
        assert "refused" in result
        assert "http/https" in result

    def test_ftp_scheme_refused(self):
        result = _web_fetch({"url": "ftp://example.com/file"})
        assert "refused" in result

    def test_no_scheme_refused(self):
        result = _web_fetch({"url": "example.com/page"})
        assert "refused" in result

    def test_missing_netloc_refused(self):
        result = _web_fetch({"url": "http://"})
        assert "refused" in result


class TestTruncate:
    """正文截断。"""

    def test_no_truncation_under_limit(self):
        text, truncated = _truncate("short text", limit=100)
        assert text == "short text"
        assert truncated is False

    def test_truncation_over_limit(self):
        long_text = "x" * 200
        text, truncated = _truncate(long_text, limit=100)
        assert len(text) == 100
        assert truncated is True


@pytest.mark.skipif(not HAS_BS4, reason="beautifulsoup4 not installed")
class TestBeautifulSoupExtraction:
    """BeautifulSoup 兜底抽取。"""

    def test_extract_removes_nav_footer_script(self):
        result = _extract_beautifulsoup(SAMPLE_HTML)
        assert result is not None
        title, text = result
        assert title == "测试文章标题"
        assert "WebRTC" in text
        assert "ICE" in text
        # 非正文元素应被去掉
        assert "导航栏应该被去掉" not in text
        assert "页脚应该被去掉" not in text
        assert "脚本应该被去掉" not in text

    def test_extract_empty_html_returns_none(self):
        result = _extract_beautifulsoup("<html><body></body></html>")
        assert result is None

    def test_extract_content_falls_through_to_bs4(self):
        # trafilatura/readability 未装时，应该落到 beautifulsoup
        title, content, extractor = _extract_content(SAMPLE_HTML, "https://example.com")
        assert extractor == "beautifulsoup"
        assert "WebRTC" in content


@pytest.mark.skipif(not HAS_REQUESTS, reason="requests not installed")
class TestFetchAndExtract:
    """完整抓取 + 抽取流程（mock HTTP 层）。"""

    def _mock_response(self, html: str, content_type: str = "text/html",
                       url: str = "https://example.com/article"):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.headers = {"Content-Type": content_type}
        resp.url = url
        resp.encoding = "utf-8"
        resp.iter_content = MagicMock(return_value=[html.encode("utf-8")])
        return resp

    def test_successful_fetch_returns_json(self):
        with patch("requests.get", return_value=self._mock_response(SAMPLE_HTML)):
            result = _web_fetch({"url": "https://example.com/article"})

        parsed = json.loads(result)
        assert parsed["url"] == "https://example.com/article"
        assert parsed["title"] == "测试文章标题"
        assert "WebRTC" in parsed["content"]
        assert parsed["extractor"] == "beautifulsoup"
        assert "chars" in parsed
        assert "elapsed_seconds" in parsed

    def test_non_html_content_type_rejected(self):
        with patch("requests.get",
                   return_value=self._mock_response("PDF", content_type="application/pdf")):
            result = _web_fetch({"url": "https://example.com/doc.pdf"})
        assert "error" in result
        assert "content-type" in result

    def test_fetch_exception_returns_error(self):
        with patch("requests.get", side_effect=Exception("Connection refused")):
            result = _web_fetch({"url": "https://example.com"})
        assert "error:" in result
        assert "Connection refused" in result

    def test_response_too_large_rejected(self):
        # 单块就超过上限
        big_chunk = b"x" * 6_000_000
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.headers = {"Content-Type": "text/html"}
        resp.url = "https://example.com"
        resp.encoding = "utf-8"
        resp.iter_content = MagicMock(return_value=[big_chunk])
        with patch("requests.get", return_value=resp):
            result = _web_fetch({"url": "https://example.com"})
        assert "error" in result
        assert "too large" in result

    def test_max_chars_truncates_content(self):
        # 生成一篇超长文章
        long_body = "".join(f"<p>段落 {i} 内容内容内容内容内容内容。</p>" for i in range(2000))
        html = f"<html><head><title>长文</title></head><body><article>{long_body}</article></body></html>"
        with patch("requests.get", return_value=self._mock_response(html)):
            result = _web_fetch({"url": "https://example.com", "max_chars": 500})

        parsed = json.loads(result)
        assert parsed["truncated"] is True
        assert parsed["chars"] <= 500

    def test_empty_content_returns_error(self):
        html = "<html><head><title>空</title></head><body></body></html>"
        with patch("requests.get", return_value=self._mock_response(html)):
            result = _web_fetch({"url": "https://example.com"})
        assert "error" in result

    def test_timeout_clamped(self):
        captured = {}

        def fake_get(url, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return self._mock_response(SAMPLE_HTML)

        with patch("requests.get", side_effect=fake_get):
            _web_fetch({"url": "https://example.com", "timeout": 100})
        assert captured["timeout"] == 30  # 上限

        with patch("requests.get", side_effect=fake_get):
            _web_fetch({"url": "https://example.com", "timeout": 1})
        assert captured["timeout"] == 5  # 下限


class TestNoExtractor:
    """所有抽取器都没装时的优雅降级。"""

    def test_no_extractor_returns_install_hint(self):
        # mock 所有抽取器都返回 None
        with patch("app.tools.builtin.web_fetch._fetch_html",
                   return_value=("<html></html>", "https://example.com", None)), \
             patch("app.tools.builtin.web_fetch._extract_content",
                   return_value=("", "", "none")):
            result = _web_fetch({"url": "https://example.com"})
        assert "error" in result
        assert "extractor" in result
        assert "trafilatura" in result


class TestRedaction:
    """脱敏 —— URL/正文里的密钥不应原样返回。"""

    def test_content_is_redacted(self):
        html = (
            "<html><head><title>密钥泄漏</title></head><body><article>"
            "<p>token: sk-ant-1234567890abcdefghij</p>"
            "</article></body></html>"
        )
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.headers = {"Content-Type": "text/html"}
        resp.url = "https://example.com"
        resp.encoding = "utf-8"
        resp.iter_content = MagicMock(return_value=[html.encode("utf-8")])

        with patch("requests.get", return_value=resp):
            result = _web_fetch({"url": "https://example.com"})

        # 原始密钥不应出现在结果中
        assert "sk-ant-1234567890abcdefghij" not in result
