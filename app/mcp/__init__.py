"""MCP Client 层 —— AgentLab 作为 MCP Client 连接外部 MCP Server。

见 technical_architecture.md §9。当前实现 stdio transport + 工具发现 + 同步调用桥,
首个接入的 server 是 Playwright MCP(浏览器控制)。
"""
