"""离线测试:MCP server 配置加载。"""
from pathlib import Path

from app.mcp.config import (
    MCPServerConfig,
    enabled_servers,
    load_mcp_servers,
)


def test_missing_file_returns_empty(tmp_path):
    assert load_mcp_servers(tmp_path / "nope.yaml") == []


def test_parses_server(tmp_path):
    cfg = tmp_path / "mcp.yaml"
    cfg.write_text(
        "servers:\n"
        "  playwright:\n"
        "    transport: stdio\n"
        "    command: npx\n"
        "    args: ['-y', '@playwright/mcp@latest']\n"
        "    cwd: .\n"
        "    enabled: true\n"
        "    risk: browser_control\n"
        "    auto_approve: ['browser_snapshot']\n",
        encoding="utf-8",
    )
    servers = load_mcp_servers(cfg)
    assert len(servers) == 1
    s = servers[0]
    assert s.name == "playwright"
    assert s.command == "npx"
    assert s.args == ["-y", "@playwright/mcp@latest"]
    assert s.cwd == "."
    assert s.enabled is True
    assert s.auto_approve == ["browser_snapshot"]


def test_defaults(tmp_path):
    cfg = tmp_path / "mcp.yaml"
    cfg.write_text("servers:\n  foo:\n    command: foo\n", encoding="utf-8")
    s = load_mcp_servers(cfg)[0]
    assert s.transport == "stdio"        # 默认 stdio
    assert s.enabled is False            # 新 server 默认禁用
    assert s.cwd is None
    assert s.env_allowlist == ["PATH"]   # 默认只透传 PATH
    assert s.auto_approve == []


def test_enabled_servers_filters(tmp_path):
    cfg = tmp_path / "mcp.yaml"
    cfg.write_text(
        "servers:\n"
        "  alpha:\n    command: a\n    enabled: true\n"
        "  beta:\n    command: b\n    enabled: false\n",
        encoding="utf-8",
    )
    names = [s.name for s in enabled_servers(cfg)]
    assert names == ["alpha"]


def test_empty_servers_section(tmp_path):
    cfg = tmp_path / "mcp.yaml"
    cfg.write_text("servers:\n", encoding="utf-8")
    assert load_mcp_servers(cfg) == []
