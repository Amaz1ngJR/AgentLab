"""web_search 工具的单元测试。"""
from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.tools.builtin.web_search import (
    WEB_SEARCH,
    _enforce_output_limit,
    _extract_duckduckgo_result_url,
    _search_duckduckgo,
    _search_with_requests,
    _web_search,
)

# 检查当前或旧版可选依赖是否安装
try:
    import ddgs
    DDGS_MODULE = "ddgs"
except ImportError:
    try:
        import duckduckgo_search
        DDGS_MODULE = "duckduckgo_search"
    except ImportError:
        DDGS_MODULE = None
HAS_DUCKDUCKGO = DDGS_MODULE is not None

try:
    import requests
    import bs4
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class TestWebSearchTool:
    """测试 web_search 工具的基本功能。"""

    def test_tool_registration(self):
        """工具应该正确注册名称和描述。"""
        assert WEB_SEARCH.name == "web_search"
        assert "互联网" in WEB_SEARCH.description
        assert WEB_SEARCH.requires_approval is False

    def test_tool_schema(self):
        """工具 schema 应该包含必需参数。"""
        schema = WEB_SEARCH.to_schema()
        assert schema["name"] == "web_search"
        assert "query" in schema["input_schema"]["properties"]
        assert "query" in schema["input_schema"]["required"]

    def test_empty_query_refused(self):
        """空查询应该被拒绝。"""
        result = _web_search({"query": ""})
        assert "refused" in result
        assert "empty query" in result

        result = _web_search({"query": "   "})
        assert "refused" in result

    def test_unknown_engine_refused(self):
        """未知搜索引擎应该被拒绝。"""
        result = _web_search({"query": "test", "engine": "google"})
        assert "refused" in result
        assert "unknown engine" in result

    def test_max_results_clamping(self):
        """max_results 应该被限制在合理范围内。"""
        with patch("app.tools.builtin.web_search._search_duckduckgo") as mock_search:
            mock_search.return_value = ([], None)

            # 测试上限
            _web_search({"query": "test", "max_results": 1000})
            _, max_res, _ = mock_search.call_args[0]
            assert max_res == 50  # 应该被限制为 50

            # 测试下限
            _web_search({"query": "test", "max_results": 0})
            _, max_res, _ = mock_search.call_args[0]
            assert max_res == 1  # 应该被限制为 1

    def test_timeout_clamping(self):
        """timeout 应该被限制在合理范围内。"""
        with patch("app.tools.builtin.web_search._search_duckduckgo") as mock_search:
            mock_search.return_value = ([], None)

            # 测试上限
            _web_search({"query": "test", "timeout": 100})
            _, _, timeout = mock_search.call_args[0]
            assert timeout == 30  # 应该被限制为 30

            # 测试下限
            _web_search({"query": "test", "timeout": 1})
            _, _, timeout = mock_search.call_args[0]
            assert timeout == 5  # 应该被限制为 5


class TestDuckDuckGoSearch:
    """测试 DuckDuckGo 搜索后端。"""

    def test_duckduckgo_not_installed(self):
        """库未安装时应该返回错误提示。"""
        # Mock ImportError at the import point inside the function
        with patch.dict("sys.modules", {"duckduckgo_search": None}):
            results, error = _search_duckduckgo("test", 10, 15)
            assert results == []
            assert error is not None
            assert "not installed" in error

    @pytest.mark.skipif(not HAS_DUCKDUCKGO, reason="duckduckgo_search not installed")
    def test_duckduckgo_success(self):
        """成功搜索应该返回格式化结果。"""
        mock_ddgs = MagicMock()
        mock_results = [
            {"title": "Result 1", "href": "https://example.com/1", "body": "Snippet 1"},
            {"title": "Result 2", "href": "https://example.com/2", "body": "Snippet 2"},
        ]
        mock_ddgs.__enter__.return_value.text.return_value = iter(mock_results)

        with patch(f"{DDGS_MODULE}.DDGS", return_value=mock_ddgs):
            results, error = _search_duckduckgo("test query", 10, 15)

            assert error is None
            assert len(results) == 2
            assert results[0]["title"] == "Result 1"
            assert results[0]["url"] == "https://example.com/1"
            assert results[0]["snippet"] == "Snippet 1"
            assert results[0]["source"] == "duckduckgo"

    @pytest.mark.skipif(not HAS_DUCKDUCKGO, reason="duckduckgo_search not installed")
    def test_duckduckgo_max_results_limit(self):
        """应该限制返回结果数量。"""
        mock_ddgs = MagicMock()
        mock_results = [
            {"title": f"Result {i}", "href": f"https://example.com/{i}", "body": f"Snippet {i}"}
            for i in range(20)
        ]
        mock_ddgs.__enter__.return_value.text.return_value = iter(mock_results)

        with patch(f"{DDGS_MODULE}.DDGS", return_value=mock_ddgs):
            results, error = _search_duckduckgo("test", 5, 15)

            assert error is None
            assert len(results) == 5  # 应该只返回 5 个

    @pytest.mark.skipif(not HAS_DUCKDUCKGO, reason="duckduckgo_search not installed")
    def test_duckduckgo_exception_handling(self):
        """搜索异常应该被捕获并返回错误信息。"""
        mock_ddgs = MagicMock()
        mock_ddgs.__enter__.return_value.text.side_effect = RuntimeError("Network error")

        with patch(f"{DDGS_MODULE}.DDGS", return_value=mock_ddgs):
            results, error = _search_duckduckgo("test", 10, 15)

            assert results == []
            assert error is not None
            assert "RuntimeError" in error
            assert "Network error" in error

    @pytest.mark.skipif(not HAS_DUCKDUCKGO, reason="duckduckgo_search not installed")
    def test_snippet_truncation(self):
        """长摘要应该被截断。"""
        mock_ddgs = MagicMock()
        long_snippet = "x" * 1000
        mock_results = [
            {"title": "Result 1", "href": "https://example.com/1", "body": long_snippet},
        ]
        mock_ddgs.__enter__.return_value.text.return_value = iter(mock_results)

        with patch(f"{DDGS_MODULE}.DDGS", return_value=mock_ddgs):
            results, error = _search_duckduckgo("test", 10, 15)

            assert error is None
            assert len(results) == 1
            assert len(results[0]["snippet"]) == 500  # MAX_SNIPPET_CHARS


class TestHTMLSearch:
    """测试 HTML 解析搜索后端。"""

    def test_extract_result_url_uses_uddg_target(self):
        href = "/l/?uddg=https%3A%2F%2Fdocs.example.com%2Fguide%3Fv%3D2"
        assert _extract_duckduckgo_result_url(href) == "https://docs.example.com/guide?v=2"

    def test_extract_result_url_rejects_non_http_scheme(self):
        href = "/l/?uddg=file%3A%2F%2F%2Fetc%2Fpasswd"
        assert _extract_duckduckgo_result_url(href) == ""

    def test_requests_not_installed(self):
        """库未安装时应该返回错误提示。"""
        with patch.dict("sys.modules", {"requests": None}):
            results, error = _search_with_requests("test", 10, 15)
            assert results == []
            assert error is not None
            assert "not installed" in error

    @pytest.mark.skipif(not HAS_REQUESTS, reason="requests or beautifulsoup4 not installed")
    def test_html_search_success(self):
        """成功解析 HTML 应该返回格式化结果。"""
        mock_response = MagicMock()
        mock_response.text = """
        <html>
            <div class="result">
                <a class="result__title">Result 1</a>
                <a class="result__url">example.com/1</a>
                <a class="result__snippet">Snippet 1</a>
            </div>
            <div class="result">
                <a class="result__title">Result 2</a>
                <a class="result__url">https://example.com/2</a>
                <a class="result__snippet">Snippet 2</a>
            </div>
        </html>
        """

        with patch("requests.get") as mock_get, \
             patch("bs4.BeautifulSoup") as mock_bs:

            mock_get.return_value = mock_response

            # Mock BeautifulSoup
            mock_soup = MagicMock()
            mock_bs.return_value = mock_soup

            # Mock result elements
            title1 = MagicMock(get_text=lambda **kw: "Result 1")
            title1.get.return_value = "/l/?uddg=https%3A%2F%2Fexample.com%2F1%3Fa%3D1"
            result1 = MagicMock()
            result1.select_one.side_effect = lambda sel: {
                ".result__title": title1,
                ".result__a": None,
                ".result__url": MagicMock(get_text=lambda **kw: "example.com/1"),
                ".result__snippet": MagicMock(get_text=lambda **kw: "Snippet 1"),
            }.get(sel)

            title2 = MagicMock(get_text=lambda **kw: "Result 2")
            title2.get.return_value = "https://example.com/2"
            result2 = MagicMock()
            result2.select_one.side_effect = lambda sel: {
                ".result__title": title2,
                ".result__a": None,
                ".result__url": MagicMock(get_text=lambda **kw: "truncated.example/…"),
                ".result__snippet": MagicMock(get_text=lambda **kw: "Snippet 2"),
            }.get(sel)

            mock_soup.select.return_value = [result1, result2]

            results, error = _search_with_requests("test", 10, 15)

            assert error is None
            assert len(results) == 2
            assert results[0]["source"] == "duckduckgo_html"
            assert results[0]["search_provider"] == "duckduckgo_html"
            assert results[0]["snippet_only"] is True
            assert results[0]["url"] == "https://example.com/1?a=1"
            assert results[1]["url"] == "https://example.com/2"

    @pytest.mark.skipif(not HAS_REQUESTS, reason="requests not installed")
    def test_html_search_exception_handling(self):
        """请求异常应该被捕获并返回错误信息。"""
        with patch("requests.get") as mock_get:
            mock_get.side_effect = Exception("Connection timeout")

            results, error = _search_with_requests("test", 10, 15)

            assert results == []
            assert error is not None
            assert "Exception" in error
            assert "Connection timeout" in error


class TestOutputLimit:
    """测试输出大小限制。"""

    def test_enforce_output_limit_no_truncation(self):
        """小结果集不应该被截断。"""
        result = {
            "query": "test",
            "count": 2,
            "truncated": False,
            "results": [
                {"title": "A", "url": "https://a.com", "snippet": "a"},
                {"title": "B", "url": "https://b.com", "snippet": "b"},
            ],
        }

        output = _enforce_output_limit(result.copy())
        assert output["truncated"] is False
        assert len(output["results"]) == 2

    def test_enforce_output_limit_with_truncation(self):
        """超大结果集应该被截断。"""
        huge_snippet = "x" * 10_000
        result = {
            "query": "test",
            "count": 50,
            "truncated": False,
            "results": [
                {"title": f"Result {i}", "url": f"https://example.com/{i}", "snippet": huge_snippet}
                for i in range(50)
            ],
        }

        output = _enforce_output_limit(result)
        assert output["truncated"] is True
        assert len(output["results"]) < 50  # 应该被截断

        # 确保输出不超过限制
        serialized = json.dumps(output, ensure_ascii=False)
        assert len(serialized) <= 32_000


class TestWebSearchIntegration:
    """测试完整的 web_search 流程。"""

    def test_successful_search_returns_json(self):
        """成功的搜索应该返回 JSON 格式结果。"""
        mock_results = [
            {"title": "Result 1", "url": "https://example.com/1", "snippet": "Snippet 1", "source": "test"},
        ]

        with patch("app.tools.builtin.web_search._search_duckduckgo") as mock_search:
            mock_search.return_value = (mock_results, None)

            result = _web_search({"query": "test query"})

            # 应该返回有效的 JSON
            parsed = json.loads(result)
            assert parsed["query"] == "test query"
            assert parsed["count"] == 1
            assert parsed["backend"] == "duckduckgo"
            assert len(parsed["results"]) == 1
            assert parsed["results"][0]["title"] == "Result 1"

    def test_failed_search_returns_error(self):
        """失败的搜索应该返回错误信息。"""
        with patch("app.tools.builtin.web_search._search_duckduckgo") as mock_ddg, \
             patch("app.tools.builtin.web_search._search_with_requests") as mock_html:

            mock_ddg.return_value = ([], "DuckDuckGo error")
            mock_html.return_value = ([], "HTML error")

            result = _web_search({"query": "test", "engine": "auto"})

            assert "error:" in result
            assert "DuckDuckGo error" in result

    def test_auto_fallback_to_html(self):
        """auto 模式下 DuckDuckGo 失败应该 fallback 到 HTML。"""
        html_results = [
            {"title": "HTML Result", "url": "https://example.com", "snippet": "From HTML", "source": "html"},
        ]

        with patch("app.tools.builtin.web_search._search_duckduckgo") as mock_ddg, \
             patch("app.tools.builtin.web_search._search_with_requests") as mock_html:

            mock_ddg.return_value = ([], "DuckDuckGo failed")
            mock_html.return_value = (html_results, None)

            result = _web_search({"query": "test", "engine": "auto"})

            # 应该使用 HTML 后端的结果
            parsed = json.loads(result)
            assert parsed["backend"] == "duckduckgo_html"
            assert parsed["count"] == 1

    def test_explicit_engine_selection(self):
        """明确指定引擎应该只使用该引擎。"""
        with patch("app.tools.builtin.web_search._search_with_requests") as mock_html:
            mock_html.return_value = ([], None)

            result = _web_search({"query": "test", "engine": "duckduckgo_html"})

            # 应该调用 HTML 搜索
            mock_html.assert_called_once()

    def test_result_includes_metadata(self):
        """结果应该包含元数据(耗时、后端等)。"""
        mock_results = [
            {"title": "Test", "url": "https://example.com", "snippet": "Test", "source": "test"}
        ]
        with patch("app.tools.builtin.web_search._search_duckduckgo") as mock_search:
            mock_search.return_value = (mock_results, None)

            result = _web_search({"query": "test"})

            parsed = json.loads(result)
            assert "backend" in parsed
            assert "elapsed_seconds" in parsed
            assert "count" in parsed
            assert "truncated" in parsed


class TestDefaultTools:
    """测试默认工具导出。"""

    def test_default_tools_export(self):
        """default_tools() 应该返回 web_search 工具。"""
        from app.tools.builtin.web_search import default_tools

        tools = default_tools()
        assert len(tools) == 1
        assert tools[0].name == "web_search"
