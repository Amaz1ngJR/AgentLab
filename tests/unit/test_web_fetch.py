"""web_fetch 工具的单元测试。"""
from __future__ import annotations

import json
import socket
from unittest.mock import MagicMock, patch

import pytest

from app.tools.builtin.web_fetch import (
    MAX_CONTENT_CHARS,
    WEB_FETCH,
    _extract_beautifulsoup,
    _extract_canonical_url,
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


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """单测默认把示例域名解析到公网测试地址，避免依赖真实 DNS。"""
    monkeypatch.setattr(
        "app.tools.builtin.web_fetch.socket.getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
        ],
    )


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

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/admin",
        "http://10.0.0.1/internal",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/admin",
        "http://localhost/admin",
    ])
    def test_private_and_local_addresses_refused(self, url):
        with patch("requests.get") as get:
            result = _web_fetch({"url": url})
        assert result.startswith("refused:")
        get.assert_not_called()

    def test_hostname_resolving_to_private_address_refused(self, monkeypatch):
        monkeypatch.setattr(
            "app.tools.builtin.web_fetch.socket.getaddrinfo",
            lambda host, port, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port)),
            ],
        )
        with patch("requests.get") as get:
            result = _web_fetch({"url": "https://internal.example/page"})
        assert result.startswith("refused:")
        get.assert_not_called()

    def test_url_credentials_refused(self):
        with patch("requests.get") as get:
            result = _web_fetch({"url": "https://user:pass@example.com/page"})
        assert result.startswith("refused:")
        get.assert_not_called()


class TestCanonicalUrl:
    def test_attribute_order_does_not_matter(self):
        html = '<link href="/canonical" data-x="1" rel="alternate canonical">'
        assert _extract_canonical_url(html, "https://example.com/page") == \
            "https://example.com/canonical"

    def test_private_canonical_falls_back_to_final_url(self):
        html = '<link rel="canonical" href="http://127.0.0.1/internal">'
        assert _extract_canonical_url(html, "https://example.com/page") == \
            "https://example.com/page"


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
        # 显式模拟高优先级可选依赖不可用，避免测试结果依赖本机安装状态。
        with (
            patch("app.tools.builtin.web_fetch._extract_trafilatura", return_value=None),
            patch("app.tools.builtin.web_fetch._extract_readability", return_value=None),
        ):
            title, content, extractor = _extract_content(
                SAMPLE_HTML,
                "https://example.com",
            )
        assert extractor == "beautifulsoup"
        assert "WebRTC" in content


@pytest.mark.skipif(not HAS_REQUESTS, reason="requests not installed")
class TestFetchAndExtract:
    """完整抓取 + 抽取流程（mock HTTP 层）。"""

    def _mock_response(self, html: str, content_type: str = "text/html",
                       url: str = "https://example.com/article"):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.headers = {
            "Content-Type": content_type,
            "ETag": '"test-etag"',
            "Last-Modified": "Wed, 06 Aug 2026 00:00:00 GMT",
        }
        resp.url = url
        resp.status_code = 200
        resp.encoding = "utf-8"
        resp.iter_content = MagicMock(return_value=[html.encode("utf-8")])
        return resp

    def test_successful_fetch_returns_json(self):
        html = SAMPLE_HTML.replace(
            "</head>",
            '<link rel="canonical" href="/canonical-article"></head>',
        )
        with (
            patch("requests.get", return_value=self._mock_response(html)),
            patch("app.tools.builtin.web_fetch._extract_trafilatura", return_value=None),
            patch("app.tools.builtin.web_fetch._extract_readability", return_value=None),
        ):
            result = _web_fetch({"url": "https://example.com/article"})

        parsed = json.loads(result)
        assert parsed["url"] == "https://example.com/article"
        assert parsed["requested_url"] == "https://example.com/article"
        assert parsed["final_url"] == "https://example.com/article"
        assert parsed["canonical_url"] == "https://example.com/canonical-article"
        assert parsed["http_status"] == 200
        assert parsed["content_type"] == "text/html"
        assert parsed["encoding"] == "utf-8"
        assert parsed["etag"] == '"test-etag"'
        assert parsed["last_modified"] == "Wed, 06 Aug 2026 00:00:00 GMT"
        assert parsed["response_bytes"] == len(html.encode("utf-8"))
        assert parsed["retrieved_at"].endswith("Z")
        assert len(parsed["content_hash"]) == 64
        assert parsed["trust"] == "untrusted_external_content"
        assert parsed["title"] == "测试文章标题"
        assert parsed["content"].startswith("<untrusted_web_content")
        assert parsed["content"].endswith("</untrusted_web_content>")
        assert "WebRTC" in parsed["content"]
        assert parsed["extractor"] == "beautifulsoup"
        assert "chars" in parsed
        assert "elapsed_seconds" in parsed

    def test_redirect_to_private_address_refused(self):
        redirect = MagicMock()
        redirect.status_code = 302
        redirect.headers = {"Location": "http://127.0.0.1/admin"}
        redirect.url = "https://example.com/redirect"

        with patch("requests.get", return_value=redirect) as get:
            result = _web_fetch({"url": "https://example.com/redirect"})

        assert result.startswith("refused:")
        get.assert_called_once()
        assert get.call_args.kwargs["allow_redirects"] is False

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
        resp.status_code = 200
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
                   return_value=("<html></html>", {
                       "requested_url": "https://example.com",
                       "final_url": "https://example.com",
                       "redirect_chain": [],
                   }, None)), \
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
        resp.status_code = 200
        resp.encoding = "utf-8"
        resp.iter_content = MagicMock(return_value=[html.encode("utf-8")])

        with patch("requests.get", return_value=resp):
            result = _web_fetch({"url": "https://example.com"})

        # 原始密钥不应出现在结果中
        assert "sk-ant-1234567890abcdefghij" not in result
